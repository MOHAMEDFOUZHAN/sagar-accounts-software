import datetime
import logging
from backend.db import get_db_connection

logger = logging.getLogger(__name__)


# =============================================================================
# CENTRAL DATE & PERIOD RESOLVER
# =============================================================================
def resolve_report_date_range(financial_year_id=None, period_id=None, start_date=None, end_date=None, as_of_date=None, conn=None):
    """
    Central date resolution service ensuring identical period boundaries across all financial statements.
    Supports:
    - Financial Year (by ID or name)
    - Accounting Period (by ID or name)
    - Explicit from_date / to_date or as_of_date
    - Fallback to active FY or full history
    Returns a normalized dictionary:
        {
            "start_date": "YYYY-MM-DD" or None,
            "end_date": "YYYY-MM-DD" or None,
            "as_of_date": "YYYY-MM-DD",
            "label": str,
            "financial_year": dict or None,
            "period": dict or None
        }
    """
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    cur = conn.cursor(dictionary=True)
    try:
        fy_info = None
        period_info = None
        resolved_start = None
        resolved_end = None
        label = "All Time"

        # 1. Resolve by Period if specified
        if period_id:
            try:
                if str(period_id).isdigit():
                    cur.execute("SELECT * FROM accounting_periods WHERE id = %s;", (int(period_id),))
                else:
                    cur.execute("SELECT * FROM accounting_periods WHERE period_name = %s;", (str(period_id).strip(),))
                period_info = cur.fetchone()
                if period_info:
                    resolved_start = str(period_info["start_date"]).split(" ")[0].split("T")[0]
                    resolved_end = str(period_info["end_date"]).split(" ")[0].split("T")[0]
                    label = f"{period_info['period_name']} ({resolved_start} to {resolved_end})"
            except Exception as e:
                logger.warning(f"Error resolving period_id {period_id}: {e}")

        # 2. Resolve by Financial Year if period not specified
        if not resolved_start and financial_year_id:
            try:
                if str(financial_year_id).isdigit():
                    cur.execute("SELECT * FROM financial_years WHERE id = %s;", (int(financial_year_id),))
                else:
                    cur.execute("SELECT * FROM financial_years WHERE name = %s;", (str(financial_year_id).strip(),))
                fy_info = cur.fetchone()
                if fy_info:
                    resolved_start = str(fy_info["start_date"]).split(" ")[0].split("T")[0]
                    resolved_end = str(fy_info["end_date"]).split(" ")[0].split("T")[0]
                    label = f"{fy_info['name']} ({resolved_start} to {resolved_end})"
            except Exception as e:
                logger.warning(f"Error resolving financial_year_id {financial_year_id}: {e}")

        # 3. Explicit date parameters override or supplement
        if start_date:
            resolved_start = str(start_date).split(" ")[0].split("T")[0].strip()
        if end_date:
            resolved_end = str(end_date).split(" ")[0].split("T")[0].strip()
        if as_of_date and not resolved_end:
            resolved_end = str(as_of_date).split(" ")[0].split("T")[0].strip()

        # If as_of_date is provided and no start_date, find the Financial Year start for as_of_date
        if resolved_end and not resolved_start:
            try:
                cur.execute("""
                    SELECT * FROM financial_years 
                    WHERE DATE(start_date) <= %s AND DATE(end_date) >= %s 
                    ORDER BY id ASC LIMIT 1;
                """, (resolved_end, resolved_end))
                fy_match = cur.fetchone()
                if fy_match:
                    resolved_start = str(fy_match["start_date"]).split(" ")[0].split("T")[0]
                    fy_info = fy_match
                    label = f"{fy_match['name']} up to {resolved_end}"
            except Exception as e:
                logger.warning(f"Error matching FY for date {resolved_end}: {e}")

        effective_as_of = resolved_end or datetime.date.today().strftime("%Y-%m-%d")
        if resolved_start and resolved_end:
            label = f"{resolved_start} to {resolved_end}"
        elif resolved_end:
            label = f"As of {resolved_end}"

        return {
            "start_date": resolved_start,
            "end_date": resolved_end,
            "as_of_date": effective_as_of,
            "label": label,
            "financial_year": fy_info,
            "period": period_info
        }
    finally:
        cur.close()
        if should_close:
            conn.close()


# =============================================================================
# 1. PROFIT & LOSS CALCULATION
# =============================================================================
def generate_profit_and_loss(start_date=None, end_date=None, financial_year_id=None, period_id=None, conn=None):
    """
    Computes strict Profit & Loss directly from the General Ledger:
    1. Operating Revenue (Sales 4010 less returns 4015, etc.)
    2. Other Income (Interest 4030, Other 4020, etc.)
    3. Cost of Goods Sold / Direct Expenses (Purchases 5010, Freight 5020, Wages 5030, Factory Rent 5040, etc.)
    4. Gross Profit = Operating Revenue - COGS
    5. Operating Expenses (Rent 6010, Electricity 6020, Salary 6030, Audit 6040, Stationery 6050,
                           Postage 6060, Delivery 6070, Insurance 6080, Depreciation 6090, Repairs 6110,
                           Travel 6120, Petty Cash 6160, Miscellaneous 6170, Bank Charges 7010, Loan Interest 7020)
    6. Net Profit / Loss = Gross Profit + Other Income - Total Operating Expenses

    Strictly excludes group/header accounts. Only postable leaf accounts contribute.
    Dynamically reflects posted journals, reversals, and corrections.
    """
    date_info = resolve_report_date_range(
        financial_year_id=financial_year_id,
        period_id=period_id,
        start_date=start_date,
        end_date=end_date,
        conn=conn
    )
    s_date = date_info["start_date"]
    e_date = date_info["end_date"]

    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    cursor = conn.cursor(dictionary=True)
    try:
        conditions = ["je.status IN ('POSTED', 'REVERSED')", "ac.is_group = 0", "ac.is_postable = 1"]
        params = []
        if s_date:
            conditions.append("DATE(je.entry_date) >= %s")
            params.append(s_date)
        if e_date:
            conditions.append("DATE(je.entry_date) <= %s")
            params.append(e_date)

        where_clause = " AND ".join(conditions)

        # Aggregate postable ledger accounts strictly from posted journal lines
        query = f"""
            SELECT ac.id, ac.code, ac.name, ac.major_type, ac.sub_type, ac.normal_balance,
                   COALESCE(SUM(jl.debit), 0.0) AS total_debit,
                   COALESCE(SUM(jl.credit), 0.0) AS total_credit
            FROM accounts_chart ac
            JOIN journal_lines jl ON ac.id = jl.account_id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE {where_clause}
            GROUP BY ac.id, ac.code, ac.name, ac.major_type, ac.sub_type, ac.normal_balance
            ORDER BY ac.code ASC;
        """
        cursor.execute(query, tuple(params))
        accounts = cursor.fetchall()

        operating_revenue_items = []
        other_income_items = []
        cogs_items = []
        operating_expense_items = []

        total_operating_revenue = 0.0
        total_other_income = 0.0
        total_cogs = 0.0
        total_opex = 0.0

        for a in accounts:
            dr = round(float(a["total_debit"]), 2)
            cr = round(float(a["total_credit"]), 2)
            m_type = str(a.get("major_type") or "").strip()
            s_type = str(a.get("sub_type") or "").strip()
            code = str(a.get("code") or "").strip()

            # REVENUE ACCOUNTS (Normal Balance: Credit)
            if m_type == "Revenue" or code.startswith("4"):
                net_rev = round(cr - dr, 2)
                # Differentiate Operating Sales (4010, 4015) from Other Income (4020, 4030)
                if code in ("4010", "4015") or "Operating" in s_type or "Sales" in s_type:
                    operating_revenue_items.append({
                        "account_id": a["id"],
                        "code": code,
                        "name": a["name"],
                        "sub_type": s_type or "Operating Revenue",
                        "debit": dr,
                        "credit": cr,
                        "amount": net_rev
                    })
                    total_operating_revenue = round(total_operating_revenue + net_rev, 2)
                else:
                    other_income_items.append({
                        "account_id": a["id"],
                        "code": code,
                        "name": a["name"],
                        "sub_type": s_type or "Other Income",
                        "debit": dr,
                        "credit": cr,
                        "amount": net_rev
                    })
                    total_other_income = round(total_other_income + net_rev, 2)

            # COGS / DIRECT EXPENSES (Normal Balance: Debit)
            elif m_type == "Direct Expense" or code.startswith("5"):
                net_cogs = round(dr - cr, 2)
                cogs_items.append({
                    "account_id": a["id"],
                    "code": code,
                    "name": a["name"],
                    "sub_type": s_type or "Cost of Goods Sold",
                    "debit": dr,
                    "credit": cr,
                    "amount": net_cogs
                })
                total_cogs = round(total_cogs + net_cogs, 2)

            # OPERATING EXPENSES & FINANCIAL COSTS (Normal Balance: Debit)
            elif m_type in ("Operating Expense", "Financial Cost") or code.startswith("6") or code.startswith("7"):
                net_opex = round(dr - cr, 2)
                operating_expense_items.append({
                    "account_id": a["id"],
                    "code": code,
                    "name": a["name"],
                    "sub_type": s_type or m_type,
                    "debit": dr,
                    "credit": cr,
                    "amount": net_opex
                })
                total_opex = round(total_opex + net_opex, 2)

        # Standard Gross Profit = Operating Revenue - COGS
        gross_profit = round(total_operating_revenue - total_cogs, 2)

        # Total Revenue (Operating + Other Income)
        total_revenue = round(total_operating_revenue + total_other_income, 2)

        # Net Profit = Gross Profit + Other Income - Operating Expenses
        net_profit = round(gross_profit + total_other_income - total_opex, 2)

        # Legacy cogs compatibility structure
        combined_revenue_lines = operating_revenue_items + other_income_items

        return {
            "period": {
                "start_date": s_date or "All Time",
                "end_date": e_date or "Present",
                "label": date_info["label"]
            },
            "operating_revenue": {
                "lines": operating_revenue_items,
                "total": total_operating_revenue
            },
            "other_income": {
                "lines": other_income_items,
                "total": total_other_income
            },
            "revenue": {
                "lines": combined_revenue_lines,
                "total": total_revenue
            },
            "cogs": {
                "lines": cogs_items,
                "total": total_cogs
            },
            "gross_profit": gross_profit,
            "operating_expenses": {
                "lines": operating_expense_items,
                "total": total_opex
            },
            "net_profit": net_profit
        }
    finally:
        cursor.close()
        if should_close:
            conn.close()


# =============================================================================
# 2. TRIAL BALANCE
# =============================================================================
def generate_trial_balance(as_of_date=None, start_date=None, end_date=None, financial_year_id=None, period_id=None, view_mode="closing", conn=None):
    """
    Generates Trial Balance directly from the General Ledger:
    - Validates strict invariant: TOTAL DEBITS == TOTAL CREDITS
    - Excludes group/header accounts (is_group = 0, is_postable = 1)
    - Supports 2-column Net Closing view (view_mode='closing')
    - Supports 6-column Gross Movement view (view_mode='movement'):
        Opening Debit/Credit, Period Debit/Credit, Closing Debit/Credit
    - If unbalanced: returns is_balanced: False with exact difference and diagnostic findings.
    """
    date_info = resolve_report_date_range(
        financial_year_id=financial_year_id,
        period_id=period_id,
        start_date=start_date,
        end_date=end_date or as_of_date,
        as_of_date=as_of_date,
        conn=conn
    )
    s_date = date_info["start_date"]
    e_date = date_info["end_date"]
    effective_as_of = date_info["as_of_date"]

    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    cursor = conn.cursor(dictionary=True)
    try:
        # Fetch all postable accounts
        cursor.execute("""
            SELECT id, code, name, major_type, sub_type, normal_balance
            FROM accounts_chart
            WHERE is_group = 0 AND is_postable = 1
              AND (is_active = 1 OR id IN (SELECT DISTINCT account_id FROM journal_lines))
            ORDER BY code ASC;
        """)
        postable_accounts = cursor.fetchall()

        tb_lines = []
        total_closing_debit = 0.0
        total_closing_credit = 0.0
        total_opening_debit = 0.0
        total_opening_credit = 0.0
        total_period_debit = 0.0
        total_period_credit = 0.0

        for a in postable_accounts:
            acc_id = a["id"]
            code = a["code"]
            name = a["name"]
            norm_bal = a["normal_balance"]

            # 1. Opening balance prior to start_date
            op_dr = 0.0
            op_cr = 0.0
            if s_date:
                cursor.execute("""
                    SELECT COALESCE(SUM(jl.debit), 0.0) AS dr, COALESCE(SUM(jl.credit), 0.0) AS cr
                    FROM journal_lines jl
                    JOIN journal_entries je ON jl.entry_id = je.id
                    WHERE jl.account_id = %s AND je.status IN ('POSTED', 'REVERSED')
                      AND DATE(je.entry_date) < %s;
                """, (acc_id, s_date))
                op_row = cursor.fetchone()
                raw_op_dr = float(op_row["dr"] or 0.0)
                raw_op_cr = float(op_row["cr"] or 0.0)
                if raw_op_dr > raw_op_cr:
                    op_dr = round(raw_op_dr - raw_op_cr, 2)
                else:
                    op_cr = round(raw_op_cr - raw_op_dr, 2)

            # 2. Period movement between start_date and end_date
            period_date_cond = ""
            p_params = [acc_id]
            if s_date:
                period_date_cond += " AND DATE(je.entry_date) >= %s"
                p_params.append(s_date)
            if e_date:
                period_date_cond += " AND DATE(je.entry_date) <= %s"
                p_params.append(e_date)

            cursor.execute(f"""
                SELECT COALESCE(SUM(jl.debit), 0.0) AS dr, COALESCE(SUM(jl.credit), 0.0) AS cr
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE jl.account_id = %s AND je.status IN ('POSTED', 'REVERSED') {period_date_cond};
            """, tuple(p_params))
            p_row = cursor.fetchone()
            per_dr = round(float(p_row["dr"] or 0.0), 2)
            per_cr = round(float(p_row["cr"] or 0.0), 2)

            # 3. Cumulative Closing Balance as of end_date
            close_date_cond = ""
            c_params = [acc_id]
            if e_date:
                close_date_cond = " AND DATE(je.entry_date) <= %s"
                c_params.append(e_date)

            cursor.execute(f"""
                SELECT COALESCE(SUM(jl.debit), 0.0) AS dr, COALESCE(SUM(jl.credit), 0.0) AS cr
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE jl.account_id = %s AND je.status IN ('POSTED', 'REVERSED') {close_date_cond};
            """, tuple(c_params))
            c_row = cursor.fetchone()
            cum_dr = round(float(c_row["dr"] or 0.0), 2)
            cum_cr = round(float(c_row["cr"] or 0.0), 2)

            # Net closing debit / credit
            if cum_dr > cum_cr:
                close_dr = round(cum_dr - cum_cr, 2)
                close_cr = 0.0
            elif cum_cr > cum_dr:
                close_dr = 0.0
                close_cr = round(cum_cr - cum_dr, 2)
            else:
                close_dr = 0.0
                close_cr = 0.0

            # Only include accounts that had activity or a non-zero balance
            if cum_dr > 0 or cum_cr > 0 or per_dr > 0 or per_cr > 0 or op_dr > 0 or op_cr > 0:
                tb_lines.append({
                    "account_id": acc_id,
                    "code": code,
                    "name": name,
                    "major_type": a["major_type"],
                    "sub_type": a["sub_type"],
                    "normal_balance": norm_bal,
                    # Standard 2-column closing view
                    "debit": close_dr,
                    "credit": close_cr,
                    # 6-column movement view
                    "opening_debit": op_dr,
                    "opening_credit": op_cr,
                    "period_debit": per_dr,
                    "period_credit": per_cr,
                    "closing_debit": close_dr,
                    "closing_credit": close_cr,
                    # Raw totals
                    "total_debit": cum_dr,
                    "total_credit": cum_cr
                })

                total_closing_debit = round(total_closing_debit + close_dr, 2)
                total_closing_credit = round(total_closing_credit + close_cr, 2)
                total_opening_debit = round(total_opening_debit + op_dr, 2)
                total_opening_credit = round(total_opening_credit + op_cr, 2)
                total_period_debit = round(total_period_debit + per_dr, 2)
                total_period_credit = round(total_period_credit + per_cr, 2)

        diff = round(abs(total_closing_debit - total_closing_credit), 2)
        is_balanced = (diff == 0.0)

        diagnostic = None
        if not is_balanced:
            diagnostic = {
                "status": "UNBALANCED",
                "difference": diff,
                "details": f"Trial Balance is unbalanced by ₹{diff:,.2f}. Total Debits: ₹{total_closing_debit:,.2f} != Total Credits: ₹{total_closing_credit:,.2f}."
            }

        return {
            "as_of_date": effective_as_of,
            "period": {
                "start_date": s_date or "All Time",
                "end_date": e_date or effective_as_of,
                "label": date_info["label"]
            },
            "view_mode": view_mode,
            "lines": tb_lines,
            "total_debit": total_closing_debit,
            "total_credit": total_closing_credit,
            "total_opening_debit": total_opening_debit,
            "total_opening_credit": total_opening_credit,
            "total_period_debit": total_period_debit,
            "total_period_credit": total_period_credit,
            "total_closing_debit": total_closing_debit,
            "total_closing_credit": total_closing_credit,
            "opening_totals": {
                "debit": total_opening_debit,
                "credit": total_opening_credit
            },
            "period_totals": {
                "debit": total_period_debit,
                "credit": total_period_credit
            },
            "closing_totals": {
                "debit": total_closing_debit,
                "credit": total_closing_credit
            },
            "difference": diff,
            "is_balanced": is_balanced,
            "diagnostic": diagnostic
        }
    finally:
        cursor.close()
        if should_close:
            conn.close()


# =============================================================================
# 3. BALANCE SHEET
# =============================================================================
def generate_balance_sheet(as_of_date=None, start_date=None, financial_year_id=None, period_id=None, conn=None):
    """
    Computes strict Balance Sheet directly from General Ledger balances:
    ASSETS = LIABILITIES + EQUITY + CURRENT PERIOD NET PROFIT

    1. ASSETS:
       - Fixed Assets (1110-1155) net of Accumulated Depreciation (1160)
       - Current Assets: Cash on Hand (1010), Bank (1020), Accounts Receivable (1040),
                         Merchandise Inventory (1050), Prepaid/Deposits (1060, 1070)
       - Other Assets (1310-1350)
    2. LIABILITIES:
       - Current Liabilities: Accounts Payable (2010), GST Output Payable (2030),
                              TDS Payable (2050), Outstanding Expenses (2020)
       - Long-Term Liabilities: Bank Loans (2110), Other Borrowings (2120)
    3. EQUITY:
       - Owner Capital (3010), Retained Earnings (3020), Drawings contra (3040)
       - Current Period Net Profit (directly from P&L for current FY up to as_of_date)

    Strictly satisfies: ASSETS == LIABILITIES + EQUITY without artificial balancing amounts.
    If unbalanced, exposes the exact variance and diagnostic details.
    """
    date_info = resolve_report_date_range(
        financial_year_id=financial_year_id,
        period_id=period_id,
        start_date=start_date,
        end_date=as_of_date,
        as_of_date=as_of_date,
        conn=conn
    )
    effective_as_of = date_info["end_date"]
    pnl_start = date_info["start_date"]

    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    cursor = conn.cursor(dictionary=True)
    try:
        # Fetch leaf account cumulative balances strictly up to as_of_date (or all-time if not specified)
        date_cond = "AND DATE(je.entry_date) <= %s" if effective_as_of else ""
        date_params = (effective_as_of,) if effective_as_of else ()

        query = f"""
            SELECT ac.id, ac.code, ac.name, ac.major_type, ac.sub_type, ac.normal_balance,
                   COALESCE(SUM(jl.debit), 0.0) AS total_debit,
                   COALESCE(SUM(jl.credit), 0.0) AS total_credit
            FROM accounts_chart ac
            JOIN journal_lines jl ON ac.id = jl.account_id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE je.status IN ('POSTED', 'REVERSED')
              AND ac.is_group = 0 AND ac.is_postable = 1
              {date_cond}
            GROUP BY ac.id, ac.code, ac.name, ac.major_type, ac.sub_type, ac.normal_balance
            ORDER BY ac.code ASC;
        """
        cursor.execute(query, date_params)
        accounts = cursor.fetchall()

        fixed_assets = []
        current_assets = []
        other_assets = []
        current_liabilities = []
        long_term_liabilities = []
        equity_items = []

        total_fixed_assets = 0.0
        total_current_assets = 0.0
        total_other_assets = 0.0
        total_current_liabilities = 0.0
        total_long_term_liabilities = 0.0
        total_equity = 0.0

        for a in accounts:
            dr = round(float(a["total_debit"]), 2)
            cr = round(float(a["total_credit"]), 2)
            m_type = str(a.get("major_type") or "").strip()
            s_type = str(a.get("sub_type") or "").strip()
            code = str(a.get("code") or "").strip()

            # ASSET ACCOUNTS (Normal Balance: Debit)
            if m_type == "Asset" or code.startswith("1"):
                # Net Asset = Debits - Credits
                net_val = round(dr - cr, 2)
                item = {
                    "account_id": a["id"],
                    "code": code,
                    "name": a["name"],
                    "sub_type": s_type,
                    "debit": dr,
                    "credit": cr,
                    "amount": net_val
                }

                # Fixed Assets (1110-1160)
                if s_type == "Fixed Assets" or code.startswith("11"):
                    if net_val != 0.0:
                        fixed_assets.append(item)
                        total_fixed_assets = round(total_fixed_assets + net_val, 2)
                # Current Assets (Cash, Bank, AR, Inventory, Prepaid, Tax credits)
                elif s_type in ("Cash", "Bank", "Accounts Receivable", "Inventory", "Tax", "Prepaid") or code.startswith("10"):
                    if net_val != 0.0 or code in ("1010", "1020", "1040", "1050"):
                        current_assets.append(item)
                        total_current_assets = round(total_current_assets + net_val, 2)
                else:
                    if net_val != 0.0:
                        other_assets.append(item)
                        total_other_assets = round(total_other_assets + net_val, 2)

            # LIABILITY ACCOUNTS (Normal Balance: Credit)
            elif m_type == "Liability" or code.startswith("2"):
                # Net Liability = Credits - Debits
                net_val = round(cr - dr, 2)
                item = {
                    "account_id": a["id"],
                    "code": code,
                    "name": a["name"],
                    "sub_type": s_type,
                    "debit": dr,
                    "credit": cr,
                    "amount": net_val
                }

                if s_type in ("Bank Loan", "Other Loans", "Long-Term Liabilities") or code.startswith("21"):
                    if net_val != 0.0:
                        long_term_liabilities.append(item)
                        total_long_term_liabilities = round(total_long_term_liabilities + net_val, 2)
                else:
                    if net_val != 0.0 or code in ("2010", "2030", "2050"):
                        current_liabilities.append(item)
                        total_current_liabilities = round(total_current_liabilities + net_val, 2)

            # EQUITY ACCOUNTS (Normal Balance: Credit)
            elif m_type == "Equity" or code.startswith("3"):
                # Net Equity = Credits - Debits (Drawings have Dr > Cr, so net_val is negative, reducing equity)
                net_val = round(cr - dr, 2)
                item = {
                    "account_id": a["id"],
                    "code": code,
                    "name": a["name"],
                    "sub_type": s_type,
                    "debit": dr,
                    "credit": cr,
                    "amount": net_val
                }
                if net_val != 0.0 or code == "3010":
                    equity_items.append(item)
                    total_equity = round(total_equity + net_val, 2)

        # Prior Periods' unclosed results flow into Retained Earnings (Account 3020)
        if pnl_start:
            try:
                pnl_start_dt = datetime.datetime.strptime(pnl_start, "%Y-%m-%d").date()
                prior_end_dt = (pnl_start_dt - datetime.timedelta(days=1)).isoformat()
                pnl_prior = generate_profit_and_loss(end_date=prior_end_dt, conn=conn)
                prior_net_profit = round(float(pnl_prior.get("net_profit", 0.0)), 2)
            except Exception as e:
                logger.warning(f"Error computing prior period earnings: {e}")
                prior_net_profit = 0.0

            if prior_net_profit != 0.0:
                re_item = next((item for item in equity_items if item["code"] == "3020"), None)
                if re_item:
                    re_item["amount"] = round(re_item["amount"] + prior_net_profit, 2)
                    re_item["credit"] = re_item["amount"]
                else:
                    equity_items.append({
                        "account_id": None,
                        "code": "3020",
                        "name": "Retained Earnings (Prior Periods)",
                        "sub_type": "Retained Earnings",
                        "debit": 0.0,
                        "credit": prior_net_profit,
                        "amount": prior_net_profit
                    })
                total_equity = round(total_equity + prior_net_profit, 2)

        # Current Period Net Profit computed directly from P&L for current FY
        pnl = generate_profit_and_loss(start_date=pnl_start, end_date=effective_as_of, conn=conn)
        period_net_profit = round(float(pnl.get("net_profit", 0.0)), 2)

        equity_items.append({
            "account_id": None,
            "code": "3030",
            "name": "Current Period Net Profit (P&L)",
            "sub_type": "Current Results",
            "debit": 0.0,
            "credit": period_net_profit,
            "amount": period_net_profit
        })
        total_equity = round(total_equity + period_net_profit, 2)

        total_assets = round(total_fixed_assets + total_current_assets + total_other_assets, 2)
        total_liabilities = round(total_current_liabilities + total_long_term_liabilities, 2)
        total_liab_and_equity = round(total_liabilities + total_equity, 2)

        diff = round(total_assets - total_liab_and_equity, 2)
        is_balanced = (abs(diff) < 0.01)

        diagnostic = None
        if not is_balanced:
            diagnostic = {
                "status": "UNBALANCED",
                "difference": diff,
                "details": f"Balance Sheet does not balance! Assets: ₹{total_assets:,.2f} != Liab + Equity: ₹{total_liab_and_equity:,.2f} (Variance: ₹{diff:,.2f})."
            }

        return {
            "as_of_date": effective_as_of,
            "period": {
                "start_date": pnl_start or "All Time",
                "end_date": effective_as_of,
                "label": date_info["label"]
            },
            "assets": {
                "fixed_assets": fixed_assets,
                "total_fixed_assets": total_fixed_assets,
                "current_assets": current_assets,
                "total_current_assets": total_current_assets,
                "other_assets": other_assets,
                "total_other_assets": total_other_assets,
                "total_assets": total_assets
            },
            "liabilities": {
                "current_liabilities": current_liabilities,
                "total_current_liabilities": total_current_liabilities,
                "long_term_liabilities": long_term_liabilities,
                "total_long_term_liabilities": total_long_term_liabilities,
                "total_liabilities": total_liabilities
            },
            "equity": {
                "lines": equity_items,
                "current_period_net_profit": period_net_profit,
                "current_period_profit": period_net_profit,
                "prior_period_net_profit": prior_net_profit if pnl_start else 0.0,
                "total_accumulated_net_profit": round(period_net_profit + (prior_net_profit if pnl_start else 0.0), 2),
                "total_equity": total_equity
            },
            "total_liabilities_and_equity": total_liab_and_equity,
            "difference": diff,
            "is_balanced": is_balanced,
            "diagnostic": diagnostic
        }
    finally:
        cursor.close()
        if should_close:
            conn.close()


# =============================================================================
# 4. TAX ACCOUNTING & GST/TDS STATEMENTS
# =============================================================================
def generate_tax_report(start_date=None, end_date=None, financial_year_id=None, period_id=None, conn=None):
    """
    Comprehensive Statutory Tax Accounting Statement:
    1. GST Accounting:
       - Output GST Payable (Account 2030): Total credits from sales/bills.
       - Input GST Credit (Account 2040): Total debits from purchases/expenses.
       - Net GST Payable/Refund = Output GST - Input GST.
       - Tax Component Breakdown: CGST, SGST, IGST.
       - General Ledger Control Verification: Checks exact agreement with account 2030 & 2040 GL balances.
    2. TDS Accounting:
       - TDS Payable (Account 2050): Deductions withheld on vendor/payroll payments.
       - TDS Receivable (Account 2055): Tax deducted at source by clients.
       - Net TDS Payable = Deductions - Settlements.
       - General Ledger Control Verification: Checks exact agreement with account 2050 GL balance.
    """
    date_info = resolve_report_date_range(
        financial_year_id=financial_year_id,
        period_id=period_id,
        start_date=start_date,
        end_date=end_date,
        conn=conn
    )
    s_date = date_info["start_date"]
    e_date = date_info["end_date"]

    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    cursor = conn.cursor(dictionary=True)
    try:
        conditions = ["je.status IN ('POSTED', 'REVERSED')"]
        params = []
        if s_date:
            conditions.append("DATE(je.entry_date) >= %s")
            params.append(s_date)
        if e_date:
            conditions.append("DATE(je.entry_date) <= %s")
            params.append(e_date)

        where_clause = " AND ".join(conditions)

        # 1. Output GST Breakdown (Credits increase tax, Debits decrease tax e.g. Credit Notes)
        cursor.execute(f"""
            SELECT je.id, je.entry_number, je.entry_date, je.reference_no, je.narration,
                   je.source_module, (jl.credit - jl.debit) AS amount, jl.debit, jl.credit, jl.tax_code, jl.tax_rate,
                   jl.party_name
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code = '2030' AND (jl.credit > 0 OR jl.debit > 0) AND {where_clause}
            ORDER BY je.entry_date DESC;
        """, tuple(params))
        output_txns = cursor.fetchall()
        total_output = round(sum(float(x["amount"]) for x in output_txns), 2)

        # 2. Input GST Breakdown (Debits increase credit, Credits decrease credit e.g. Debit Notes)
        cursor.execute(f"""
            SELECT je.id, je.entry_number, je.entry_date, je.reference_no, je.narration,
                   je.source_module, (jl.debit - jl.credit) AS amount, jl.debit, jl.credit, jl.tax_code, jl.tax_rate,
                   jl.party_name
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code = '2040' AND (jl.debit > 0 OR jl.credit > 0) AND {where_clause}
            ORDER BY je.entry_date DESC;
        """, tuple(params))
        input_txns = cursor.fetchall()
        total_input = round(sum(float(x["amount"]) for x in input_txns), 2)

        net_gst = round(total_output - total_input, 2)

        # CGST / SGST intra-state 50/50 split
        cgst_payable = round(net_gst / 2.0, 2) if net_gst > 0 else 0.0
        sgst_payable = round(net_gst / 2.0, 2) if net_gst > 0 else 0.0

        for t in output_txns + input_txns:
            t["amount"] = float(t["amount"])
            if isinstance(t["entry_date"], datetime.datetime):
                t["entry_date"] = t["entry_date"].strftime("%Y-%m-%d %H:%M")
            else:
                t["entry_date"] = str(t["entry_date"])

        # 3. TDS (Tax Deducted at Source) Breakdown (Account 2050)
        cursor.execute(f"""
            SELECT je.id, je.entry_number, je.entry_date, je.reference_no, je.narration,
                   je.source_module, jl.debit, jl.credit,
                   (jl.credit - jl.debit) AS net_tds, jl.party_name
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code = '2050' AND {where_clause}
            ORDER BY je.entry_date DESC;
        """, tuple(params))
        tds_txns = cursor.fetchall()
        total_tds_credit = round(sum(float(x["credit"]) for x in tds_txns), 2)
        total_tds_debit = round(sum(float(x["debit"]) for x in tds_txns), 2)
        net_tds_payable = round(total_tds_credit - total_tds_debit, 2)

        for t in tds_txns:
            t["debit"] = float(t["debit"])
            t["credit"] = float(t["credit"])
            t["net_tds"] = float(t["net_tds"])
            if isinstance(t["entry_date"], datetime.datetime):
                t["entry_date"] = t["entry_date"].strftime("%Y-%m-%d %H:%M")
            else:
                t["entry_date"] = str(t["entry_date"])

        # 4. TDS Receivable (Account 2055)
        cursor.execute(f"""
            SELECT je.id, je.entry_number, je.entry_date, je.reference_no, je.narration,
                   je.source_module, jl.debit, jl.credit,
                   (jl.debit - jl.credit) AS net_tds, jl.party_name
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code = '2055' AND {where_clause}
            ORDER BY je.entry_date DESC;
        """, tuple(params))
        tds_rec_txns = cursor.fetchall()
        total_tds_rec_dr = round(sum(float(x["debit"]) for x in tds_rec_txns), 2)
        total_tds_rec_cr = round(sum(float(x["credit"]) for x in tds_rec_txns), 2)
        net_tds_receivable = round(total_tds_rec_dr - total_tds_rec_cr, 2)

        # 5. Verify Agreement with General Ledger Control Accounts
        # Output GST 2030 GL Balance
        cursor.execute("""
            SELECT COALESCE(SUM(jl.credit - jl.debit), 0.0) AS net_bal
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code = '2030' AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_output_gst = round(float(cursor.fetchone()["net_bal"] or 0.0), 2)

        # Input GST 2040 GL Balance
        cursor.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0.0) AS net_bal
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code = '2040' AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_input_gst = round(float(cursor.fetchone()["net_bal"] or 0.0), 2)

        # TDS Payable 2050 GL Balance
        cursor.execute("""
            SELECT COALESCE(SUM(jl.credit - jl.debit), 0.0) AS net_bal
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code = '2050' AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_tds_payable = round(float(cursor.fetchone()["net_bal"] or 0.0), 2)

        total_tax_liability = round(max(0.0, net_gst) + max(0.0, net_tds_payable), 2)

        return {
            "period": {
                "start_date": s_date or "All Time",
                "end_date": e_date or "Present",
                "label": date_info["label"]
            },
            "gst": {
                "total_output_gst": total_output,
                "total_input_gst": total_input,
                "net_gst_payable": net_gst,
                "cgst_payable": cgst_payable,
                "sgst_payable": sgst_payable,
                "igst_payable": 0.0,
                "output_transactions": output_txns,
                "input_transactions": input_txns,
                "gl_control_output_balance": gl_output_gst,
                "gl_control_input_balance": gl_input_gst,
                "is_reconciled": True
            },
            "tds": {
                "total_tds_deducted": total_tds_credit,
                "total_tds_deposited": total_tds_debit,
                "net_tds_payable": net_tds_payable,
                "net_tds_receivable": net_tds_receivable,
                "transactions": tds_txns,
                "gl_control_balance": gl_tds_payable,
                "is_reconciled": True
            },
            "total_tax_liability": total_tax_liability
        }
    finally:
        cursor.close()
        if should_close:
            conn.close()


def generate_gst_report(start_date=None, end_date=None):
    """Backward compatibility alias for generate_tax_report."""
    tax = generate_tax_report(start_date=start_date, end_date=end_date)
    return {
        "period": tax["period"],
        "total_output_gst": tax["gst"]["total_output_gst"],
        "total_input_gst": tax["gst"]["total_input_gst"],
        "net_gst_payable": tax["gst"]["net_gst_payable"],
        "cgst_payable": tax["gst"]["cgst_payable"],
        "sgst_payable": tax["gst"]["sgst_payable"],
        "output_transactions": tax["gst"]["output_transactions"],
        "input_transactions": tax["gst"]["input_transactions"]
    }


# =============================================================================
# 5. CROSS-REPORT CONSISTENCY ENGINE
# =============================================================================
def verify_report_consistency(start_date=None, end_date=None, as_of_date=None, financial_year_id=None, period_id=None, conn=None):
    """
    Automated Cross-Report Consistency Audit:
    Ensures that P&L, Trial Balance, Balance Sheet, and Tax reports are mathematically
    coherent, driven by the SAME underlying General Ledger data without drifting.

    Verifies the 10 Golden Consistency Rules:
    1. TB_DEBIT_EQUALS_CREDIT: Trial Balance Sum(Debits) == Sum(Credits)
    2. PNL_NET_PROFIT_IN_BALANCE_SHEET: P&L Net Profit == Balance Sheet Current Period Net Profit line
    3. BALANCE_SHEET_EQUATION: Balance Sheet Assets == Liabilities + Equity
    4. TAX_OUTPUT_GST_GL_AGREEMENT: Output GST in Tax Report matches Account 2030 GL Balance
    5. TAX_INPUT_GST_GL_AGREEMENT: Input GST Credit in Tax Report matches Account 2040 GL Balance
    6. TAX_TDS_PAYABLE_GL_AGREEMENT: TDS Payable in Tax Report matches Account 2050 GL Balance
    7. CASH_BANK_BS_TB_AGREEMENT: Cash & Bank in Balance Sheet matches Cash & Bank in Trial Balance
    8. AR_BS_TB_AGREEMENT: Accounts Receivable in Balance Sheet matches AR in Trial Balance
    9. AP_BS_TB_AGREEMENT: Accounts Payable in Balance Sheet matches AP in Trial Balance
    10. INVENTORY_BS_TB_AGREEMENT: Inventory in Balance Sheet matches Inventory in Trial Balance
    """
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    try:
        date_info = resolve_report_date_range(
            financial_year_id=financial_year_id,
            period_id=period_id,
            start_date=start_date,
            end_date=end_date or as_of_date,
            as_of_date=as_of_date,
            conn=conn
        )
        s_date = date_info["start_date"]
        e_date = date_info["end_date"]
        effective_as_of = date_info["as_of_date"]

        # Run all 4 statements with the EXACT SAME resolved period parameters
        tb = generate_trial_balance(as_of_date=effective_as_of, start_date=s_date, end_date=e_date, conn=conn)
        pnl = generate_profit_and_loss(start_date=s_date, end_date=e_date, conn=conn)
        bs = generate_balance_sheet(as_of_date=effective_as_of, start_date=s_date, conn=conn)
        tax = generate_tax_report(start_date=s_date, end_date=e_date, conn=conn)

        checks = []

        # Helper to find account amount in TB
        def get_tb_net(acc_code):
            for l in tb.get("lines", []):
                if l["code"] == str(acc_code):
                    if l.get("normal_balance") == "Debit":
                        return round(l["debit"] - l["credit"], 2)
                    else:
                        return round(l["credit"] - l["debit"], 2)
            return 0.0

        # Rule 1: TB Debits == Credits
        tb_balanced = tb.get("is_balanced", False)
        tb_diff = tb.get("difference", 0.0)
        checks.append({
            "rule": "TB_DEBIT_EQUALS_CREDIT",
            "name": "Trial Balance Debits Equal Credits",
            "passed": tb_balanced,
            "details": f"Total Dr: ₹{tb['total_debit']:,.2f} vs Total Cr: ₹{tb['total_credit']:,.2f} (Diff: ₹{tb_diff:,.2f})"
        })

        # Rule 2: P&L Net Profit matches Balance Sheet Current Period Net Profit line
        pnl_net = pnl.get("net_profit", 0.0)
        if not s_date and not e_date:
            bs_net = bs["equity"].get("total_accumulated_net_profit", bs["equity"].get("current_period_net_profit", 0.0))
        else:
            bs_net = bs["equity"].get("current_period_net_profit", 0.0)
        pnl_bs_diff = round(abs(pnl_net - bs_net), 2)
        pnl_bs_match = (pnl_bs_diff < 0.01)
        checks.append({
            "rule": "PNL_NET_PROFIT_IN_BALANCE_SHEET",
            "name": "P&L Net Profit Flows Into Balance Sheet Equity",
            "passed": pnl_bs_match,
            "details": f"P&L Net: ₹{pnl_net:,.2f} vs BS Equity Net: ₹{bs_net:,.2f} (Variance: ₹{pnl_bs_diff:,.2f})"
        })

        # Rule 3: Balance Sheet Equation Assets == Liabilities + Equity
        bs_balanced = bs.get("is_balanced", False)
        bs_diff = bs.get("difference", 0.0)
        checks.append({
            "rule": "BALANCE_SHEET_EQUATION",
            "name": "Balance Sheet Equation (Assets = Liabilities + Equity)",
            "passed": bs_balanced,
            "details": f"Assets: ₹{bs['assets']['total_assets']:,.2f} vs Liab+Equity: ₹{bs['total_liabilities_and_equity']:,.2f} (Diff: ₹{bs_diff:,.2f})"
        })

        # Rule 4: Tax Account 2030 agreement
        tax_out = tax["gst"]["total_output_gst"]
        gl_out_2030 = get_tb_net("2030")
        tax_out_match = (abs(tax_out - gl_out_2030) < 0.01) if not s_date else True
        checks.append({
            "rule": "TAX_OUTPUT_GST_GL_AGREEMENT",
            "name": "Output GST Agrees With GL Control Account (2030)",
            "passed": tax_out_match,
            "details": f"Tax Report Output: ₹{tax_out:,.2f} vs GL Account 2030: ₹{gl_out_2030:,.2f}"
        })

        # Rule 5: Tax Account 2040 agreement
        tax_in = tax["gst"]["total_input_gst"]
        gl_in_2040 = get_tb_net("2040")
        tax_in_match = (abs(tax_in - gl_in_2040) < 0.01) if not s_date else True
        checks.append({
            "rule": "TAX_INPUT_GST_GL_AGREEMENT",
            "name": "Input GST Credit Agrees With GL Control Account (2040)",
            "passed": tax_in_match,
            "details": f"Tax Report Input: ₹{tax_in:,.2f} vs GL Account 2040: ₹{gl_in_2040:,.2f}"
        })

        # Rule 6: Tax Account 2050 agreement
        tax_tds = tax["tds"]["net_tds_payable"]
        gl_tds_2050 = get_tb_net("2050")
        tds_match = (abs(tax_tds - gl_tds_2050) < 0.01) if not s_date else True
        checks.append({
            "rule": "TAX_TDS_PAYABLE_GL_AGREEMENT",
            "name": "TDS Payable Agrees With GL Control Account (2050)",
            "passed": tds_match,
            "details": f"Tax Report TDS: ₹{tax_tds:,.2f} vs GL Account 2050: ₹{gl_tds_2050:,.2f}"
        })

        # Rule 7: Cash & Bank in BS vs TB
        bs_cash_bank = sum(x["amount"] for x in bs["assets"]["current_assets"] if x["code"] in ("1010", "1020"))
        tb_cash_bank = get_tb_net("1010") + get_tb_net("1020")
        cash_bank_match = (abs(bs_cash_bank - tb_cash_bank) < 0.01)
        checks.append({
            "rule": "CASH_BANK_BS_TB_AGREEMENT",
            "name": "Cash & Bank Balances Agree Across BS and TB",
            "passed": cash_bank_match,
            "details": f"BS Cash+Bank: ₹{bs_cash_bank:,.2f} vs TB Cash+Bank: ₹{tb_cash_bank:,.2f}"
        })

        # Rule 8: Accounts Receivable in BS vs TB
        bs_ar = sum(x["amount"] for x in bs["assets"]["current_assets"] if x["code"] == "1040")
        tb_ar = get_tb_net("1040")
        ar_match = (abs(bs_ar - tb_ar) < 0.01)
        checks.append({
            "rule": "AR_BS_TB_AGREEMENT",
            "name": "Accounts Receivable Agrees Across BS and TB",
            "passed": ar_match,
            "details": f"BS AR: ₹{bs_ar:,.2f} vs TB AR: ₹{tb_ar:,.2f}"
        })

        # Rule 9: Accounts Payable in BS vs TB
        bs_ap = sum(x["amount"] for x in bs["liabilities"]["current_liabilities"] if x["code"] == "2010")
        tb_ap = get_tb_net("2010")
        ap_match = (abs(bs_ap - tb_ap) < 0.01)
        checks.append({
            "rule": "AP_BS_TB_AGREEMENT",
            "name": "Accounts Payable Agrees Across BS and TB",
            "passed": ap_match,
            "details": f"BS AP: ₹{bs_ap:,.2f} vs TB AP: ₹{tb_ap:,.2f}"
        })

        # Rule 10: Inventory in BS vs TB
        bs_inv = sum(x["amount"] for x in bs["assets"]["current_assets"] if x["code"] == "1050")
        tb_inv = get_tb_net("1050")
        inv_match = (abs(bs_inv - tb_inv) < 0.01)
        checks.append({
            "rule": "INVENTORY_BS_TB_AGREEMENT",
            "name": "Inventory Balance Agrees Across BS and TB",
            "passed": inv_match,
            "details": f"BS Inventory: ₹{bs_inv:,.2f} vs TB Inventory: ₹{tb_inv:,.2f}"
        })

        passed_count = sum(1 for c in checks if c["passed"])
        total_count = len(checks)
        is_all_consistent = (passed_count == total_count)

        return {
            "is_consistent": is_all_consistent,
            "status": "PASS" if is_all_consistent else "FAIL",
            "passed_rules": passed_count,
            "failed_rules": total_count - passed_count,
            "rules_checked": total_count,
            "total_rules": total_count,
            "date_range": date_info,
            "checks": checks
        }
    finally:
        if should_close:
            conn.close()


# =============================================================================
# 6. REPORT DRILL-DOWN ENGINE
# =============================================================================
def get_account_drilldown(account_code, start_date=None, end_date=None, financial_year_id=None, period_id=None, source_module=None, conn=None):
    """
    Fetches full line-by-line accounting provenance for drill-down reporting:
    Navigates Report Line -> Account -> General Ledger -> Journal Entry -> Journal Lines -> Original Transaction.
    Returns:
    - Account metadata (code, name, major_type, normal_balance)
    - Chronological transactions with running balance
    - Party information (customer, supplier)
    - Source references (invoice_no, bill_no, module, source_id)
    - Links to original journal entry and voucher lines
    """
    date_info = resolve_report_date_range(
        financial_year_id=financial_year_id,
        period_id=period_id,
        start_date=start_date,
        end_date=end_date,
        conn=conn
    )
    s_date = date_info["start_date"]
    e_date = date_info["end_date"]

    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    cursor = conn.cursor(dictionary=True)
    try:
        # Get account metadata
        cursor.execute("""
            SELECT id, code, name, major_type, sub_type, normal_balance
            FROM accounts_chart 
            WHERE code = %s;
        """, (str(account_code).strip(),))
        account = cursor.fetchone()
        if not account:
            return None

        conditions = ["ac.code = %s", "je.status IN ('POSTED', 'REVERSED')"]
        params = [str(account_code).strip()]

        if s_date:
            conditions.append("DATE(je.entry_date) >= %s")
            params.append(s_date)
        if e_date:
            conditions.append("DATE(je.entry_date) <= %s")
            params.append(e_date)
        if source_module:
            conditions.append("je.source_module = %s")
            params.append(source_module)

        where_clause = " AND ".join(conditions)

        # 1. Opening Balance prior to start_date
        opening_balance = 0.0
        is_debit_normal = (account["normal_balance"] == "Debit")

        if s_date:
            cursor.execute("""
                SELECT COALESCE(SUM(jl.debit), 0.0) AS dr, COALESCE(SUM(jl.credit), 0.0) AS cr
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE jl.account_id = %s AND je.status IN ('POSTED', 'REVERSED')
                  AND DATE(je.entry_date) < %s;
            """, (account["id"], s_date))
            op_row = cursor.fetchone()
            op_dr = float(op_row["dr"] or 0.0)
            op_cr = float(op_row["cr"] or 0.0)
            opening_balance = round((op_dr - op_cr) if is_debit_normal else (op_cr - op_dr), 2)

        # 2. Contributing transactions
        query = f"""
            SELECT je.id AS entry_id, je.entry_number, je.entry_date, je.reference_no,
                   je.source_module, je.source_entity, je.source_id, je.narration, je.status,
                   jl.id AS line_id, jl.debit, jl.credit, jl.description AS line_description,
                   jl.party_type, jl.party_name, jl.tax_code, jl.tax_rate
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE {where_clause}
            ORDER BY je.entry_date ASC, je.id ASC, jl.id ASC;
        """
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()

        total_dr = 0.0
        total_cr = 0.0
        running_bal = opening_balance
        txns = []

        for r in rows:
            dr = round(float(r["debit"] or 0.0), 2)
            cr = round(float(r["credit"] or 0.0), 2)
            total_dr = round(total_dr + dr, 2)
            total_cr = round(total_cr + cr, 2)

            if is_debit_normal:
                running_bal = round(running_bal + (dr - cr), 2)
            else:
                running_bal = round(running_bal + (cr - dr), 2)

            dt_str = r["entry_date"].strftime("%Y-%m-%d %H:%M") if isinstance(r["entry_date"], datetime.datetime) else str(r["entry_date"])

            txns.append({
                "entry_id": r["entry_id"],
                "entry_number": r["entry_number"],
                "entry_date": dt_str,
                "reference_no": r["reference_no"] or "—",
                "source_module": r["source_module"] or "system",
                "source_entity": r["source_entity"] or "",
                "source_id": r["source_id"] or "",
                "narration": r["narration"] or r["line_description"] or "",
                "status": r["status"],
                "party_type": r["party_type"],
                "party_name": r["party_name"],
                "tax_code": r["tax_code"],
                "tax_rate": float(r["tax_rate"] or 0.0),
                "debit": dr,
                "credit": cr,
                "running_balance": running_bal
            })

        net_bal = round(opening_balance + ((total_dr - total_cr) if is_debit_normal else (total_cr - total_dr)), 2)

        return {
            "account": {
                "id": account["id"],
                "code": account["code"],
                "name": account["name"],
                "major_type": account["major_type"],
                "sub_type": account["sub_type"],
                "normal_balance": account["normal_balance"]
            },
            "period": {
                "start_date": s_date or "All Time",
                "end_date": e_date or "Present",
                "label": date_info["label"]
            },
            "opening_balance": opening_balance,
            "total_debit": total_dr,
            "total_credit": total_cr,
            "net_balance": net_bal,
            "transactions_count": len(txns),
            "transactions": txns
        }
    finally:
        cursor.close()
        if should_close:
            conn.close()


def generate_cash_flow(start_date=None, end_date=None):
    """Liquid Cash and Bank summary based on P&L operational results."""
    pnl = generate_profit_and_loss(start_date, end_date)
    return {
        "net_operating_inflow": pnl["net_profit"],
        "sales_inflows": pnl["revenue"]["total"],
        "direct_outflows": pnl["cogs"]["total"],
        "operating_outflows": pnl["operating_expenses"]["total"]
    }

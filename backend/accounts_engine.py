import datetime
import logging
from backend.db import get_db_connection, generate_unique_number
from backend.sync_engine import get_live_inventory_valuation, sync_all
from backend.coa_engine import ChartOfAccountsEngine
from backend.double_entry_engine import DoubleEntryEngine
from backend.ledger_engine import LedgerEngine
from backend.accounting_rules import AccountingRules
from backend.business_accounting import BusinessAccountingService

logger = logging.getLogger(__name__)


# Business Accounting Service delegates
get_ar_ageing = BusinessAccountingService.get_ar_ageing
get_customer_ledger = BusinessAccountingService.get_customer_ledger
get_ar_reconciliation = BusinessAccountingService.get_ar_reconciliation
get_ap_ageing = BusinessAccountingService.get_ap_ageing
get_supplier_ledger = BusinessAccountingService.get_supplier_ledger
get_ap_reconciliation = BusinessAccountingService.get_ap_reconciliation
get_cash_bank_summary = BusinessAccountingService.get_cash_bank_summary
get_asset_register = BusinessAccountingService.get_asset_register
get_loan_subledger = BusinessAccountingService.get_loan_subledger
get_inventory_reconciliation = BusinessAccountingService.get_inventory_reconciliation


def get_chart_of_accounts(major_type=None):
    """Fetches chart of accounts via ChartOfAccountsEngine."""
    return ChartOfAccountsEngine.get_accounts(is_group=None, major_type=major_type, is_active=True)


def add_manual_entry(description, amount, payment_type, account_id, reference_no=None, notes=None, created_by="admin", entry_date=None):
    """
    Creates a clean double-entry manual accounting entry via DoubleEntryEngine.
    Validates Description, Amount (>0), Payment Type, Account Type.
    Enforces Total Debit = Total Credit and postability invariants.
    """
    try:
        val = round(float(amount), 2)
        if val <= 0:
            raise ValueError("Amount must be greater than zero.")
    except (ValueError, TypeError):
        raise ValueError("Invalid amount specified.")

    if not description or not str(description).strip():
        raise ValueError("Description is required.")

    # 1. Fetch Target Account and confirm postability
    target_account = ChartOfAccountsEngine.get_account_by_id(account_id)
    if not target_account:
        raise ValueError("Selected Account Type is invalid.")

    is_postable, err_msg, _ = ChartOfAccountsEngine.is_account_postable(account_id)
    if not is_postable:
        raise ValueError(f"Cannot post to selected account: {err_msg}")

    # 2. Determine Payment / Counter Account
    liquid_acc = AccountingRules.resolve_liquid_account(payment_type)
    payment_acc_id = liquid_acc["id"]

    major_type = target_account["major_type"]
    p_type = str(payment_type or "Cash").capitalize()
    narration = f"{description.strip()} [{p_type}]"
    if notes:
        narration += f" - {notes.strip()}"

    lines = []
    # Double-entry posting rules:
    if major_type in ("Operating Expense", "Direct Expense", "Tax", "Financial Cost"):
        # Dr Expense, Cr Cash/Bank
        lines.append({"account_id": target_account["id"], "debit": val, "credit": 0.0, "description": description.strip()})
        lines.append({"account_id": payment_acc_id, "debit": 0.0, "credit": val, "description": f"Payment via {p_type}"})

    elif major_type == "Asset":
        # Dr Asset, Cr Cash/Bank
        lines.append({"account_id": target_account["id"], "debit": val, "credit": 0.0, "description": description.strip()})
        lines.append({"account_id": payment_acc_id, "debit": 0.0, "credit": val, "description": f"Payment via {p_type}"})

    elif major_type == "Revenue":
        # Dr Cash/Bank, Cr Revenue
        lines.append({"account_id": payment_acc_id, "debit": val, "credit": 0.0, "description": f"Receipt via {p_type}"})
        lines.append({"account_id": target_account["id"], "debit": 0.0, "credit": val, "description": description.strip()})

    elif major_type == "Equity":
        if "draw" in target_account["name"].lower() or "draw" in str(target_account.get("sub_type") or "").lower():
            # Drawings: Dr Drawings, Cr Cash/Bank
            lines.append({"account_id": target_account["id"], "debit": val, "credit": 0.0, "description": description.strip()})
            lines.append({"account_id": payment_acc_id, "debit": 0.0, "credit": val, "description": f"Drawings via {p_type}"})
        else:
            # Capital: Dr Cash/Bank, Cr Capital
            lines.append({"account_id": payment_acc_id, "debit": val, "credit": 0.0, "description": f"Capital introduced via {p_type}"})
            lines.append({"account_id": target_account["id"], "debit": 0.0, "credit": val, "description": description.strip()})

    elif major_type == "Liability":
        # Repayment of Liability: Dr Liability, Cr Cash/Bank
        lines.append({"account_id": target_account["id"], "debit": val, "credit": 0.0, "description": description.strip()})
        lines.append({"account_id": payment_acc_id, "debit": 0.0, "credit": val, "description": f"Settlement via {p_type}"})

    res = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_date": entry_date or datetime.datetime.now(),
            "source_module": "manual",
            "source_entity": "manual_voucher",
            "reference_no": reference_no,
            "narration": narration,
            "status": "POSTED"
        },
        lines_data=lines,
        user=created_by
    )

    return {"entry_id": res["entry_id"], "entry_number": res["entry_number"], "status": "success"}


def get_dashboard_summary(date_filter=None):
    """
    Computes real-time executive dashboard financial indicators:
    TOTAL REVENUE
    TOTAL PURCHASES / COGS
    GROSS PROFIT = REVENUE - COGS
    OPERATING EXPENSES
    NET PROFIT = GROSS PROFIT - OPERATING EXPENSES
    CASH, BANK, RECEIVABLES, PAYABLES, INVENTORY VALUATION, GST, TDS, LOANS.
    """
    # Trigger lightweight sync check
    try:
        sync_all()
    except Exception as e:
        logger.warning(f"Background sync warning in dashboard: {e}")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    date_clause = ""
    params = []
    if date_filter:
        today = datetime.date.today()
        if date_filter == "today":
            date_clause = " AND DATE(je.entry_date) = %s"
            params.append(today)
        elif date_filter == "week":
            start_week = today - datetime.timedelta(days=today.weekday())
            date_clause = " AND DATE(je.entry_date) >= %s"
            params.append(start_week)
        elif date_filter == "month":
            start_month = today.replace(day=1)
            date_clause = " AND DATE(je.entry_date) >= %s"
            params.append(start_month)
        elif date_filter == "year":
            start_year = today.replace(month=1, day=1)
            date_clause = " AND DATE(je.entry_date) >= %s"
            params.append(start_year)

    try:
        # 1. Total Revenue (Credits to Revenue accounts minus Debits)
        query_rev = f"""
            SELECT COALESCE(SUM(jl.credit - jl.debit), 0) AS total
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.major_type = 'Revenue' AND je.status = 'POSTED' {date_clause};
        """
        cursor.execute(query_rev, tuple(params))
        total_revenue = float(cursor.fetchone()["total"])

        # 2. Total COGS / Direct Expenses (Debits minus Credits)
        query_cogs = f"""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) AS total
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.major_type = 'Direct Expense' AND je.status = 'POSTED' {date_clause};
        """
        cursor.execute(query_cogs, tuple(params))
        total_cogs = float(cursor.fetchone()["total"])

        # 3. Gross Profit
        gross_profit = total_revenue - total_cogs

        # 4. Total Operating Expenses (Debits minus Credits to Operating Expense accounts)
        query_opex = f"""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) AS total
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.major_type IN ('Operating Expense', 'Financial Cost') AND je.status = 'POSTED' {date_clause};
        """
        cursor.execute(query_opex, tuple(params))
        total_opex = float(cursor.fetchone()["total"])

        # 5. Net Profit
        net_profit = gross_profit - total_opex

        # 6. Liquid Balances (Cumulative)
        cursor.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) AS total
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code = '1010' AND je.status = 'POSTED';
        """)
        cash_balance = float(cursor.fetchone()["total"])

        cursor.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) AS total
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code IN ('1020', '1030') AND je.status = 'POSTED';
        """)
        bank_balance = float(cursor.fetchone()["total"])

        # 7. Receivables & Payables
        cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) AS total FROM accounts_receivables WHERE status != 'Paid';")
        total_receivables = float(cursor.fetchone()["total"])

        cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) AS total FROM accounts_payables WHERE status != 'Paid';")
        total_payables = float(cursor.fetchone()["total"])

        # 8. Live Inventory Valuation
        inventory_value = get_live_inventory_valuation()

        # 9. Net GST Payable (Output GST credit minus Input GST debit)
        cursor.execute("""
            SELECT 
                COALESCE(SUM(CASE WHEN ac.code = '2030' THEN jl.credit - jl.debit ELSE 0 END), 0) AS output_gst,
                COALESCE(SUM(CASE WHEN ac.code = '2040' THEN jl.debit - jl.credit ELSE 0 END), 0) AS input_gst
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE je.status = 'POSTED';
        """)
        gst_row = cursor.fetchone()
        output_gst = float(gst_row["output_gst"])
        input_gst = float(gst_row["input_gst"])
        net_gst_payable = output_gst - input_gst

        # 10. TDS Payable
        cursor.execute("""
            SELECT COALESCE(SUM(jl.credit - jl.debit), 0) AS total
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE ac.code = '2050' AND je.status = 'POSTED';
        """)
        tds_payable = float(cursor.fetchone()["total"])

        # 11. Loans Outstanding
        cursor.execute("SELECT COALESCE(SUM(outstanding_balance), 0) AS total FROM accounts_liabilities WHERE status = 'Active';")
        loans_outstanding = float(cursor.fetchone()["total"])

        # 12. Recent Accounting Transactions
        cursor.execute("""
            SELECT je.id, je.entry_number, je.entry_date, je.source_module, je.reference_no, je.narration,
                   SUM(jl.debit) AS total_amount, ac.name AS primary_account, ac.major_type
            FROM journal_entries je
            JOIN journal_lines jl ON je.id = jl.entry_id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE je.status = 'POSTED'
            GROUP BY je.id, je.entry_number, je.entry_date, je.source_module, je.reference_no, je.narration, ac.name, ac.major_type
            ORDER BY je.entry_date DESC, je.id DESC
            LIMIT 10;
        """)
        recent_txns = cursor.fetchall()
        for t in recent_txns:
            t["total_amount"] = float(t["total_amount"])
            if isinstance(t["entry_date"], datetime.datetime):
                t["entry_date"] = t["entry_date"].strftime("%Y-%m-%d %H:%M")
            else:
                t["entry_date"] = str(t["entry_date"])

        return {
            "total_revenue": total_revenue,
            "total_cogs": total_cogs,
            "gross_profit": gross_profit,
            "total_opex": total_opex,
            "net_profit": net_profit,
            "cash_balance": cash_balance,
            "bank_balance": bank_balance,
            "total_receivables": total_receivables,
            "total_payables": total_payables,
            "inventory_value": inventory_value,
            "output_gst": output_gst,
            "input_gst": input_gst,
            "net_gst_payable": net_gst_payable,
            "tds_payable": tds_payable,
            "loans_outstanding": loans_outstanding,
            "recent_txns": recent_txns
        }

    finally:
        cursor.close()
        conn.close()


def get_transactions_list(filters=None):
    """Fetches list of journal transactions powered by LedgerEngine."""
    txns = LedgerEngine.get_transactions(filters=filters)
    for t in txns:
        t["amount"] = t.get("total_debit", 0.0)
        t["debit_account"] = t.get("primary_debit_account")
        t["credit_account"] = t.get("primary_credit_account")
    return txns


def get_general_ledger(account_id=None, start_date=None, end_date=None):
    """
    Detailed Account Ledger with running balance powered by LedgerEngine.
    """
    statement = LedgerEngine.get_ledger_statement(
        account_id=account_id if account_id else None,
        start_date=start_date if start_date else None,
        end_date=end_date if end_date else None
    )
    return statement["movements"]


def get_receivables(status_filter=None):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if status_filter:
            cursor.execute("SELECT * FROM accounts_receivables WHERE status = %s ORDER BY due_date ASC, id DESC;", (status_filter,))
        else:
            cursor.execute("SELECT * FROM accounts_receivables ORDER BY id DESC;")
        rows = cursor.fetchall()
        for r in rows:
            r["total_amount"] = float(r["total_amount"])
            r["paid_amount"] = float(r["paid_amount"])
            r["remaining_balance"] = float(r["remaining_balance"])
            if r.get("invoice_date") and isinstance(r["invoice_date"], (datetime.date, datetime.datetime)):
                r["invoice_date"] = r["invoice_date"].strftime("%Y-%m-%d")
            if r.get("due_date") and isinstance(r["due_date"], (datetime.date, datetime.datetime)):
                r["due_date"] = r["due_date"].strftime("%Y-%m-%d")
        return rows
    finally:
        cursor.close()
        conn.close()


def record_receivable_payment(receivable_id, amount_paid, payment_method="Bank Transfer", reference_no=None, notes=None, created_by="admin"):
    val = float(amount_paid)
    if val <= 0:
        raise ValueError("Payment amount must be greater than zero.")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM accounts_receivables WHERE id = %s;", (receivable_id,))
        rec = cursor.fetchone()
        if not rec:
            raise ValueError("Receivable record not found.")

        rem = float(rec["remaining_balance"])
        if val > rem:
            raise ValueError(f"Amount exceeds outstanding balance (₹{rem:,.2f}).")

        new_paid = float(rec["paid_amount"]) + val
        new_rem = rem - val
        new_status = "Paid" if new_rem <= 0.01 else "Partial"

        cursor.execute("""
            UPDATE accounts_receivables
            SET paid_amount = %s, remaining_balance = %s, status = %s
            WHERE id = %s;
        """, (new_paid, new_rem, new_status, receivable_id))

        cursor.execute("""
            INSERT INTO accounts_receivable_payments
                (receivable_id, payment_date, amount_paid, payment_method, reference_no, notes, created_by)
            VALUES
                (%s, CURRENT_TIMESTAMP, %s, %s, %s, %s, %s);
        """, (receivable_id, val, payment_method, reference_no, notes, created_by))

        # Create double-entry journal via DoubleEntryEngine: Dr Bank/Cash, Cr Accounts Receivable
        liquid_acc = AccountingRules.resolve_liquid_account(payment_method, conn=conn)
        ar_acc = AccountingRules.get_mapped_account("accounts_receivable", default_code="1040", conn=conn)
        lines = [
            {
                "account_id": liquid_acc["id"],
                "debit": val,
                "credit": 0.0,
                "description": f"Receipt for {rec['customer_name']} (Inv #{rec['invoice_ref']})",
                "party_type": "customer",
                "party_id": str(rec.get("source_bill_id") or rec["id"]),
                "party_name": rec["customer_name"],
            },
            {
                "account_id": ar_acc["id"],
                "debit": 0.0,
                "credit": val,
                "description": f"Receivable settlement for {rec['customer_name']}",
                "party_type": "customer",
                "party_id": str(rec.get("source_bill_id") or rec["id"]),
                "party_name": rec["customer_name"],
            }
        ]
        jv_res = DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": datetime.datetime.now(),
                "source_module": "sales",
                "source_entity": "receivable_receipt",
                "source_id": str(receivable_id),
                "reference_no": reference_no or f"REC-{rec['invoice_ref']}",
                "narration": f"Payment received from {rec['customer_name']} (Inv #{rec['invoice_ref']})",
                "status": "POSTED"
            },
            lines_data=lines,
            user=created_by,
            external_conn=conn
        )

        conn.commit()
        return {"status": "success", "remaining_balance": new_rem, "journal_id": jv_res.get("entry_id")}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def get_payables(status_filter=None):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if status_filter:
            cursor.execute("SELECT * FROM accounts_payables WHERE status = %s ORDER BY id DESC;", (status_filter,))
        else:
            cursor.execute("SELECT * FROM accounts_payables ORDER BY id DESC;")
        rows = cursor.fetchall()
        for r in rows:
            r["total_amount"] = float(r["total_amount"])
            r["paid_amount"] = float(r["paid_amount"])
            r["remaining_balance"] = float(r["remaining_balance"])
            if r.get("invoice_date") and isinstance(r["invoice_date"], (datetime.date, datetime.datetime)):
                r["invoice_date"] = r["invoice_date"].strftime("%Y-%m-%d")
            if r.get("due_date") and isinstance(r["due_date"], (datetime.date, datetime.datetime)):
                r["due_date"] = r["due_date"].strftime("%Y-%m-%d")
        return rows
    finally:
        cursor.close()
        conn.close()


def record_payable_payment(payable_id, amount_paid, payment_method="Bank Transfer", reference_no=None, notes=None, created_by="admin"):
    val = float(amount_paid)
    if val <= 0:
        raise ValueError("Payment amount must be greater than zero.")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM accounts_payables WHERE id = %s;", (payable_id,))
        pay = cursor.fetchone()
        if not pay:
            raise ValueError("Payable record not found.")

        rem = float(pay["remaining_balance"])
        if val > rem:
            raise ValueError(f"Amount exceeds payable balance (₹{rem:,.2f}).")

        new_paid = float(pay["paid_amount"]) + val
        new_rem = rem - val
        new_status = "Paid" if new_rem <= 0.01 else "Partial"

        cursor.execute("""
            UPDATE accounts_payables
            SET paid_amount = %s, remaining_balance = %s, status = %s
            WHERE id = %s;
        """, (new_paid, new_rem, new_status, payable_id))

        cursor.execute("""
            INSERT INTO accounts_payable_payments
                (payable_id, payment_date, amount_paid, payment_method, reference_no, notes, created_by)
            VALUES
                (%s, CURRENT_TIMESTAMP, %s, %s, %s, %s, %s);
        """, (payable_id, val, payment_method, reference_no, notes, created_by))

        # Create double-entry journal via DoubleEntryEngine: Dr Accounts Payable, Cr Bank/Cash
        liquid_acc = AccountingRules.resolve_liquid_account(payment_method, conn=conn)
        ap_acc = AccountingRules.get_mapped_account("accounts_payable", default_code="2010", conn=conn)
        lines = [
            {
                "account_id": ap_acc["id"],
                "debit": val,
                "credit": 0.0,
                "description": f"Disbursement to {pay['supplier_name']} (Inv #{pay['invoice_ref']})",
                "party_type": "supplier",
                "party_name": pay["supplier_name"],
            },
            {
                "account_id": liquid_acc["id"],
                "debit": 0.0,
                "credit": val,
                "description": f"Payment disbursement via {payment_method}",
                "party_type": "supplier",
                "party_name": pay["supplier_name"],
            }
        ]
        jv_res = DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": datetime.datetime.now(),
                "source_module": "inventory",
                "source_entity": "payable_disbursement",
                "source_id": str(payable_id),
                "reference_no": reference_no or f"PAY-{pay['invoice_ref']}",
                "narration": f"Disbursement to {pay['supplier_name']} (Inv #{pay['invoice_ref']})",
                "status": "POSTED"
            },
            lines_data=lines,
            user=created_by,
            external_conn=conn
        )

        conn.commit()
        return {"status": "success", "remaining_balance": new_rem, "journal_id": jv_res.get("entry_id")}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def get_fixed_assets():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM accounts_fixed_assets ORDER BY id DESC;")
        rows = cursor.fetchall()
        for r in rows:
            r["purchase_value"] = float(r["purchase_value"])
            r["current_value"] = float(r["current_value"])
            r["accumulated_depreciation"] = float(r["accumulated_depreciation"])
            if r.get("purchase_date"):
                r["purchase_date"] = str(r["purchase_date"])
        return rows
    finally:
        cursor.close()
        conn.close()


def create_fixed_asset(asset_name, category, purchase_date, purchase_value, useful_life_years=5, supplier=None, payment_method="Bank Transfer", notes=None):
    val = float(purchase_value)
    if val <= 0:
        raise ValueError("Purchase value must be greater than zero.")
    code = generate_unique_number("AST", "accounts_fixed_assets", "asset_code")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            INSERT INTO accounts_fixed_assets
                (asset_code, asset_name, category, purchase_date, purchase_value, current_value, useful_life_years, supplier, payment_method, notes)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (code, asset_name, category, purchase_date, val, val, useful_life_years, supplier, payment_method, notes))
        asset_id = cursor.lastrowid

        # Post journal entry via DoubleEntryEngine: Dr Fixed Asset, Cr Bank/Cash
        target_code = "1130" if category in ("Machinery", "Plant") else ("1150" if category == "Vehicles" else "1140")
        fa_acc = AccountingRules.get_mapped_account(f"fixed_asset_{category.lower()}", default_code=target_code, conn=conn)
        liquid_acc = AccountingRules.resolve_liquid_account(payment_method, conn=conn)

        lines = [
            {
                "account_id": fa_acc["id"],
                "debit": val,
                "credit": 0.0,
                "description": f"Asset Purchase: {asset_name} ({category})",
                "party_type": "supplier",
                "party_name": supplier or "Vendor",
            },
            {
                "account_id": liquid_acc["id"],
                "debit": 0.0,
                "credit": val,
                "description": f"Payment via {payment_method}",
                "party_type": "supplier",
                "party_name": supplier or "Vendor",
            }
        ]
        DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": purchase_date,
                "source_module": "asset",
                "source_entity": "asset_purchase",
                "source_id": str(asset_id),
                "reference_no": code,
                "narration": f"Asset Purchase: {asset_name} ({category})",
                "status": "POSTED"
            },
            lines_data=lines,
            user="admin",
            external_conn=conn
        )

        conn.commit()
        return {"asset_id": asset_id, "asset_code": code}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def get_liabilities_list():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM accounts_liabilities ORDER BY id DESC;")
        rows = cursor.fetchall()
        for r in rows:
            r["principal_amount"] = float(r["principal_amount"])
            r["outstanding_balance"] = float(r["outstanding_balance"])
            r["interest_rate"] = float(r["interest_rate"])
            if r.get("start_date"):
                r["start_date"] = str(r["start_date"])
        return rows
    finally:
        cursor.close()
        conn.close()


def create_liability(title, liability_type, principal_amount, interest_rate=0.0, tenure_months=0, lender=None, start_date=None, notes=None):
    val = float(principal_amount)
    if val <= 0:
        raise ValueError("Principal amount must be greater than zero.")
    code = generate_unique_number("LIA", "accounts_liabilities", "liability_code")
    start_date = start_date or datetime.date.today().strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            INSERT INTO accounts_liabilities
                (liability_code, title, liability_type, principal_amount, interest_rate, tenure_months, outstanding_balance, lender, start_date, notes)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (code, title, liability_type, val, float(interest_rate), int(tenure_months), val, lender, start_date, notes))
        lia_id = cursor.lastrowid

        # Loan Inflow via DoubleEntryEngine: Dr Bank, Cr Loan Liability
        bank_acc = AccountingRules.get_mapped_account("bank_default", default_code="1020", conn=conn)
        target_code = "2110" if "bank" in str(liability_type or "").lower() else "2120"
        lia_acc = AccountingRules.get_mapped_account("loan_default", default_code=target_code, conn=conn)

        lines = [
            {
                "account_id": bank_acc["id"],
                "debit": val,
                "credit": 0.0,
                "description": f"Loan proceeds received for {title}",
                "party_type": "lender",
                "party_name": lender or "Lender",
            },
            {
                "account_id": lia_acc["id"],
                "debit": 0.0,
                "credit": val,
                "description": f"Loan liability recorded: {code} [{liability_type}]",
                "party_type": "lender",
                "party_name": lender or "Lender",
            }
        ]
        DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": start_date,
                "source_module": "liability",
                "source_entity": "liability_booking",
                "source_id": str(lia_id),
                "reference_no": code,
                "narration": f"Loan Received: {title} from {lender or 'Lender'}",
                "status": "POSTED"
            },
            lines_data=lines,
            user="admin",
            external_conn=conn
        )

        conn.commit()
        return {"liability_id": lia_id, "liability_code": code}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def _get_account_id(cursor, code):
    cursor.execute("SELECT id FROM accounts_chart WHERE code = %s;", (code,))
    r = cursor.fetchone()
    return r["id"] if isinstance(r, dict) else r[0]


def purge_all_transaction_data():
    """Purge all journal lines, journal entries, sync registry, and balances."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("DELETE FROM journal_lines;")
        cursor.execute("DELETE FROM journal_entries;")
        cursor.execute("DELETE FROM accounting_sync_registry;")
        cursor.execute("DELETE FROM accounts_receivables;")
        cursor.execute("DELETE FROM accounts_payables;")
        conn.commit()
        return {"status": "success", "message": "All transaction data purged successfully."}
    finally:
        cursor.close()
        conn.close()


# -------------------------------------------------------------------------
# BUSINESS ACCOUNTING SERVICE DELEGATES
# -------------------------------------------------------------------------
from backend.business_accounting import BusinessAccountingService

get_ar_ageing = BusinessAccountingService.get_ar_ageing
get_customer_ledger = BusinessAccountingService.get_customer_ledger
get_ar_reconciliation = BusinessAccountingService.get_ar_reconciliation
get_ap_ageing = BusinessAccountingService.get_ap_ageing
get_supplier_ledger = BusinessAccountingService.get_supplier_ledger
get_ap_reconciliation = BusinessAccountingService.get_ap_reconciliation
get_cash_bank_summary = BusinessAccountingService.get_cash_bank_summary
get_asset_register = BusinessAccountingService.get_asset_register
get_loan_subledger = BusinessAccountingService.get_loan_subledger
get_inventory_reconciliation = BusinessAccountingService.get_inventory_reconciliation

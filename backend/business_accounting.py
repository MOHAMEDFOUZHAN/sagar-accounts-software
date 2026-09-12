import datetime
import logging
from decimal import Decimal
from backend.db import get_db_connection
from backend.coa_engine import ChartOfAccountsEngine
from backend.ledger_engine import LedgerEngine
from backend.sync_engine import get_live_inventory_valuation

logger = logging.getLogger(__name__)


class BusinessAccountingService:
    """
    Unified Business Accounting Query & Subledger Control Service.
    Connects operational business entities directly to the core double-entry accounting engine.
    Ensures mathematical equality between subledgers and general ledger control accounts:
      - Customer Subledger == Accounts Receivable Control (1040)
      - Supplier Subledger == Accounts Payable Control (2010)
      - Fixed Asset Register == Net Fixed Assets (1110-1160)
      - Loan Subledger == Loan Liability (2110 + 2120)
      - Inventory Storage Valuation == Inventory Control (1050 + 1060)
    """

    # -------------------------------------------------------------------------
    # 1. ACCOUNTS RECEIVABLE (AR) SUBLEDGER & AGEING
    # -------------------------------------------------------------------------
    @classmethod
    def get_ar_ageing(cls, as_of_date=None, conn=None):
        """
        Computes 5-bucket Accounts Receivable ageing:
          - Current (Not yet past due)
          - 1–30 Days Overdue
          - 31–60 Days Overdue
          - 61–90 Days Overdue
          - 90+ Days Overdue
        Classifies by transaction due date (or invoice date if due date is not specified).
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            ref_date = datetime.date.today()
            if as_of_date:
                if isinstance(as_of_date, str):
                    ref_date = datetime.datetime.strptime(as_of_date.split("T")[0].split(" ")[0], "%Y-%m-%d").date()
                elif isinstance(as_of_date, datetime.date):
                    ref_date = as_of_date

            cur.execute("""
                SELECT id, receivable_no, invoice_ref, customer_name, contact_phone,
                       invoice_date, due_date, total_amount, paid_amount, remaining_balance, status
                FROM accounts_receivables
                WHERE remaining_balance > 0 AND status != 'Paid'
                ORDER BY due_date ASC, id ASC;
            """)
            rows = cur.fetchall()

            customers_map = {}
            totals = {
                "current": 0.0,
                "days_1_30": 0.0,
                "days_31_60": 0.0,
                "days_61_90": 0.0,
                "days_90_plus": 0.0,
                "total_outstanding": 0.0,
                "count": len(rows)
            }

            for r in rows:
                bal = round(float(r["remaining_balance"]), 2)
                c_name = str(r["customer_name"] or "General Customer").strip()

                # Determine effective due date
                d_date = r.get("due_date") or r.get("invoice_date")
                if isinstance(d_date, str):
                    try:
                        d_date = datetime.datetime.strptime(d_date.split("T")[0].split(" ")[0], "%Y-%m-%d").date()
                    except Exception:
                        d_date = ref_date
                elif isinstance(d_date, datetime.datetime):
                    d_date = d_date.date()
                elif not d_date:
                    d_date = ref_date

                days_overdue = (ref_date - d_date).days

                bucket = "current"
                if days_overdue <= 0:
                    bucket = "current"
                elif 1 <= days_overdue <= 30:
                    bucket = "days_1_30"
                elif 31 <= days_overdue <= 60:
                    bucket = "days_31_60"
                elif 61 <= days_overdue <= 90:
                    bucket = "days_61_90"
                else:
                    bucket = "days_90_plus"

                totals[bucket] = round(totals[bucket] + bal, 2)
                totals["total_outstanding"] = round(totals["total_outstanding"] + bal, 2)

                if c_name not in customers_map:
                    customers_map[c_name] = {
                        "customer_name": c_name,
                        "contact_phone": r.get("contact_phone") or "",
                        "current": 0.0,
                        "days_1_30": 0.0,
                        "days_31_60": 0.0,
                        "days_61_90": 0.0,
                        "days_90_plus": 0.0,
                        "total": 0.0,
                        "invoice_count": 0,
                        "invoices": []
                    }

                cust_entry = customers_map[c_name]
                cust_entry[bucket] = round(cust_entry[bucket] + bal, 2)
                cust_entry["total"] = round(cust_entry["total"] + bal, 2)
                cust_entry["invoice_count"] += 1
                cust_entry["invoices"].append({
                    "id": r["id"],
                    "invoice_ref": r["invoice_ref"],
                    "invoice_date": str(r["invoice_date"]).split("T")[0],
                    "due_date": str(r["due_date"]).split("T")[0] if r.get("due_date") else "",
                    "total_amount": float(r["total_amount"]),
                    "paid_amount": float(r["paid_amount"]),
                    "remaining_balance": bal,
                    "days_overdue": days_overdue,
                    "bucket": bucket
                })

            customer_list = sorted(list(customers_map.values()), key=lambda x: x["total"], reverse=True)

            return {
                "as_of_date": ref_date.isoformat(),
                "totals": totals,
                "customers": customer_list,
                "customer_count": len(customer_list)
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_customer_ledger(cls, customer_name, start_date=None, end_date=None, conn=None):
        """
        Customer Account Statement showing line-by-line debit (invoices) and credit (receipts/credit notes)
        movements with accurate running balance.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            date_cond = ""
            params = [f"%{customer_name.strip()}%"]
            if start_date:
                date_cond += " AND DATE(je.entry_date) >= %s"
                params.append(start_date)
            if end_date:
                date_cond += " AND DATE(je.entry_date) <= %s"
                params.append(end_date)

            cur.execute(f"""
                SELECT jl.id as line_id, je.id as entry_id, je.entry_number, je.entry_date,
                       je.source_module, je.source_entity, je.reference_no, je.narration,
                       jl.debit, jl.credit, jl.description, ac.code as account_code, ac.name as account_name
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE (jl.party_name LIKE %s OR je.narration LIKE %s)
                  AND je.status = 'POSTED'
                  {date_cond}
                ORDER BY je.entry_date ASC, je.id ASC, jl.id ASC;
            """, (params[0], params[0], *params[1:]))
            rows = cur.fetchall()

            movements = []
            running_bal = 0.0
            total_invoiced = 0.0
            total_collected = 0.0

            for r in rows:
                dr = round(float(r["debit"]), 2)
                cr = round(float(r["credit"]), 2)
                # Customer receivable: Debits increase balance, Credits reduce balance
                net_change = round(dr - cr, 2)
                running_bal = round(running_bal + net_change, 2)

                if dr > 0:
                    total_invoiced = round(total_invoiced + dr, 2)
                if cr > 0:
                    total_collected = round(total_collected + cr, 2)

                d_str = r["entry_date"].strftime("%Y-%m-%d %H:%M") if isinstance(r["entry_date"], datetime.datetime) else str(r["entry_date"])
                movements.append({
                    "line_id": r["line_id"],
                    "entry_id": r["entry_id"],
                    "entry_number": r["entry_number"],
                    "date": d_str,
                    "reference": r["reference_no"] or r["entry_number"],
                    "account": f"{r['account_code']} - {r['account_name']}",
                    "description": r["description"] or r["narration"],
                    "debit": dr,
                    "credit": cr,
                    "running_balance": running_bal
                })

            return {
                "customer_name": customer_name,
                "start_date": start_date,
                "end_date": end_date,
                "total_invoiced": total_invoiced,
                "total_collected": total_collected,
                "closing_balance": running_bal,
                "movements": movements,
                "count": len(movements)
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_ar_reconciliation(cls, as_of_date=None, conn=None):
        """
        Reconciles Customer AR Subledger (accounts_receivables remaining balances)
        against General Ledger Control Account 1040 (Accounts Receivable).
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            # 1. Subledger Total
            date_cond = "AND DATE(invoice_date) <= %s" if as_of_date else ""
            params = (as_of_date,) if as_of_date else ()
            cur.execute(f"""
                SELECT COALESCE(SUM(remaining_balance), 0.0) AS total
                FROM accounts_receivables
                WHERE status != 'Paid' {date_cond};
            """, params)
            sub_total = round(float(cur.fetchone()["total"]), 2)

            # 2. GL Control Account 1040
            ar_acc = ChartOfAccountsEngine.get_account_by_code("1040", conn=conn)
            if not ar_acc:
                return {"status": "ERROR", "message": "Account 1040 not found"}

            bal_data = LedgerEngine.get_account_balance(ar_acc["id"], as_of_date=as_of_date, conn=conn)
            gl_total = round(float(bal_data["balance"]), 2)

            diff = round(sub_total - gl_total, 2)
            is_reconciled = (abs(diff) < 0.01)

            return {
                "control_account": "1040 - Accounts Receivable",
                "subledger_total": sub_total,
                "gl_control_balance": gl_total,
                "difference": diff,
                "is_reconciled": is_reconciled,
                "status": "RECONCILED" if is_reconciled else "DISCREPANCY"
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 2. ACCOUNTS PAYABLE (AP) SUBLEDGER & AGEING
    # -------------------------------------------------------------------------
    @classmethod
    def get_ap_ageing(cls, as_of_date=None, conn=None):
        """
        Computes 5-bucket Accounts Payable ageing for suppliers:
          - Current, 1–30 Days, 31–60 Days, 61–90 Days, 90+ Days
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            ref_date = datetime.date.today()
            if as_of_date:
                if isinstance(as_of_date, str):
                    ref_date = datetime.datetime.strptime(as_of_date.split("T")[0].split(" ")[0], "%Y-%m-%d").date()
                elif isinstance(as_of_date, datetime.date):
                    ref_date = as_of_date

            cur.execute("""
                SELECT id, payable_no, invoice_ref, supplier_name, contact_phone,
                       invoice_date, due_date, total_amount, paid_amount, remaining_balance, status
                FROM accounts_payables
                WHERE remaining_balance > 0 AND status != 'Paid'
                ORDER BY due_date ASC, id ASC;
            """)
            rows = cur.fetchall()

            suppliers_map = {}
            totals = {
                "current": 0.0,
                "days_1_30": 0.0,
                "days_31_60": 0.0,
                "days_61_90": 0.0,
                "days_90_plus": 0.0,
                "total_outstanding": 0.0,
                "count": len(rows)
            }

            for r in rows:
                bal = round(float(r["remaining_balance"]), 2)
                s_name = str(r["supplier_name"] or "Vendor").strip()

                d_date = r.get("due_date") or r.get("invoice_date")
                if isinstance(d_date, str):
                    try:
                        d_date = datetime.datetime.strptime(d_date.split("T")[0].split(" ")[0], "%Y-%m-%d").date()
                    except Exception:
                        d_date = ref_date
                elif isinstance(d_date, datetime.datetime):
                    d_date = d_date.date()
                elif not d_date:
                    d_date = ref_date

                days_overdue = (ref_date - d_date).days

                bucket = "current"
                if days_overdue <= 0:
                    bucket = "current"
                elif 1 <= days_overdue <= 30:
                    bucket = "days_1_30"
                elif 31 <= days_overdue <= 60:
                    bucket = "days_31_60"
                elif 61 <= days_overdue <= 90:
                    bucket = "days_61_90"
                else:
                    bucket = "days_90_plus"

                totals[bucket] = round(totals[bucket] + bal, 2)
                totals["total_outstanding"] = round(totals["total_outstanding"] + bal, 2)

                if s_name not in suppliers_map:
                    suppliers_map[s_name] = {
                        "supplier_name": s_name,
                        "contact_phone": r.get("contact_phone") or "",
                        "current": 0.0,
                        "days_1_30": 0.0,
                        "days_31_60": 0.0,
                        "days_61_90": 0.0,
                        "days_90_plus": 0.0,
                        "total": 0.0,
                        "bill_count": 0,
                        "bills": []
                    }

                supp_entry = suppliers_map[s_name]
                supp_entry[bucket] = round(supp_entry[bucket] + bal, 2)
                supp_entry["total"] = round(supp_entry["total"] + bal, 2)
                supp_entry["bill_count"] += 1
                supp_entry["bills"].append({
                    "id": r["id"],
                    "invoice_ref": r["invoice_ref"],
                    "invoice_date": str(r["invoice_date"]).split("T")[0],
                    "due_date": str(r["due_date"]).split("T")[0] if r.get("due_date") else "",
                    "total_amount": float(r["total_amount"]),
                    "paid_amount": float(r["paid_amount"]),
                    "remaining_balance": bal,
                    "days_overdue": days_overdue,
                    "bucket": bucket
                })

            supplier_list = sorted(list(suppliers_map.values()), key=lambda x: x["total"], reverse=True)

            return {
                "as_of_date": ref_date.isoformat(),
                "totals": totals,
                "suppliers": supplier_list,
                "supplier_count": len(supplier_list)
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_supplier_ledger(cls, supplier_name, start_date=None, end_date=None, conn=None):
        """
        Supplier Account Statement showing inward purchases (credits) and payments/debit notes (debits).
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            date_cond = ""
            params = [f"%{supplier_name.strip()}%"]
            if start_date:
                date_cond += " AND DATE(je.entry_date) >= %s"
                params.append(start_date)
            if end_date:
                date_cond += " AND DATE(je.entry_date) <= %s"
                params.append(end_date)

            cur.execute(f"""
                SELECT jl.id as line_id, je.id as entry_id, je.entry_number, je.entry_date,
                       je.source_module, je.source_entity, je.reference_no, je.narration,
                       jl.debit, jl.credit, jl.description, ac.code as account_code, ac.name as account_name
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE (jl.party_name LIKE %s OR je.narration LIKE %s)
                  AND je.status = 'POSTED'
                  {date_cond}
                ORDER BY je.entry_date ASC, je.id ASC, jl.id ASC;
            """, (params[0], params[0], *params[1:]))
            rows = cur.fetchall()

            movements = []
            running_bal = 0.0
            total_billed = 0.0
            total_disbursed = 0.0

            for r in rows:
                dr = round(float(r["debit"]), 2)
                cr = round(float(r["credit"]), 2)
                # Trade payable: Credits increase liability, Debits reduce liability
                net_change = round(cr - dr, 2)
                running_bal = round(running_bal + net_change, 2)

                if cr > 0:
                    total_billed = round(total_billed + cr, 2)
                if dr > 0:
                    total_disbursed = round(total_disbursed + dr, 2)

                d_str = r["entry_date"].strftime("%Y-%m-%d %H:%M") if isinstance(r["entry_date"], datetime.datetime) else str(r["entry_date"])
                movements.append({
                    "line_id": r["line_id"],
                    "entry_id": r["entry_id"],
                    "entry_number": r["entry_number"],
                    "date": d_str,
                    "reference": r["reference_no"] or r["entry_number"],
                    "account": f"{r['account_code']} - {r['account_name']}",
                    "description": r["description"] or r["narration"],
                    "debit": dr,
                    "credit": cr,
                    "running_balance": running_bal
                })

            return {
                "supplier_name": supplier_name,
                "start_date": start_date,
                "end_date": end_date,
                "total_billed": total_billed,
                "total_disbursed": total_disbursed,
                "closing_balance": running_bal,
                "movements": movements,
                "count": len(movements)
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_ap_reconciliation(cls, as_of_date=None, conn=None):
        """
        Reconciles Supplier AP Subledger (accounts_payables remaining balances)
        against General Ledger Control Account 2010 (Accounts Payable).
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            date_cond = "AND DATE(invoice_date) <= %s" if as_of_date else ""
            params = (as_of_date,) if as_of_date else ()
            cur.execute(f"""
                SELECT COALESCE(SUM(remaining_balance), 0.0) AS total
                FROM accounts_payables
                WHERE status != 'Paid' {date_cond};
            """, params)
            sub_total = round(float(cur.fetchone()["total"]), 2)

            ap_acc = ChartOfAccountsEngine.get_account_by_code("2010", conn=conn)
            if not ap_acc:
                return {"status": "ERROR", "message": "Account 2010 not found"}

            bal_data = LedgerEngine.get_account_balance(ap_acc["id"], as_of_date=as_of_date, conn=conn)
            gl_total = round(float(bal_data["balance"]), 2)

            diff = round(sub_total - gl_total, 2)
            is_reconciled = (abs(diff) < 0.01)

            return {
                "control_account": "2010 - Accounts Payable",
                "subledger_total": sub_total,
                "gl_control_balance": gl_total,
                "difference": diff,
                "is_reconciled": is_reconciled,
                "status": "RECONCILED" if is_reconciled else "DISCREPANCY"
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 3. CASH & BANK ACCOUNTING (BALANCES & MOVEMENTS)
    # -------------------------------------------------------------------------
    @classmethod
    def get_cash_bank_summary(cls, as_of_date=None, conn=None):
        """
        Fetches live double-entry GL balances for:
          - 1010: Cash on Hand
          - 1020: Bank Current Account
          - 1030: UPI & Digital Clearing
        Also lists recent contra cash/bank transfers.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        try:
            acc_1010 = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
            acc_1020 = ChartOfAccountsEngine.get_account_by_code("1020", conn=conn)
            acc_1030 = ChartOfAccountsEngine.get_account_by_code("1030", conn=conn)

            bal_1010 = round(float(LedgerEngine.get_account_balance(acc_1010["id"], as_of_date=as_of_date, conn=conn)["balance"]) if acc_1010 else 0.0, 2)
            bal_1020 = round(float(LedgerEngine.get_account_balance(acc_1020["id"], as_of_date=as_of_date, conn=conn)["balance"]) if acc_1020 else 0.0, 2)
            bal_1030 = round(float(LedgerEngine.get_account_balance(acc_1030["id"], as_of_date=as_of_date, conn=conn)["balance"]) if acc_1030 else 0.0, 2)
            total_liquid = round(bal_1010 + bal_1020 + bal_1030, 2)

            # Recent contra transfers
            cur = conn.cursor(dictionary=True)
            cur.execute("""
                SELECT je.id, je.entry_number, je.entry_date, je.reference_no, je.narration,
                       SUM(jl.debit) as amount
                FROM journal_entries je
                JOIN journal_lines jl ON je.id = jl.entry_id
                WHERE je.source_entity = 'contra_transfer' AND je.status = 'POSTED'
                GROUP BY je.id, je.entry_number, je.entry_date, je.reference_no, je.narration
                ORDER BY je.entry_date DESC, je.id DESC LIMIT 10;
            """)
            recent_transfers = cur.fetchall()
            for t in recent_transfers:
                t["amount"] = float(t["amount"])
                t["date"] = t["entry_date"].strftime("%Y-%m-%d") if isinstance(t["entry_date"], (datetime.date, datetime.datetime)) else str(t["entry_date"])

            return {
                "as_of_date": as_of_date or datetime.date.today().isoformat(),
                "cash_balance": bal_1010,
                "bank_balance": bal_1020,
                "upi_balance": bal_1030,
                "total_liquid_assets": total_liquid,
                "recent_transfers": recent_transfers
            }
        finally:
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 4. FIXED ASSET REGISTER & NET BOOK VALUE
    # -------------------------------------------------------------------------
    @classmethod
    def get_asset_register(cls, as_of_date=None, conn=None):
        """
        Returns full Fixed Asset Register with:
          - Purchase Cost
          - Accumulated Depreciation
          - Net Book Value (NBV = Cost - Depreciation)
          - Current Status
        And reconciles Asset Register NBV against GL Fixed Assets minus Accumulated Depreciation.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT id, asset_code, asset_name, category, purchase_date, purchase_value,
                       current_value, useful_life_years, depreciation_rate, accumulated_depreciation,
                       payment_method, supplier, status, disposal_date, disposal_proceeds, disposal_gain_loss, notes
                FROM accounts_fixed_assets
                ORDER BY id ASC;
            """)
            assets = cur.fetchall()

            total_cost = 0.0
            total_depr = 0.0
            total_nbv = 0.0

            for a in assets:
                cost = round(float(a["purchase_value"]), 2)
                depr = round(float(a["accumulated_depreciation"]), 2)
                nbv = round(cost - depr, 2) if a["status"] != "Disposed" else 0.0
                a["purchase_value"] = cost
                a["accumulated_depreciation"] = depr
                a["net_book_value"] = nbv
                a["purchase_date"] = str(a["purchase_date"]).split("T")[0] if a.get("purchase_date") else ""

                if a["status"] != "Disposed":
                    total_cost = round(total_cost + cost, 2)
                    total_depr = round(total_depr + depr, 2)
                    total_nbv = round(total_nbv + nbv, 2)

            # GL Control Accounts: Gross Fixed Assets (1110-1155) and Accumulated Depreciation (1160)
            cur.execute("""
                SELECT 
                    COALESCE(SUM(CASE WHEN ac.code LIKE '11%' AND ac.code != '1160' THEN jl.debit - jl.credit ELSE 0 END), 0.0) as gl_fixed_assets,
                    COALESCE(SUM(CASE WHEN ac.code = '1160' THEN jl.credit - jl.debit ELSE 0 END), 0.0) as gl_accum_depr
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE je.status = 'POSTED';
            """)
            gl_row = cur.fetchone()
            gl_cost = round(float(gl_row["gl_fixed_assets"]), 2)
            gl_depr = round(float(gl_row["gl_accum_depr"]), 2)
            gl_nbv = round(gl_cost - gl_depr, 2)

            diff = round(total_nbv - gl_nbv, 2)
            is_reconciled = (abs(diff) < 0.01)

            return {
                "assets": assets,
                "asset_count": len(assets),
                "total_cost": total_cost,
                "total_accumulated_depreciation": total_depr,
                "total_net_book_value": total_nbv,
                "gl_fixed_assets_cost": gl_cost,
                "gl_accumulated_depreciation": gl_depr,
                "gl_net_book_value": gl_nbv,
                "difference": diff,
                "is_reconciled": is_reconciled,
                "status": "RECONCILED" if is_reconciled else "DISCREPANCY"
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 5. LOAN SUBLEDGER & RECONCILIATION
    # -------------------------------------------------------------------------
    @classmethod
    def get_loan_subledger(cls, as_of_date=None, conn=None):
        """
        Returns full Loan & Liability Register with:
          - Original Principal
          - Total Principal Repaid
          - Total Interest Paid
          - Current Outstanding Principal
        And reconciles Loan Subledger Outstanding Principal against GL Loan Accounts (2110 + 2120).
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT id, liability_code, title, liability_type, principal_amount,
                       interest_rate, tenure_months, outstanding_balance, lender, start_date, status, notes
                FROM accounts_liabilities
                ORDER BY id ASC;
            """)
            loans = cur.fetchall()

            total_principal = 0.0
            total_repaid = 0.0
            total_interest = 0.0
            total_outstanding = 0.0

            for l in loans:
                p_amt = round(float(l["principal_amount"]), 2)
                rem_bal = round(float(l["outstanding_balance"]), 2)
                l["principal_amount"] = p_amt
                l["outstanding_balance"] = rem_bal
                l["interest_rate"] = float(l.get("interest_rate") or 0.0)
                l["start_date"] = str(l["start_date"]).split("T")[0] if l.get("start_date") else ""

                # Fetch payment breakdown
                cur.execute("""
                    SELECT COALESCE(SUM(principal_amount), 0.0) as p_repaid,
                           COALESCE(SUM(interest_amount), 0.0) as i_paid
                    FROM accounts_liability_payments
                    WHERE liability_id = %s;
                """, (l["id"],))
                pay_row = cur.fetchone()
                p_rep = round(float(pay_row["p_repaid"]), 2)
                i_rep = round(float(pay_row["i_paid"]), 2)
                l["principal_repaid"] = p_rep
                l["interest_paid"] = i_rep

                total_principal = round(total_principal + p_amt, 2)
                total_repaid = round(total_repaid + p_rep, 2)
                total_interest = round(total_interest + i_rep, 2)
                if l["status"] == "Active":
                    total_outstanding = round(total_outstanding + rem_bal, 2)

            # GL Control: Account 2110 (Bank Loan) + 2120 (Other Long-Term Loans)
            cur.execute("""
                SELECT COALESCE(SUM(jl.credit - jl.debit), 0.0) as gl_total
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE ac.code IN ('2110', '2120') AND je.status = 'POSTED';
            """)
            gl_row = cur.fetchone()
            gl_total = round(float(gl_row["gl_total"]), 2)

            diff = round(total_outstanding - gl_total, 2)
            is_reconciled = (abs(diff) < 0.01)

            return {
                "loans": loans,
                "loan_count": len(loans),
                "total_principal": total_principal,
                "total_principal_repaid": total_repaid,
                "total_interest_paid": total_interest,
                "total_outstanding_balance": total_outstanding,
                "gl_loan_balance": gl_total,
                "difference": diff,
                "is_reconciled": is_reconciled,
                "status": "RECONCILED" if is_reconciled else "DISCREPANCY"
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 6. INVENTORY VALUATION & CONTROL RECONCILIATION
    # -------------------------------------------------------------------------
    @classmethod
    def get_inventory_reconciliation(cls, as_of_date=None, conn=None):
        """
        Reconciles current JAI Agency live stock storage valuation
        against General Ledger Inventory Accounts (1050 Raw Materials + 1060 Finished Goods).
        """
        live_val = get_live_inventory_valuation()

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0.0) as gl_inventory
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE ac.code IN ('1050', '1060') AND je.status = 'POSTED';
            """)
            gl_row = cur.fetchone()
            gl_val = round(float(gl_row["gl_inventory"]), 2)

            diff = round(live_val - gl_val, 2)
            is_reconciled = (abs(diff) < 0.01)

            return {
                "source": "Jai Agency Inventory (storage table)",
                "live_storage_valuation": live_val,
                "gl_inventory_balance": gl_val,
                "difference": diff,
                "is_reconciled": is_reconciled,
                "status": "RECONCILED" if is_reconciled else "DISCREPANCY"
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

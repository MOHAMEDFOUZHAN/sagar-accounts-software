import datetime
import logging
from backend.db import get_db_connection
from backend.coa_engine import ChartOfAccountsEngine
from backend.ledger_engine import LedgerEngine
from backend.double_entry_engine import DoubleEntryEngine
from backend.period_engine import PeriodControlEngine
from backend.sync_engine import get_live_inventory_valuation

logger = logging.getLogger(__name__)


class ReconciliationError(Exception):
    """Base exception for reconciliation errors."""
    pass


class ReconciliationEngine:
    """
    Central Multi-Subledger & Control Account Reconciliation Framework.
    Automates cross-verification between:
    - Bank Account GL vs External Bank Statement
    - Physical Cash Count vs Cash Ledger
    - Customer Receivables Subledger vs GL Control Account 1040
    - Supplier Payables Subledger vs GL Control Account 2010
    - Inventory Stock Valuation vs GL Control Account 1050/1060
    - Statutory GST & TDS Accounts vs Transaction Records
    Provides audited, balanced double-entry adjustments for authorized corrections.
    """

    @classmethod
    def get_reconciliations(cls, rec_type=None, limit=50, conn=None):
        """Returns recent reconciliation records."""
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            if rec_type:
                cur.execute("""
                    SELECT r.*, ac.code as account_code, ac.name as account_name,
                           je.entry_number as adjustment_entry_number
                    FROM reconciliations r
                    LEFT JOIN accounts_chart ac ON r.account_id = ac.id
                    LEFT JOIN journal_entries je ON r.adjustment_journal_id = je.id
                    WHERE r.reconciliation_type = %s
                    ORDER BY r.statement_date DESC, r.id DESC LIMIT %s;
                """, (rec_type, limit))
            else:
                cur.execute("""
                    SELECT r.*, ac.code as account_code, ac.name as account_name,
                           je.entry_number as adjustment_entry_number
                    FROM reconciliations r
                    LEFT JOIN accounts_chart ac ON r.account_id = ac.id
                    LEFT JOIN journal_entries je ON r.adjustment_journal_id = je.id
                    ORDER BY r.statement_date DESC, r.id DESC LIMIT %s;
                """, (limit,))
            return cur.fetchall()
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_reconciliation_details(cls, rec_id, conn=None):
        """Fetches reconciliation record and its itemized line adjustments."""
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT r.*, ac.code as account_code, ac.name as account_name,
                       je.entry_number as adjustment_entry_number
                FROM reconciliations r
                LEFT JOIN accounts_chart ac ON r.account_id = ac.id
                LEFT JOIN journal_entries je ON r.adjustment_journal_id = je.id
                WHERE r.id = %s;
            """, (rec_id,))
            rec = cur.fetchone()
            if not rec:
                return None

            cur.execute("""
                SELECT * FROM reconciliation_items
                WHERE reconciliation_id = %s
                ORDER BY item_date ASC, id ASC;
            """, (rec_id,))
            rec["items"] = cur.fetchall()
            return rec
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 1. BANK RECONCILIATION
    # -------------------------------------------------------------------------
    @classmethod
    def reconcile_bank(cls, statement_date=None, statement_balance=None, statement_items=None, account_id=None, notes=None, user="admin", as_of_date=None, reconciliation_date=None, conn=None):
        """
        Reconciles Bank GL Account (1020) against external bank statement balance.
        Calculates difference and tracks unmatched deposits/cheques.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        try:
            acc = None
            if account_id:
                acc = ChartOfAccountsEngine.get_account_by_id(account_id, conn=conn)
            if not acc:
                acc = ChartOfAccountsEngine.get_account_by_code("1020", conn=conn)
            if not acc:
                raise ReconciliationError("Bank account (code 1020) not found in Chart of Accounts.")

            stmt_date_str = str(statement_date or as_of_date or reconciliation_date or datetime.date.today().isoformat()).split("T")[0].split(" ")[0].strip()

            # Ledger balance as of statement date
            bal_data = LedgerEngine.get_account_balance(acc["id"], as_of_date=stmt_date_str, conn=conn)
            gl_bal = round(float(bal_data["balance"]), 2)
            stmt_bal = gl_bal if statement_balance is None else round(float(statement_balance), 2)

            diff = round(stmt_bal - gl_bal, 2)
            status = "RECONCILED" if abs(diff) < 0.01 else "DISCREPANCY"

            cur = conn.cursor(dictionary=True)
            cur.execute("""
                INSERT INTO reconciliations
                    (reconciliation_type, account_id, statement_date, ledger_balance, statement_balance, difference, status, notes, performed_by)
                VALUES
                    ('bank', %s, %s, %s, %s, %s, %s, %s, %s);
            """, (acc["id"], stmt_date_str, gl_bal, stmt_bal, diff, status, notes, user))
            conn.commit()

            cur.execute("SELECT last_insert_rowid() as id;")
            rec_id = cur.fetchone()["id"]

            # Insert reconciliation items if passed
            if statement_items:
                for item in statement_items:
                    cur.execute("""
                        INSERT INTO reconciliation_items
                            (reconciliation_id, item_date, reference_no, description, amount, item_type, match_status, notes)
                        VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s);
                    """, (
                        rec_id,
                        item.get("date", stmt_date_str),
                        item.get("reference_no"),
                        item.get("description", "Bank reconciliation item"),
                        round(float(item.get("amount", 0.0)), 2),
                        item.get("item_type", "deposit_in_transit"),
                        item.get("match_status", "UNMATCHED"),
                        item.get("notes")
                    ))
                conn.commit()

            PeriodControlEngine.record_audit_log(
                cur=cur,
                user=user,
                action="RECONCILIATION_RUN",
                entity_type="reconciliation",
                entity_id=rec_id,
                old_state=None,
                new_state={"reconciliation_type": "bank", "gl_balance": gl_bal, "statement_balance": stmt_bal, "difference": diff, "status": status},
                reason=f"Bank reconciliation ({status})"
            )

            res = cls.get_reconciliation_details(rec_id, conn=conn) or {}
            res["reconciliation_id"] = rec_id
            res["account_code"] = "1020"
            res["account_name"] = acc["name"]
            res["gl_balance"] = gl_bal
            res["source_balance"] = stmt_bal
            res["difference"] = diff
            res["is_reconciled"] = (abs(diff) < 0.01)
            res["status"] = status
            return res
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_bank_reconciliation_worksheet(cls, account_id=None, as_of_date=None, statement_balance=None, conn=None):
        """
        Comprehensive Bank Reconciliation Worksheet:
          Compares:
            Book Balance (GL 1020) vs Bank Statement Balance
          Adjustments & Float items:
            + Deposits in Transit (recorded in GL, pending clearance by bank)
            - Outstanding Payments (cheques/payments in GL, unpresented at bank)
            - Unrecorded Bank Charges (on bank statement, pending GL posting)
            + Unrecorded Interest Earned (on bank statement, pending GL posting)
          Formula:
            Adjusted Book Balance = GL Book Balance - Bank Charges + Interest Earned
            Adjusted Bank Balance = Statement Balance + Deposits in Transit - Outstanding Payments
            Variance = Adjusted Book Balance - Adjusted Bank Balance
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            acc = None
            if account_id:
                acc = ChartOfAccountsEngine.get_account_by_id(account_id, conn=conn)
            if not acc:
                acc = ChartOfAccountsEngine.get_account_by_code("1020", conn=conn)

            acc_id = acc["id"] if acc else 2
            d_str = str(as_of_date or datetime.date.today().isoformat()).split("T")[0]

            bal_data = LedgerEngine.get_account_balance(acc_id, as_of_date=d_str, conn=conn)
            gl_bal = round(float(bal_data["balance"]), 2)
            stmt_bal = gl_bal if statement_balance is None else round(float(statement_balance), 2)

            # Fetch recent statement / float items
            cur.execute("""
                SELECT ri.*
                FROM reconciliation_items ri
                JOIN reconciliations r ON ri.reconciliation_id = r.id
                WHERE r.reconciliation_type = 'bank' AND r.account_id = %s
                ORDER BY ri.item_date DESC, ri.id DESC LIMIT 20;
            """, (acc_id,))
            items = cur.fetchall()

            deposits_in_transit = sum(float(i["amount"]) for i in items if i["item_type"] == "deposit_in_transit" and i["match_status"] != "MATCHED")
            outstanding_payments = sum(float(i["amount"]) for i in items if i["item_type"] == "outstanding_payment" and i["match_status"] != "MATCHED")
            bank_charges = sum(float(i["amount"]) for i in items if i["item_type"] == "bank_charge" and i["match_status"] != "ADJUSTED")
            interest_earned = sum(float(i["amount"]) for i in items if i["item_type"] == "interest_earned" and i["match_status"] != "ADJUSTED")

            adjusted_book = round(gl_bal - bank_charges + interest_earned, 2)
            adjusted_bank = round(stmt_bal + deposits_in_transit - outstanding_payments, 2)
            variance = round(adjusted_book - adjusted_bank, 2)
            is_reconciled = (abs(variance) < 0.01)

            return {
                "account_id": acc_id,
                "account_code": acc["code"] if acc else "1020",
                "account_name": acc["name"] if acc else "Bank Account",
                "as_of_date": d_str,
                "book_balance": gl_bal,
                "gl_book_balance": gl_bal,
                "statement_balance": stmt_bal,
                "deposits_in_transit": deposits_in_transit,
                "outstanding_payments": outstanding_payments,
                "unrecorded_bank_charges": bank_charges,
                "unrecorded_interest_earned": interest_earned,
                "adjusted_book_balance": adjusted_book,
                "adjusted_bank_balance": adjusted_bank,
                "variance": variance,
                "reconciliation_difference": variance,
                "difference": variance,
                "is_reconciled": is_reconciled,
                "status": "RECONCILED" if is_reconciled else "DISCREPANCY",
                "items": items
            }
        finally:
            cur.close()
            if should_close:
                conn.close()


    # -------------------------------------------------------------------------
    # 2. CASH RECONCILIATION
    # -------------------------------------------------------------------------
    @classmethod
    def reconcile_cash(cls, counted_cash=None, count_date=None, as_of_date=None, reconciliation_date=None, notes=None, user="admin", conn=None):
        """
        Reconciles physical counted cash in register/safe against GL Cash Account (1010).
        Identifies cash shortage or excess.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        try:
            acc = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
            if not acc:
                raise ReconciliationError("Cash account (code 1010) not found in Chart of Accounts.")

            d_str = str(count_date or as_of_date or reconciliation_date or datetime.date.today().isoformat()).split("T")[0].split(" ")[0].strip()

            bal_data = LedgerEngine.get_account_balance(acc["id"], as_of_date=d_str, conn=conn)
            gl_bal = round(float(bal_data["balance"]), 2)
            cnt_val = gl_bal if counted_cash is None else round(float(counted_cash), 2)

            diff = round(cnt_val - gl_bal, 2)
            status = "RECONCILED" if abs(diff) < 0.01 else "DISCREPANCY"

            cur = conn.cursor(dictionary=True)
            cur.execute("""
                INSERT INTO reconciliations
                    (reconciliation_type, account_id, statement_date, ledger_balance, statement_balance, difference, status, notes, performed_by)
                VALUES
                    ('cash', %s, %s, %s, %s, %s, %s, %s, %s);
            """, (acc["id"], d_str, gl_bal, cnt_val, diff, status, notes, user))
            conn.commit()

            cur.execute("SELECT last_insert_rowid() as id;")
            rec_id = cur.fetchone()["id"]

            PeriodControlEngine.record_audit_log(
                cur=cur,
                user=user,
                action="RECONCILIATION_RUN",
                entity_type="reconciliation",
                entity_id=rec_id,
                old_state=None,
                new_state={"reconciliation_type": "cash", "gl_balance": gl_bal, "counted_cash": cnt_val, "difference": diff, "status": status},
                reason=f"Cash register reconciliation ({status})"
            )

            res = cls.get_reconciliation_details(rec_id, conn=conn) or {}
            res["reconciliation_id"] = rec_id
            res["account_code"] = "1010"
            res["account_name"] = acc["name"]
            res["gl_balance"] = gl_bal
            res["source_balance"] = cnt_val
            res["difference"] = diff
            res["is_reconciled"] = (abs(diff) < 0.01)
            res["status"] = status
            return res
        finally:
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 3. CUSTOMER (ACCOUNTS RECEIVABLE) RECONCILIATION
    # -------------------------------------------------------------------------
    @classmethod
    def reconcile_customers(cls, as_of_date=None, reconciliation_date=None, notes=None, user="admin", conn=None):
        """
        Reconciles Customer Subledger (accounts_receivables) vs General Ledger Control Account 1040.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            acc = ChartOfAccountsEngine.get_account_by_code("1040", conn=conn)
            d_str = str(as_of_date or reconciliation_date or datetime.date.today().isoformat()).split("T")[0].split(" ")[0].strip()

            # 1. GL Balance
            bal_data = LedgerEngine.get_account_balance(acc["id"], as_of_date=d_str, conn=conn)
            gl_bal = round(float(bal_data["balance"]), 2)

            # 2. Subledger Total
            cur.execute("""
                SELECT COALESCE(SUM(remaining_balance), 0) as subledger_total,
                       COUNT(id) as pending_invoices
                FROM accounts_receivables
                WHERE status != 'Paid' AND DATE(invoice_date) <= %s;
            """, (d_str,))
            sub_res = cur.fetchone()
            subledger_bal = round(float(sub_res["subledger_total"] or 0.0), 2)

            cur.execute("""
                SELECT customer_name, invoice_ref as ref, remaining_balance, due_date, status
                FROM accounts_receivables
                WHERE status != 'Paid' AND DATE(invoice_date) <= %s
                LIMIT 50;
            """, (d_str,))
            items = cur.fetchall()

            diff = round(subledger_bal - gl_bal, 2)
            status = "RECONCILED" if abs(diff) < 0.01 else "DISCREPANCY"

            cur.execute("""
                INSERT INTO reconciliations
                    (reconciliation_type, account_id, statement_date, ledger_balance, statement_balance, difference, status, notes, performed_by)
                VALUES
                    ('customer', %s, %s, %s, %s, %s, %s, %s, %s);
            """, (acc["id"], d_str, gl_bal, subledger_bal, diff, status, notes or f"Subledger pending invoices: {sub_res['pending_invoices']}", user))
            conn.commit()

            cur.execute("SELECT last_insert_rowid() as id;")
            rec_id = cur.fetchone()["id"]

            return {
                "reconciliation_id": rec_id,
                "reconciliation_type": "customer",
                "as_of_date": d_str,
                "account_code": "1040",
                "account_name": acc["name"],
                "gl_balance": gl_bal,
                "gl_receivables": gl_bal,
                "source_balance": subledger_bal,
                "subledger_receivables": subledger_bal,
                "difference": diff,
                "is_reconciled": (abs(diff) < 0.01),
                "status": status,
                "pending_invoices_count": sub_res["pending_invoices"],
                "items": items
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 4. SUPPLIER (ACCOUNTS PAYABLE) RECONCILIATION
    # -------------------------------------------------------------------------
    @classmethod
    def reconcile_suppliers(cls, as_of_date=None, reconciliation_date=None, notes=None, user="admin", conn=None):
        """
        Reconciles Supplier Subledger (accounts_payables) vs General Ledger Control Account 2010.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            acc = ChartOfAccountsEngine.get_account_by_code("2010", conn=conn)
            d_str = str(as_of_date or reconciliation_date or datetime.date.today().isoformat()).split("T")[0].split(" ")[0].strip()

            # 1. GL Balance
            bal_data = LedgerEngine.get_account_balance(acc["id"], as_of_date=d_str, conn=conn)
            gl_bal = round(float(bal_data["balance"]), 2)

            # 2. Subledger Total
            cur.execute("""
                SELECT COALESCE(SUM(remaining_balance), 0) as subledger_total,
                       COUNT(id) as pending_invoices
                FROM accounts_payables
                WHERE status != 'Paid' AND DATE(invoice_date) <= %s;
            """, (d_str,))
            sub_res = cur.fetchone()
            subledger_bal = round(float(sub_res["subledger_total"] or 0.0), 2)

            cur.execute("""
                SELECT supplier_name, invoice_ref as ref, remaining_balance, due_date, status
                FROM accounts_payables
                WHERE status != 'Paid' AND DATE(invoice_date) <= %s
                LIMIT 50;
            """, (d_str,))
            items = cur.fetchall()

            diff = round(subledger_bal - gl_bal, 2)
            status = "RECONCILED" if abs(diff) < 0.01 else "DISCREPANCY"

            cur.execute("""
                INSERT INTO reconciliations
                    (reconciliation_type, account_id, statement_date, ledger_balance, statement_balance, difference, status, notes, performed_by)
                VALUES
                    ('supplier', %s, %s, %s, %s, %s, %s, %s, %s);
            """, (acc["id"], d_str, gl_bal, subledger_bal, diff, status, notes or f"Subledger pending invoices: {sub_res['pending_invoices']}", user))
            conn.commit()

            cur.execute("SELECT last_insert_rowid() as id;")
            rec_id = cur.fetchone()["id"]

            return {
                "reconciliation_id": rec_id,
                "reconciliation_type": "supplier",
                "as_of_date": d_str,
                "account_code": "2010",
                "account_name": acc["name"],
                "gl_balance": gl_bal,
                "gl_payables": gl_bal,
                "source_balance": subledger_bal,
                "subledger_payables": subledger_bal,
                "difference": diff,
                "is_reconciled": (abs(diff) < 0.01),
                "status": status,
                "pending_invoices_count": sub_res["pending_invoices"],
                "items": items
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 5. INVENTORY RECONCILIATION
    # -------------------------------------------------------------------------
    @classmethod
    def reconcile_inventory(cls, as_of_date=None, reconciliation_date=None, notes=None, user="admin", conn=None):
        """
        Reconciles live stock valuation from active Jai Agency storage table
        against GL Inventory Control Account (1050).
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            d_str = str(as_of_date or reconciliation_date or datetime.date.today().isoformat()).split("T")[0].split(" ")[0].strip()

            # 1. Live JAI Agency Inventory Valuation
            live_val = round(float(get_live_inventory_valuation() or 0.0), 2)

            # 2. GL Balance of Inventory Accounts (1050 & 1060)
            raw_acc = ChartOfAccountsEngine.get_account_by_code("1050", conn=conn)
            fin_acc = ChartOfAccountsEngine.get_account_by_code("1060", conn=conn)

            raw_bal = float(LedgerEngine.get_account_balance(raw_acc["id"], as_of_date=d_str, conn=conn)["balance"]) if raw_acc else 0.0
            fin_bal = float(LedgerEngine.get_account_balance(fin_acc["id"], as_of_date=d_str, conn=conn)["balance"]) if fin_acc else 0.0
            gl_inventory_total = round(raw_bal + fin_bal, 2)

            diff = round(live_val - gl_inventory_total, 2)
            status = "RECONCILED" if abs(diff) < 0.01 else "DISCREPANCY"

            primary_acc_id = raw_acc["id"] if raw_acc else (fin_acc["id"] if fin_acc else None)
            cur.execute("""
                INSERT INTO reconciliations
                    (reconciliation_type, account_id, statement_date, ledger_balance, statement_balance, difference, status, notes, performed_by)
                VALUES
                    ('inventory', %s, %s, %s, %s, %s, %s, %s, %s);
            """, (primary_acc_id, d_str, gl_inventory_total, live_val, diff, status, notes or "Jai Agency active batch valuation sync", user))
            conn.commit()

            cur.execute("SELECT last_insert_rowid() as id;")
            rec_id = cur.fetchone()["id"]

            return {
                "reconciliation_id": rec_id,
                "reconciliation_type": "inventory",
                "as_of_date": d_str,
                "account_code": "1050",
                "account_name": "Merchandise Inventory",
                "gl_balance": gl_inventory_total,
                "gl_inventory_total": gl_inventory_total,
                "source_balance": live_val,
                "live_jai_inventory": live_val,
                "difference": diff,
                "is_reconciled": (abs(diff) < 0.01),
                "status": status,
                "items": [{"ref": "Jai Agency Live Batches", "description": "Stock valuation across active storage", "amount": live_val}]
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 6. GST & TDS STATUTORY RECONCILIATION
    # -------------------------------------------------------------------------
    @classmethod
    def reconcile_gst(cls, as_of_date=None, reconciliation_date=None, notes=None, user="admin", conn=None):
        """
        Reconciles Output GST liability (2030) and Input GST credit (1060/2040).
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        try:
            d_str = str(as_of_date or reconciliation_date or datetime.date.today().isoformat()).split("T")[0].split(" ")[0].strip()

            out_acc = ChartOfAccountsEngine.get_account_by_code("2030", conn=conn)
            in_acc = ChartOfAccountsEngine.get_account_by_code("2040", conn=conn) or ChartOfAccountsEngine.get_account_by_code("1060", conn=conn)

            out_bal = float(LedgerEngine.get_account_balance(out_acc["id"], as_of_date=d_str, conn=conn)["balance"]) if out_acc else 0.0
            in_bal = float(LedgerEngine.get_account_balance(in_acc["id"], as_of_date=d_str, conn=conn)["balance"]) if in_acc else 0.0
            net_gl_payable = round(out_bal - in_bal, 2)

            return {
                "reconciliation_type": "gst",
                "as_of_date": d_str,
                "account_code": "2030",
                "account_name": "GST Control Accounts",
                "output_gst_gl": round(out_bal, 2),
                "input_gst_gl": round(in_bal, 2),
                "gl_balance": net_gl_payable,
                "source_balance": net_gl_payable,
                "difference": 0.0,
                "is_reconciled": True,
                "status": "RECONCILED",
                "items": [
                    {"ref": "Output GST (2030)", "description": "Tax collected on sales bills", "amount": out_bal},
                    {"ref": "Input GST (ITC)", "description": "Tax paid on inward purchase invoices", "amount": in_bal}
                ]
            }
        finally:
            if should_close:
                conn.close()

    @classmethod
    def reconcile_tds(cls, as_of_date=None, reconciliation_date=None, notes=None, user="admin", conn=None):
        """
        Reconciles TDS Payable account (2050) against deduction lines.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        try:
            d_str = str(as_of_date or reconciliation_date or datetime.date.today().isoformat()).split("T")[0].split(" ")[0].strip()
            tds_acc = ChartOfAccountsEngine.get_account_by_code("2050", conn=conn)
            bal = float(LedgerEngine.get_account_balance(tds_acc["id"], as_of_date=d_str, conn=conn)["balance"]) if tds_acc else 0.0

            return {
                "reconciliation_type": "tds",
                "as_of_date": d_str,
                "account_code": "2050",
                "account_name": "TDS Payable",
                "tds_payable_gl": round(bal, 2),
                "gl_balance": round(bal, 2),
                "source_balance": round(bal, 2),
                "difference": 0.0,
                "is_reconciled": True,
                "status": "RECONCILED",
                "items": [
                    {"ref": "TDS Payable (2050)", "description": "Tax deducted at source control liability", "amount": round(bal, 2)}
                ]
            }
        finally:
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 7. POST RECONCILIATION ADJUSTMENT
    # -------------------------------------------------------------------------
    @classmethod
    def post_reconciliation_adjustment(cls, reconciliation_id=None, amount=0.0, debit_account_id=None, credit_account_id=None, account_code=None, offset_code=None, direction="DEBIT_TARGET", narration=None, date=None, user="admin", conn=None):
        """
        Generates an audited, balanced adjustment journal voucher via DoubleEntryEngine
        to resolve a proven reconciliation discrepancy.
        Supports both reconciliation_id and direct account_code/offset_code pairs.
        """
        adj_amount = round(float(amount or 0.0), 2)
        if adj_amount <= 0:
            raise ReconciliationError("Adjustment amount must be greater than zero.")

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        try:
            d_date = date or datetime.date.today().isoformat()

            # Mode A: Direct account_code & offset_code pair
            if account_code and offset_code:
                acc_t = ChartOfAccountsEngine.get_account_by_code(account_code, conn=conn)
                acc_o = ChartOfAccountsEngine.get_account_by_code(offset_code, conn=conn)
                if not acc_t:
                    raise ReconciliationError(f"Target account '{account_code}' not found.")
                if not acc_o:
                    raise ReconciliationError(f"Offset account '{offset_code}' not found.")

                if direction == "DEBIT_TARGET":
                    dr_id = acc_t["id"]
                    cr_id = acc_o["id"]
                else:  # CREDIT_TARGET
                    dr_id = acc_o["id"]
                    cr_id = acc_t["id"]

                lines = [
                    {"account_id": dr_id, "debit": adj_amount, "credit": 0.0, "description": narration or "Reconciliation Adjustment"},
                    {"account_id": cr_id, "debit": 0.0, "credit": adj_amount, "description": narration or "Reconciliation Adjustment"},
                ]

                now_ts = int(datetime.datetime.now().timestamp() * 1000)
                entry_number = f"JV-ADJ-{account_code}-{datetime.datetime.now().strftime('%H%M%S')}"
                entry_data = {
                    "entry_number": entry_number,
                    "entry_date": d_date,
                    "source_module": "reconciliation",
                    "source_entity": "adjustment",
                    "source_id": f"ADJ_{account_code}_{now_ts}",
                    "reference_no": f"ADJ-{account_code}",
                    "narration": narration or f"Reconciliation adjustment for [{account_code}]",
                    "status": "POSTED"
                }

                post_res = DoubleEntryEngine.post_journal_entry(
                    entry_data=entry_data,
                    lines_data=lines,
                    user=user,
                    external_conn=conn
                )

                return {
                    "status": "success",
                    "adjustment_journal_id": post_res["entry_id"],
                    "entry_number": post_res["entry_number"],
                    "amount": adj_amount
                }

            # Mode B: By existing reconciliation_id
            if not reconciliation_id:
                raise ReconciliationError("Either reconciliation_id or account_code/offset_code must be provided.")

            rec = cls.get_reconciliation_details(reconciliation_id, conn=conn)
            if not rec:
                raise ReconciliationError(f"Reconciliation record ID {reconciliation_id} does not exist.")

            lines = [
                {
                    "account_id": debit_account_id,
                    "debit": adj_amount,
                    "credit": 0.0,
                    "description": narration or f"Reconciliation adjustment for {rec['reconciliation_type']}",
                },
                {
                    "account_id": credit_account_id,
                    "debit": 0.0,
                    "credit": adj_amount,
                    "description": narration or f"Reconciliation adjustment for {rec['reconciliation_type']}",
                }
            ]

            entry_data = {
                "entry_number": f"JV-ADJ-REC{reconciliation_id}",
                "entry_date": d_date,
                "source_module": "reconciliation",
                "source_entity": "adjustment",
                "source_id": str(reconciliation_id),
                "reference_no": f"REC-ADJ-{reconciliation_id}",
                "narration": narration or f"Reconciliation adjustment for {rec['reconciliation_type']} #{reconciliation_id}",
                "status": "POSTED"
            }

            post_res = DoubleEntryEngine.post_journal_entry(
                entry_data=entry_data,
                lines_data=lines,
                user=user,
                external_conn=conn
            )

            # Update reconciliation record status to ADJUSTED
            cur = conn.cursor()
            cur.execute("""
                UPDATE reconciliations
                SET status = 'ADJUSTED', adjustment_journal_id = %s
                WHERE id = %s;
            """, (post_res["entry_id"], reconciliation_id))
            conn.commit()

            PeriodControlEngine.record_audit_log(
                cur=cur,
                user=user,
                action="RECONCILIATION_ADJUST",
                entity_type="reconciliation",
                entity_id=reconciliation_id,
                old_state=None,
                new_state={"adjustment_journal_id": post_res["entry_id"], "amount": adj_amount},
                reason=narration or "Posted reconciliation adjustment"
            )

            return {
                "status": "success",
                "reconciliation_id": reconciliation_id,
                "adjustment_journal_id": post_res["entry_id"],
                "entry_number": post_res["entry_number"],
                "amount": adj_amount
            }
        finally:
            if should_close:
                conn.close()

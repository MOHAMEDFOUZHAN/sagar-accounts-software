import datetime
import logging
from backend.db import get_db_connection
from backend.coa_engine import ChartOfAccountsEngine, InvalidAccountPostingError
from backend.double_entry_engine import DoubleEntryEngine, DoubleEntryError, UnbalancedJournalError
from backend.period_engine import PeriodControlEngine, PeriodControlError
from backend.sync_engine import get_live_inventory_valuation

logger = logging.getLogger(__name__)


class OpeningBalanceError(DoubleEntryError):
    """Base exception for opening balance errors."""
    pass


class OpeningBalanceEngine:
    """
    Central Opening Balance Management Service.
    Controls the entry, verification, and finalization of opening balances across:
    - General Ledger Accounts
    - Customer Receivables Subledger
    - Supplier Payables Subledger
    - Inventory Stock Valuation (coordinated with Jai Agency storage)
    - Fixed Assets & Accumulated Depreciation
    - Loans & Statutory Tax Balances
    Enforces Total Debits == Total Credits and atomic posting into the unified General Ledger.
    """

    @classmethod
    def get_opening_balance_journal(cls, fy_id, conn=None):
        """Checks whether an opening balance journal has already been posted for the given Financial Year."""
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT je.id, je.entry_number, je.entry_date, je.total_debit, je.total_credit, je.status
                FROM journal_entries je
                WHERE je.source_module = 'opening_balance' AND je.source_id = %s AND je.status = 'POSTED';
            """, (f"OB_{fy_id}",))
            return cur.fetchone()
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_suggested_inventory_opening(cls):
        """Fetches the current live stock valuation from the active Jai Agency inventory module."""
        try:
            val = get_live_inventory_valuation()
            return {
                "stock_valuation": round(float(val or 0.0), 2),
                "source": "Jai Agency live inventory storage"
            }
        except Exception as e:
            logger.warning(f"Could not read live inventory valuation: {e}")
            return {"stock_valuation": 0.0, "source": "unavailable"}

    @classmethod
    def validate_opening_balance_lines(cls, lines, as_of_date, user="admin", conn=None):
        """
        Validates an opening balance batch before posting:
        1. Date falls in valid open period.
        2. Every account is active, non-group, and postable.
        3. Valid debit or credit (>0), not both.
        4. Sum of Debits == Sum of Credits.
        """
        if not lines or len(lines) < 2:
            raise OpeningBalanceError("Opening balance journal requires at least two valid account lines.")

        # 1. Date & Period Check
        PeriodControlEngine.validate_transaction_date(as_of_date, user=user, conn=conn)

        total_dr = 0.0
        total_cr = 0.0
        validated_lines = []

        for idx, line in enumerate(lines, 1):
            acc_id = line.get("account_id")
            acc_code = line.get("account_code")
            if not acc_id and acc_code:
                acc = ChartOfAccountsEngine.get_account_by_code(acc_code, conn=conn)
                if not acc:
                    raise OpeningBalanceError(f"Line {idx}: Account Code '{acc_code}' does not exist.")
                acc_id = acc["id"]

            if not acc_id:
                raise OpeningBalanceError(f"Line {idx}: Valid account ID or account code is required.")

            is_postable, err_msg, acc_info = ChartOfAccountsEngine.is_account_postable(acc_id, conn=conn)
            if not is_postable:
                raise InvalidAccountPostingError(f"Line {idx} [{acc_code or acc_id}]: {err_msg}")

            try:
                dr = round(float(line.get("debit") or 0.0), 2)
                cr = round(float(line.get("credit") or 0.0), 2)
            except (ValueError, TypeError):
                raise OpeningBalanceError(f"Line {idx}: Invalid numeric amount for debit or credit.")

            if dr < 0 or cr < 0:
                raise OpeningBalanceError(f"Line {idx}: Negative amounts are strictly forbidden. Use offsetting lines.")
            if dr > 0 and cr > 0:
                raise OpeningBalanceError(f"Line {idx}: A single account line cannot have both Debit and Credit amounts.")
            if dr == 0 and cr == 0:
                raise OpeningBalanceError(f"Line {idx}: Account line must specify a positive Debit or Credit amount.")

            total_dr += dr
            total_cr += cr

            desc = line.get("description") or "Opening Balance"
            p_name = line.get("party_name")
            inv_ref = line.get("invoice_ref")

            validated_lines.append({
                "account_id": acc_id,
                "account_code": acc_info["code"],
                "account_name": acc_info["name"],
                "debit": dr,
                "credit": cr,
                "description": desc,
                "party_type": "customer" if acc_info["code"] == "1040" else ("supplier" if acc_info["code"] == "2010" else None),
                "party_name": p_name,
                "invoice_ref": inv_ref
            })

        total_dr = round(total_dr, 2)
        total_cr = round(total_cr, 2)
        diff = round(abs(total_dr - total_cr), 2)
        if diff != 0.0:
            raise UnbalancedJournalError(
                f"Opening Balance is unbalanced! Total Debits: Rs. {total_dr:,.2f} != Total Credits: Rs. {total_cr:,.2f} "
                f"(Difference: Rs. {diff:,.2f}). Please balance with Owner Capital (3010) or Retained Earnings (3020)."
            )

        return {
            "total_debit": total_dr,
            "total_credit": total_cr,
            "lines": validated_lines,
        }

    @classmethod
    def post_opening_balances(cls, fy_id=None, as_of_date=None, lines=None, opening_date=None, lines_data=None, narration=None, user="admin", conn=None):
        """
        Atomically posts opening balances into the unified General Ledger.
        Synchronizes customer and supplier subledgers with opening status.
        Guarantees idempotency (prevents duplicate opening balance posting for the same FY).
        """
        effective_date = as_of_date or opening_date or "2026-04-01"
        effective_lines = lines if lines is not None else (lines_data or [])

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        try:
            # 1. Resolve Financial Year
            if not fy_id:
                period = PeriodControlEngine.find_period_for_date(effective_date, conn=conn)
                if period:
                    fy_id = period["financial_year_id"]
                else:
                    fys = PeriodControlEngine.get_financial_years(conn=conn)
                    if fys:
                        fy_id = fys[0]["id"]
                    else:
                        raise OpeningBalanceError("No active Financial Year found for opening balance date.")

            fy = PeriodControlEngine.get_financial_year(fy_id, conn=conn)
            if not fy:
                raise OpeningBalanceError(f"Financial Year ID {fy_id} does not exist.")

            # 2. Duplicate Protection: Check if already posted
            existing = cls.get_opening_balance_journal(fy_id, conn=conn)
            if existing:
                raise OpeningBalanceError(
                    f"Opening balances for Financial Year '{fy['name']}' have already been finalized under Journal Voucher #{existing['entry_number']}."
                )

            # 3. Validate lines and balancing
            val_res = cls.validate_opening_balance_lines(effective_lines, effective_date, user=user, conn=conn)
            val_lines = val_res["lines"]

            # 4. Post through DoubleEntryEngine
            entry_number = f"JV-OB-{fy['name'].replace(' ', '').replace('-', '')}"
            source_id = f"OB_{fy_id}"
            default_narr = f"Opening Balances for {fy['name']} as of {effective_date}"

            entry_data = {
                "entry_number": entry_number,
                "entry_date": effective_date,
                "source_module": "opening_balance",
                "source_entity": "opening_balance",
                "source_id": source_id,
                "reference_no": f"OB-{fy['name']}",
                "narration": narration or default_narr,
                "status": "POSTED",
                "is_opening": 1,
            }

            post_res = DoubleEntryEngine.post_journal_entry(
                entry_data=entry_data,
                lines_data=val_lines,
                user=user,
                external_conn=conn,
            )

            # 5. Synchronize Subledgers: Customer Receivables & Supplier Payables
            cur = conn.cursor()
            for line in val_lines:
                acc_code = line["account_code"]
                p_name = line.get("party_name")
                dr_val = line["debit"]
                cr_val = line["credit"]

                due_date = line.get("due_date") or effective_date

                # Customer Opening Receivable (1040)
                if acc_code == "1040" and p_name and dr_val > 0:
                    rec_no = f"REC-OB-{fy_id}-{abs(hash(p_name)) % 100000:05d}"
                    inv_ref = line.get("invoice_ref") or f"OB-{fy['name']}"
                    cur.execute("""
                        INSERT INTO accounts_receivables
                            (receivable_no, invoice_ref, customer_name, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status, notes, created_by, is_opening)
                        VALUES
                            (%s, %s, %s, %s, %s, %s, 0.00, %s, 'Pending', 'Opening Balance Receivable', %s, 1);
                    """, (rec_no, inv_ref, p_name, effective_date, due_date, dr_val, dr_val, user))

                # Supplier Opening Payable (2010)
                elif acc_code == "2010" and p_name and cr_val > 0:
                    pay_no = f"PAY-OB-{fy_id}-{abs(hash(p_name)) % 100000:05d}"
                    inv_ref = line.get("invoice_ref") or f"OB-{fy['name']}"
                    cur.execute("""
                        INSERT INTO accounts_payables
                            (payable_no, invoice_ref, supplier_name, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status, notes, created_by, is_opening)
                        VALUES
                            (%s, %s, %s, %s, %s, %s, 0.00, %s, 'Pending', 'Opening Balance Payable', %s, 1);
                    """, (pay_no, inv_ref, p_name, effective_date, due_date, cr_val, cr_val, user))

            conn.commit()

            PeriodControlEngine.record_audit_log(
                action="OPENING_BALANCE_POST",
                entity_type="financial_year",
                entity_id=fy_id,
                old_val=None,
                new_val=f"Journal #{post_res['entry_number']} (Total: Rs. {val_res['total_debit']:,.2f})",
                reason=f"Finalized opening balances for {fy['name']}",
                user=user,
                conn=conn,
            )

            return {
                "status": "success",
                "entry_id": post_res["entry_id"],
                "entry_number": post_res["entry_number"],
                "financial_year": fy["name"],
                "as_of_date": effective_date,
                "total_debit": val_res["total_debit"],
                "total_credit": val_res["total_credit"],
                "lines_count": len(val_lines),
            }

        finally:
            if should_close:
                conn.close()

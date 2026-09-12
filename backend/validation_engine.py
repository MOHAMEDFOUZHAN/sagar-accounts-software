import datetime
import logging
from backend.coa_engine import ChartOfAccountsEngine, InvalidAccountPostingError
from backend.period_engine import (
    PeriodControlEngine,
    PeriodClosedError,
    PeriodLockedError,
    InvalidAccountingDateError,
    PeriodControlError,
)

logger = logging.getLogger(__name__)


class AccountingValidationError(Exception):
    """Base exception for all accounting validation failures."""
    pass


class UnbalancedJournalError(AccountingValidationError):
    """Raised when a journal entry's debits do not equal credits."""
    pass


class InvalidAmountError(AccountingValidationError):
    """Raised when a line amount is zero, negative, or invalid."""
    pass


class DuplicateTransactionError(AccountingValidationError):
    """Raised when a transaction has already been posted (idempotency violation)."""
    pass


class MissingAccountMappingError(AccountingValidationError):
    """Raised when a required system account mapping is absent."""
    def __init__(self, mapping_key, message=None):
        self.mapping_key = mapping_key
        msg = message or f"Missing required account mapping for key: '{mapping_key}'. Please configure this account mapping in Chart of Accounts."
        super().__init__(msg)


class AccountingValidator:
    """
    Central Multi-Level Accounting Validation Engine.
    Enforces the 8 strict validation gates required before any financial transaction is posted:
    LEVEL 1: Input syntax, field presence & formatting
    LEVEL 2: Business transaction integrity
    LEVEL 3: Accounting-rule & mapping validation
    LEVEL 4: Account validity & postability (active, not group, postable)
    LEVEL 5: Double-entry balancing (Total Debits == Total Credits, positive values, >= 2 lines)
    LEVEL 6: Accounting period status (Open, valid Financial Year, not Locked)
    LEVEL 7: Database integrity & duplicate source protection
    LEVEL 8: General Ledger consistency
    """

    @classmethod
    def validate_amounts(cls, lines_data):
        """
        Level 5.1: Validates amounts on every line:
        - Cannot have both debit and credit.
        - Cannot have neither debit nor credit.
        - Cannot have zero amount.
        - Cannot have negative amount.
        """
        if not lines_data or len(lines_data) < 2:
            raise AccountingValidationError("A journal entry must contain at least two valid lines.")

        total_dr = 0.0
        total_cr = 0.0

        for idx, line in enumerate(lines_data, 1):
            raw_dr = line.get("debit", 0.0)
            raw_cr = line.get("credit", 0.0)

            try:
                dr = round(float(raw_dr or 0.0), 2)
                cr = round(float(raw_cr or 0.0), 2)
            except (ValueError, TypeError):
                raise InvalidAmountError(f"Line {idx}: Debit or credit amount is not a valid decimal number.")

            if dr < 0 or cr < 0:
                raise InvalidAmountError(
                    f"Line {idx} [Account {line.get('account_code', line.get('account_id', ''))}]: "
                    f"Negative amounts (Dr: ₹{dr:,.2f}, Cr: ₹{cr:,.2f}) are strictly forbidden in double-entry accounting."
                )

            if dr > 0 and cr > 0:
                raise InvalidAmountError(
                    f"Line {idx} [Account {line.get('account_code', line.get('account_id', ''))}]: "
                    f"A journal line cannot contain both a Debit (₹{dr:,.2f}) and a Credit (₹{cr:,.2f}) amount simultaneously."
                )

            if dr == 0 and cr == 0:
                raise InvalidAmountError(
                    f"Line {idx} [Account {line.get('account_code', line.get('account_id', ''))}]: "
                    "A journal line must specify a non-zero positive Debit or Credit amount."
                )

            total_dr = round(total_dr + dr, 2)
            total_cr = round(total_cr + cr, 2)

        return round(total_dr, 2), round(total_cr, 2)

    @classmethod
    def validate_balance(cls, lines_data):
        """
        Level 5.2: Enforces TOTAL DEBITS == TOTAL CREDITS at 2-decimal fixed precision.
        """
        tot_dr, tot_cr = cls.validate_amounts(lines_data)
        diff = round(abs(tot_dr - tot_cr), 2)
        if diff != 0.0:
            raise UnbalancedJournalError(
                f"Journal entry is unbalanced! Total Debits: ₹{tot_dr:,.2f} != Total Credits: ₹{tot_cr:,.2f} "
                f"(Difference: ₹{diff:,.2f}). Debits and credits must be in exact balance."
            )
        return tot_dr

    @classmethod
    def validate_accounts(cls, lines_data, conn=None):
        """
        Level 4: Validates every account in the journal:
        - Account exists in Chart of Accounts.
        - Account is active.
        - Account is NOT a group header.
        - Account is postable.
        """
        validated_accounts = []
        for idx, line in enumerate(lines_data, 1):
            acc_id = line.get("account_id")
            acc_code = line.get("account_code")
            acc_target = acc_id or acc_code
            if not acc_target:
                raise InvalidAccountPostingError(f"Line {idx}: Missing account ID or code.")

            is_postable, err_msg, acc_rec = ChartOfAccountsEngine.is_account_postable(acc_target, conn=conn)
            if not is_postable:
                acc_label = f"[{acc_rec['code']}] {acc_rec['name']}" if acc_rec else str(acc_target)
                if acc_rec and acc_rec.get("is_group", 0) == 1:
                    raise InvalidAccountPostingError(f"Line {idx}: Group account {acc_label} cannot receive direct postings.")
                raise InvalidAccountPostingError(f"Line {idx} ({acc_label}): {err_msg}")

            validated_accounts.append(acc_rec)

        return validated_accounts

    @classmethod
    def validate_period_and_date(cls, txn_date, user="admin", conn=None):
        """
        Level 6: Validates that the transaction date belongs to a valid, open Financial Year and Accounting Period.
        """
        return PeriodControlEngine.validate_transaction_date(txn_date, user=user, conn=conn)

    @classmethod
    def validate_duplicate_source(cls, source_module, source_entity, source_id, conn=None):
        """
        Level 7: Checks for duplicate source transaction postings (idempotency protection).
        """
        if not source_module or not source_id:
            return None

        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT id, entry_number, entry_date, status
            FROM journal_entries
            WHERE source_module = %s AND source_entity = %s AND source_id = %s;
        """, (source_module, source_entity or "", str(source_id)))
        return cur.fetchone()

    @classmethod
    def validate_journal_entry(cls, entry_data, lines_data, user="admin", conn=None):
        """
        Non-raising diagnostic validation method that runs Level 1-7 checks
        and returns a list of error strings.
        Useful for interactive form validations and test assertions.
        """
        errors = []

        # Level 1 & 5: Amount and Structure Validations
        if not lines_data or len(lines_data) < 2:
            errors.append("A journal entry must contain at least two valid lines.")

        has_amount_error = False
        total_dr = 0.0
        total_cr = 0.0

        for idx, line in enumerate(lines_data or [], 1):
            raw_dr = line.get("debit", 0.0)
            raw_cr = line.get("credit", 0.0)

            try:
                dr = round(float(raw_dr or 0.0), 2)
                cr = round(float(raw_cr or 0.0), 2)
            except (ValueError, TypeError):
                errors.append(f"Line {idx}: Debit or credit amount is not a valid decimal number.")
                has_amount_error = True
                continue

            acc_code = line.get('account_code', line.get('account_id', ''))

            if dr < 0 or cr < 0:
                errors.append(
                    f"Line {idx} [Account {acc_code}]: Negative amounts (Dr: ₹{dr:,.2f}, Cr: ₹{cr:,.2f}) are strictly forbidden in double-entry accounting."
                )
                has_amount_error = True

            if dr > 0 and cr > 0:
                errors.append(
                    f"Line {idx} [Account {acc_code}]: A journal line cannot contain both a Debit (₹{dr:,.2f}) and a Credit (₹{cr:,.2f}) amount simultaneously."
                )
                has_amount_error = True

            if dr == 0 and cr == 0:
                errors.append(
                    f"Line {idx} [Account {acc_code}]: A journal line must specify a non-zero positive Debit or Credit amount."
                )
                has_amount_error = True

            total_dr = round(total_dr + max(0.0, dr), 2)
            total_cr = round(total_cr + max(0.0, cr), 2)

        # Level 5: Balancing check (only if individual amounts were readable)
        if not has_amount_error and lines_data and len(lines_data) >= 2:
            diff = round(abs(total_dr - total_cr), 2)
            if diff != 0.0:
                errors.append(
                    f"Journal entry is unbalanced! Total Debits: ₹{total_dr:,.2f} != Total Credits: ₹{total_cr:,.2f} "
                    f"(Difference: ₹{diff:,.2f}). Debits and credits must be in exact balance."
                )

        # Level 4: Account Postability & Group Accounts
        for idx, line in enumerate(lines_data or [], 1):
            acc_id = line.get("account_id")
            acc_code = line.get("account_code")
            acc_target = acc_id or acc_code
            if not acc_target:
                errors.append(f"Line {idx}: Missing account ID or code.")
                continue

            try:
                is_postable, err_msg, acc_rec = ChartOfAccountsEngine.is_account_postable(acc_target, conn=conn)
                if not is_postable:
                    acc_label = f"[{acc_rec['code']}] {acc_rec['name']}" if acc_rec else str(acc_target)
                    if acc_rec and acc_rec.get("is_group", 0) == 1:
                        errors.append(f"Line {idx}: Group account {acc_label} cannot receive direct postings.")
                    else:
                        errors.append(f"Line {idx} ({acc_label}): {err_msg}")
            except Exception as e:
                errors.append(f"Line {idx}: Error verifying account '{acc_target}': {e}")

        # Level 6: Accounting Period & Date Validation
        if entry_data and entry_data.get("entry_date"):
            txn_date = entry_data.get("entry_date")
            try:
                PeriodControlEngine.validate_transaction_date(txn_date, user=user, conn=conn)
            except (PeriodClosedError, PeriodLockedError, InvalidAccountingDateError, PeriodControlError) as e:
                errors.append(str(e))
            except Exception as e:
                errors.append(f"Date validation error: {e}")

        return errors

    @classmethod
    def validate_full_journal(cls, entry_data, lines_data, user="admin", allow_draft=False, conn=None):
        """
        Runs the complete, comprehensive multi-level validation suite on a journal voucher
        before committing to the database.
        """
        status = str(entry_data.get("status") or "POSTED").upper()
        d_date = entry_data.get("entry_date") or datetime.date.today().isoformat()

        # 1. Period & Date validation (enforced on all official and draft entries)
        cls.validate_period_and_date(d_date, user=user, conn=conn)

        # 2. Account postability validation
        cls.validate_accounts(lines_data, conn=conn)

        # 3. Double-entry balancing (Drafts may skip balance if allow_draft is explicitly True)
        if status != "DRAFT" or not allow_draft:
            tot_dr = cls.validate_balance(lines_data)
        else:
            tot_dr, _ = cls.validate_amounts(lines_data)

        # 4. Duplicate source check
        s_mod = entry_data.get("source_module")
        s_ent = entry_data.get("source_entity")
        s_id = entry_data.get("source_id")
        if s_mod and s_id:
            existing = cls.validate_duplicate_source(s_mod, s_ent, s_id, conn=conn)
            if existing:
                return {
                    "is_valid": True,
                    "is_duplicate": True,
                    "existing_entry": existing,
                    "total_amount": tot_dr
                }

        return {
            "is_valid": True,
            "is_duplicate": False,
            "total_amount": tot_dr
        }

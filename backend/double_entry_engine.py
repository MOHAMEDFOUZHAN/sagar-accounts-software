import datetime
import decimal
import logging
from decimal import Decimal
from backend.db import get_db_connection, generate_unique_number
from backend.coa_engine import ChartOfAccountsEngine, InvalidAccountPostingError
from backend.period_engine import PeriodControlEngine, PeriodClosedError, PeriodLockedError, InvalidAccountingDateError

logger = logging.getLogger(__name__)

# Fixed 2-decimal monetary precision
ROUND_FACTOR = Decimal("0.01")


def to_decimal(val):
    if val is None or val == "":
        return Decimal("0.00")
    try:
        d = Decimal(str(val)).quantize(ROUND_FACTOR, rounding=decimal.ROUND_HALF_UP)
        return d
    except (decimal.InvalidOperation, ValueError, TypeError) as e:
        raise ValueError(f"Invalid monetary value '{val}': {e}")


class DoubleEntryError(ValueError):
    """Base exception for accounting validation and invariant errors."""
    pass


class UnbalancedJournalError(DoubleEntryError):
    """Raised when Total Debits does not equal Total Credits."""
    pass


class DuplicateJournalError(DoubleEntryError):
    """Raised when an automatic source transaction is posted more than once."""
    pass


class DoubleEntryEngine:
    """
    Central Double-Entry Accounting Engine.
    The sole authority for validating, balancing, and atomically posting journal entries.
    Guarantees: Total Debits == Total Credits, atomic DB transactions, idempotency,
    reversal integrity, and strict account postability.
    """

    @classmethod
    def post_journal_entry(
        cls,
        entry_data,
        lines_data,
        user="system",
        allow_draft=False,
        external_conn=None,
    ):
        """
        Validates and atomically posts a double-entry journal voucher.

        entry_data = {
            'entry_number': optional unique entry number string (auto-generated if omitted),
            'entry_date': datetime or date or ISO string (defaults to now),
            'source_module': 'sales' | 'inventory' | 'manual' | 'payroll' | 'banking',
            'source_entity': 'bill' | 'expense' | 'return' | 'purchase_invoice' | 'manual_voucher' | 'credit_payment' | 'supplier_payment',
            'source_id': optional unique source identifier (invoice_no, bill_id, etc.),
            'reference_no': optional external invoice/receipt reference,
            'narration': required descriptive accounting summary,
            'status': 'POSTED' (default) or 'DRAFT' (if allow_draft=True)
        }

        lines_data = [
            {
                'account_id': int OR 'account_code': str,
                'debit': Decimal / float / int,
                'credit': Decimal / float / int,
                'description': optional line narration,
                'party_type': optional 'customer' | 'supplier' | 'other',
                'party_id': optional party ID / phone / code,
                'party_name': optional party name,
                'tax_code': optional tax code,
                'tax_rate': optional tax percentage,
                'source_info': optional extra details
            },
            ...
        ]
        """
        # 1. Basic Structure Validations
        narration = str(entry_data.get("narration") or "").strip()
        if not narration:
            raise DoubleEntryError("Journal entry must have a non-empty narration / description.")

        if not lines_data or len(lines_data) < 2:
            raise DoubleEntryError("A valid journal entry must contain at least two lines (double-entry requirement).")

        source_module = str(entry_data.get("source_module") or "manual").strip().lower()
        source_entity = str(entry_data.get("source_entity") or "manual_voucher").strip().lower()
        source_id = str(entry_data.get("source_id") or "").strip() or None
        reference_no = str(entry_data.get("reference_no") or "").strip() or None

        # Parse Entry Date
        raw_date = entry_data.get("entry_date")
        if isinstance(raw_date, (datetime.datetime, datetime.date)):
            entry_date = raw_date
        elif raw_date:
            try:
                if " " in str(raw_date):
                    entry_date = datetime.datetime.strptime(str(raw_date).split(".")[0], "%Y-%m-%d %H:%M:%S")
                else:
                    entry_date = datetime.datetime.strptime(str(raw_date), "%Y-%m-%d")
            except Exception:
                entry_date = datetime.datetime.now()
        else:
            entry_date = datetime.datetime.now()

        status = str(entry_data.get("status") or "POSTED").upper()
        if status not in ("POSTED", "DRAFT"):
            status = "POSTED"
        if status == "DRAFT" and not allow_draft:
            status = "POSTED"

        # Manage DB Connection & Transaction Boundary
        should_close = False
        conn = external_conn
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            # 2. Idempotency Check (Duplicate Source Prevention)
            if source_id and source_module and source_entity and status == "POSTED":
                cur.execute("""
                    SELECT id, entry_number, status FROM journal_entries
                    WHERE source_module = %s AND source_entity = %s AND source_id = %s AND status = 'POSTED';
                """, (source_module, source_entity, source_id))
                existing_entry = cur.fetchone()
                if existing_entry:
                    logger.info(f"Idempotency hit: Journal already exists for {source_module}:{source_entity}:{source_id} -> {existing_entry['entry_number']}")
                    return {
                        "entry_id": existing_entry["id"],
                        "entry_number": existing_entry["entry_number"],
                        "status": existing_entry["status"],
                        "is_duplicate": True,
                    }

            # 2.5 Accounting Period & Financial Year Date Validation
            if status == "POSTED":
                PeriodControlEngine.validate_transaction_date(entry_date, user=user, conn=conn)

            # 3. Line-by-Line Validation & Debits/Credits Accumulation
            validated_lines = []
            total_debit = Decimal("0.00")
            total_credit = Decimal("0.00")

            for idx, raw_line in enumerate(lines_data, start=1):
                # Resolve Account
                account_id = raw_line.get("account_id")
                account_code = raw_line.get("account_code")
                target_ref = account_id if account_id is not None else account_code

                if not target_ref:
                    raise DoubleEntryError(f"Line {idx}: Account ID or Account Code is required.")

                is_postable, err_msg, account = ChartOfAccountsEngine.is_account_postable(target_ref, conn=conn)
                if not is_postable:
                    raise InvalidAccountPostingError(f"Line {idx}: {err_msg}")

                resolved_account_id = account["id"]

                # Parse amounts with fixed precision
                dr = to_decimal(raw_line.get("debit"))
                cr = to_decimal(raw_line.get("credit"))

                # Invariant: Line cannot have both debit and credit
                if dr > 0 and cr > 0:
                    raise DoubleEntryError(f"Line {idx} [{account['code']}]: Line cannot have both Debit (Rs. {dr}) and Credit (Rs. {cr}).")

                # Invariant: Line cannot have neither debit nor credit (must be > 0)
                if dr <= 0 and cr <= 0:
                    raise DoubleEntryError(f"Line {idx} [{account['code']}]: Line must have a non-zero positive Debit or Credit amount.")

                # Invariant: Negative amounts forbidden
                if dr < 0 or cr < 0:
                    raise DoubleEntryError(f"Line {idx} [{account['code']}]: Negative amounts are forbidden. Use offsetting lines.")

                total_debit += dr
                total_credit += cr

                validated_lines.append({
                    "account_id": resolved_account_id,
                    "account_code": account["code"],
                    "account_name": account["name"],
                    "debit": float(dr),
                    "credit": float(cr),
                    "description": str(raw_line.get("description") or "").strip() or None,
                    "party_type": str(raw_line.get("party_type") or "").strip() or None,
                    "party_id": str(raw_line.get("party_id") or "").strip() or None,
                    "party_name": str(raw_line.get("party_name") or "").strip() or None,
                    "tax_code": str(raw_line.get("tax_code") or "").strip() or None,
                    "tax_rate": float(raw_line.get("tax_rate") or 0.0),
                    "source_info": str(raw_line.get("source_info") or "").strip() or None,
                })

            # 4. Strict Balancing Invariant: TOTAL DEBITS = TOTAL CREDITS
            if total_debit != total_credit:
                diff = abs(total_debit - total_credit)
                raise UnbalancedJournalError(
                    f"Journal is unbalanced! Total Debits: Rs. {total_debit:,.2f} != Total Credits: Rs. {total_credit:,.2f} (Difference: Rs. {diff:,.2f})."
                )

            # 5. Generate / Assign Unique Entry Number
            entry_number = entry_data.get("entry_number")
            if entry_number:
                cur.execute("SELECT id FROM journal_entries WHERE entry_number = %s;", (entry_number,))
                if cur.fetchone():
                    prefix = entry_number.rsplit("-", 1)[0] if "-" in entry_number else f"JV-{source_module.upper()[:3]}"
                    entry_number = generate_unique_number(prefix, "journal_entries", "entry_number")
            else:
                prefix = f"JV-{source_module.upper()[:3]}"
                entry_number = generate_unique_number(prefix, "journal_entries", "entry_number")

            posting_date = datetime.datetime.now() if status == "POSTED" else None
            posted_by = user if status == "POSTED" else None
            is_opening = 1 if entry_data.get("is_opening") else 0

            # 6. Atomic Journal Insertion
            cur.execute("""
                INSERT INTO journal_entries
                    (entry_number, entry_date, posting_date, source_module, source_entity,
                     source_id, reference_no, narration, total_debit, total_credit,
                     status, created_by, posted_by, posted_at, is_opening)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (
                entry_number,
                entry_date,
                posting_date,
                source_module,
                source_entity,
                source_id,
                reference_no,
                narration,
                float(total_debit),
                float(total_credit),
                status,
                user,
                posted_by,
                posting_date,
                is_opening
            ))
            entry_id = cur.lastrowid

            # 7. Insert Journal Lines
            for line in validated_lines:
                cur.execute("""
                    INSERT INTO journal_lines
                        (entry_id, account_id, debit, credit, description,
                         party_type, party_id, party_name, tax_code, tax_rate, source_info)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    entry_id,
                    line["account_id"],
                    line["debit"],
                    line["credit"],
                    line["description"],
                    line["party_type"],
                    line["party_id"],
                    line["party_name"],
                    line["tax_code"],
                    line["tax_rate"],
                    line["source_info"]
                ))


            # Commit the atomic transaction
            conn.commit()
            logger.info(f"Journal Entry #{entry_number} (ID: {entry_id}) POSTED successfully. Balance: Rs. {total_debit:,.2f}")

            # Audit trail log
            try:
                from backend.audit_engine import AuditEngine
                AuditEngine.log_event(
                    action="POST",
                    entity_type="journal_entry",
                    entity_id=str(entry_id),
                    user=user,
                    new_value={"entry_number": entry_number, "total_debit": float(total_debit), "source_module": source_module, "reference_no": reference_no},
                    reason=narration
                )
            except Exception as audit_err:
                logger.warning(f"Audit log note for journal #{entry_number}: {audit_err}")

            return {
                "entry_id": entry_id,
                "entry_number": entry_number,
                "total_debit": float(total_debit),
                "total_credit": float(total_credit),
                "status": status,
                "lines_count": len(validated_lines),
                "is_duplicate": False,
            }

        except Exception as e:
            conn.rollback()
            logger.error(f"DoubleEntryEngine rolled back transaction: {e}")
            raise
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def reverse_journal(cls, entry_id, reason, user="system", reversal_date=None, external_conn=None):
        """
        Formal accounting reversal:
        1. Ensures original journal exists and is POSTED.
        2. Prevents double reversal.
        3. Creates a new mirror journal entry with debits & credits swapped.
        4. Validates reversal_date against accounting periods (must be OPEN).
        5. Links reversal_of_entry_id to the original journal.
        6. Marks original journal status as REVERSED.
        7. Preserves complete audit trail.
        """
        reason = str(reason or "").strip()
        if not reason:
            raise DoubleEntryError("A valid reversal reason must be provided.")

        should_close = False
        conn = external_conn
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            # 1. Fetch original entry
            cur.execute("""
                SELECT id, entry_number, entry_date, source_module, source_entity, source_id,
                       reference_no, narration, status, reversal_of_entry_id
                FROM journal_entries WHERE id = %s;
            """, (entry_id,))
            original = cur.fetchone()

            if not original:
                raise DoubleEntryError(f"Journal entry ID '{entry_id}' not found.")

            if original["status"] == "REVERSED":
                raise DoubleEntryError(f"Journal #{original['entry_number']} is already REVERSED. Double reversal is strictly forbidden.")

            if original["status"] != "POSTED":
                raise DoubleEntryError(f"Only POSTED journal entries can be reversed. Current status: {original['status']}.")

            # 2. Fetch original lines
            cur.execute("""
                SELECT account_id, debit, credit, description, party_type, party_id, party_name, tax_code, tax_rate
                FROM journal_lines WHERE entry_id = %s;
            """, (entry_id,))
            orig_lines = cur.fetchall()

            if not orig_lines:
                raise DoubleEntryError(f"Journal #{original['entry_number']} has no lines to reverse.")

            # 3. Create swapped reversal lines (Debits become Credits, Credits become Debits)
            reversal_lines = []
            for l in orig_lines:
                reversal_lines.append({
                    "account_id": l["account_id"],
                    "debit": l["credit"],  # Swapped
                    "credit": l["debit"],  # Swapped
                    "description": f"Reversal of line: {l['description'] or ''}".strip(),
                    "party_type": l["party_type"],
                    "party_id": l["party_id"],
                    "party_name": l["party_name"],
                    "tax_code": l["tax_code"],
                    "tax_rate": l["tax_rate"],
                    "source_info": f"Reversal line for entry #{original['entry_number']}"
                })

            reversal_entry_number = f"REV-{original['entry_number']}"
            effective_date = reversal_date if reversal_date else datetime.datetime.now()
            narration = f"REVERSAL of #{original['entry_number']}: {reason}"

            reversal_result = cls.post_journal_entry(
                entry_data={
                    "entry_number": reversal_entry_number,
                    "entry_date": effective_date,
                    "source_module": original["source_module"],
                    "source_entity": original["source_entity"],
                    "source_id": f"rev_{original['source_id']}" if original["source_id"] else None,
                    "reference_no": f"REV-{original['reference_no']}" if original["reference_no"] else None,
                    "narration": narration,
                    "status": "POSTED",
                },
                lines_data=reversal_lines,
                user=user,
                external_conn=conn
            )

            rev_id = reversal_result["entry_id"]

            # 4. Update Original Journal: Mark REVERSED, record reason and reversal reference
            cur.execute("""
                UPDATE journal_entries
                SET status = 'REVERSED', reversal_reason = %s
                WHERE id = %s;
            """, (reason, entry_id))

            # 5. Link reversal entry to original
            cur.execute("""
                UPDATE journal_entries
                SET reversal_of_entry_id = %s, reversal_reason = %s
                WHERE id = %s;
            """, (entry_id, reason, rev_id))

            # 6. Audit trail
            PeriodControlEngine.record_audit_log(
                cur=cur,
                user=user,
                action="JOURNAL_REVERSAL",
                entity_type="journal_entry",
                entity_id=entry_id,
                old_state={"status": "POSTED", "entry_number": original["entry_number"]},
                new_state={"status": "REVERSED", "reversal_entry_id": rev_id, "reversal_entry_number": reversal_entry_number},
                reason=reason
            )

            conn.commit()
            logger.info(f"Reversed Journal #{original['entry_number']} via Reversal #{reversal_entry_number} (ID: {rev_id})")

            return {
                "original_entry_id": entry_id,
                "reversal_entry_id": rev_id,
                "reversal_entry_number": reversal_entry_number,
                "status": "success",
                "message": f"Successfully reversed #{original['entry_number']} via #{reversal_entry_number}."
            }

        except Exception as e:
            conn.rollback()
            logger.error(f"Reversal failed for entry ID {entry_id}: {e}")
            raise
        finally:
            cur.close()
            if should_close:
                conn.close()

    reverse_journal_entry = reverse_journal

    @classmethod
    def correct_journal_entry(cls, entry_id, corrected_entry_data, corrected_lines_data, reason="Transaction Correction", user="admin", reversal_date=None, external_conn=None):
        """
        Controlled Accounting Correction:
        Original Transaction -> Reversal -> Corrected Transaction.
        Preserves 100% accounting history and complete audit trail.
        """
        reason = str(reason or "Transaction Correction").strip()
        should_close = False
        conn = external_conn
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            # Step 1: Execute reversal
            rev_res = cls.reverse_journal(
                entry_id=entry_id,
                reason=f"Correction replacement: {reason}",
                user=user,
                reversal_date=reversal_date,
                external_conn=conn
            )
            reversal_entry_id = rev_res["reversal_entry_id"]

            # Step 2: Prepare and post corrected transaction
            cur.execute("SELECT entry_number FROM journal_entries WHERE id = %s;", (entry_id,))
            orig_row = cur.fetchone()
            orig_num = orig_row["entry_number"] if orig_row else str(entry_id)

            c_data = dict(corrected_entry_data)
            if not c_data.get("reference_no"):
                c_data["reference_no"] = f"CORR-{orig_num}"
            if not c_data.get("narration"):
                c_data["narration"] = f"Corrected entry replacing #{orig_num}: {reason}"
            else:
                c_data["narration"] = f"{c_data['narration']} (replaces #{orig_num})"

            corrected_res = cls.post_journal_entry(
                entry_data=c_data,
                lines_data=corrected_lines_data,
                user=user,
                external_conn=conn
            )
            corrected_entry_id = corrected_res["entry_id"]

            # Step 3: Record correction audit trail
            PeriodControlEngine.record_audit_log(
                cur=cur,
                user=user,
                action="JOURNAL_CORRECTION",
                entity_type="journal_entry",
                entity_id=entry_id,
                old_state={"original_id": entry_id, "original_number": orig_num},
                new_state={
                    "reversal_entry_id": reversal_entry_id,
                    "reversal_entry_number": rev_res["reversal_entry_number"],
                    "corrected_entry_id": corrected_entry_id,
                    "corrected_entry_number": corrected_res["entry_number"]
                },
                reason=reason
            )

            conn.commit()
            return {
                "status": "success",
                "original_entry_id": entry_id,
                "reversal_entry_id": reversal_entry_id,
                "corrected_entry_id": corrected_entry_id,
                "corrected_entry_number": corrected_res["entry_number"],
                "message": f"Successfully corrected #{orig_num} via Reversal and New Journal #{corrected_res['entry_number']}."
            }
        except Exception as e:
            conn.rollback()
            logger.error(f"Correction failed for entry ID {entry_id}: {e}")
            raise
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_journal_entry(cls, entry_id, conn=None):
        """Fetches complete journal entry header and all line items."""
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT je.id, je.entry_number, je.entry_date, je.posting_date,
                       je.source_module, je.source_entity, je.source_id, je.reference_no,
                       je.narration, je.total_debit, je.total_credit, je.status,
                       je.created_by, je.posted_by, je.posted_at,
                       je.reversal_of_entry_id, je.reversal_reason, je.created_at
                FROM journal_entries je
                WHERE je.id = %s;
            """, (entry_id,))
            entry = cur.fetchone()
            if not entry:
                return None

            cur.execute("""
                SELECT jl.id, jl.account_id, jl.debit, jl.credit, jl.description,
                       jl.party_type, jl.party_id, jl.party_name, jl.tax_code, jl.tax_rate, jl.source_info,
                       ac.code AS account_code, ac.name AS account_name, ac.major_type, ac.sub_type, ac.normal_balance
                FROM journal_lines jl
                JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE jl.entry_id = %s
                ORDER BY jl.id ASC;
            """, (entry_id,))
            lines = cur.fetchall()

            entry["lines"] = lines
            return entry
        finally:
            cur.close()
            if should_close:
                conn.close()

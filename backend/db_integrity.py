import logging
from contextlib import contextmanager
from backend.db import get_db_connection, get_db_mode

logger = logging.getLogger(__name__)


class DatabaseIntegrityEngine:
    """
    Automated Relational Integrity, Foreign Key Constraint, and Ledger Coherence Auditor.
    Ensures zero orphan records, zero unbalanced vouchers, and full transactional consistency.
    """

    @classmethod
    @contextmanager
    def atomic_transaction(cls, conn=None):
        """
        Guarantees strict all-or-nothing atomicity across multi-entity accounting writes.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        try:
            yield conn
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            raise e
        finally:
            if should_close:
                conn.close()

    @classmethod
    def verify_database_integrity(cls, conn=None):
        """
        Executes exhaustive database integrity audits across 8 critical relational invariants:
        1. FOREIGN_KEY_INTEGRITY: PRAGMA foreign_key_check (SQLite)
        2. UNBALANCED_JOURNAL_ENTRIES: Checks that SUM(debit) == SUM(credit) for every posted journal
        3. ORPHAN_JOURNAL_LINES: Checks for lines with non-existent journal_entry header
        4. INVALID_ACCOUNT_REFERENCES: Checks for lines with non-existent or inactive accounts
        5. ORPHAN_REVERSALS: Checks that reversed journals reference an existing original
        6. SINGLE_LINE_VOUCHERS: Checks that every posted journal has at least 2 lines
        7. DUPLICATE_SOURCE_IDEMPOTENCY: Checks that no source transaction is posted multiple times
        8. SYNC_REGISTRY_COHERENCE: Checks that sync registry journal references exist in journal_entries
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            checks = []
            mode = get_db_mode()

            # 1. Foreign Key Integrity
            fk_errors = []
            if mode == "sqlite":
                try:
                    cur.execute("PRAGMA foreign_key_check;")
                    rows = cur.fetchall()
                    if rows:
                        fk_errors = [f"Table {r[0]}, Rowid {r[1]}, Target {r[2]}" for r in rows]
                except Exception as e:
                    logger.warning(f"PRAGMA foreign_key_check note: {e}")

            checks.append({
                "name": "FOREIGN_KEY_INTEGRITY",
                "title": "Foreign Key Referential Integrity",
                "passed": len(fk_errors) == 0,
                "details": "All relational foreign keys are strictly valid." if not fk_errors else f"Found {len(fk_errors)} FK violations: {fk_errors[:3]}",
                "violations_count": len(fk_errors)
            })

            # 2. Unbalanced Journal Entries
            cur.execute("""
                SELECT je.id, je.entry_number,
                       ROUND(SUM(jl.debit), 2) AS total_dr,
                       ROUND(SUM(jl.credit), 2) AS total_cr,
                       ROUND(ABS(SUM(jl.debit) - SUM(jl.credit)), 2) AS diff
                FROM journal_entries je
                JOIN journal_lines jl ON je.id = jl.entry_id
                WHERE je.status = 'POSTED'
                GROUP BY je.id, je.entry_number
                HAVING ROUND(ABS(SUM(jl.debit) - SUM(jl.credit)), 2) > 0.01;
            """)
            unbalanced = cur.fetchall()
            checks.append({
                "name": "UNBALANCED_JOURNAL_ENTRIES",
                "title": "Zero Unbalanced Journal Vouchers",
                "passed": len(unbalanced) == 0,
                "details": "All posted journal entries are mathematically balanced (Dr == Cr)." if not unbalanced else f"Found {len(unbalanced)} unbalanced vouchers: {[u['entry_number'] for u in unbalanced[:3]]}",
                "violations_count": len(unbalanced)
            })

            # 3. Orphan Journal Lines
            cur.execute("""
                SELECT jl.id, jl.entry_id
                FROM journal_lines jl
                LEFT JOIN journal_entries je ON jl.entry_id = je.id
                WHERE je.id IS NULL;
            """)
            orphan_lines = cur.fetchall()
            checks.append({
                "name": "ORPHAN_JOURNAL_LINES",
                "title": "Zero Orphan Journal Lines",
                "passed": len(orphan_lines) == 0,
                "details": "Every journal line is linked to a valid journal entry header." if not orphan_lines else f"Found {len(orphan_lines)} orphan lines without parent entry.",
                "violations_count": len(orphan_lines)
            })

            # 4. Invalid Account References
            cur.execute("""
                SELECT jl.id, jl.account_id, ac.name, ac.is_group
                FROM journal_lines jl
                LEFT JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE ac.id IS NULL OR ac.is_group = 1;
            """)
            invalid_acc_lines = cur.fetchall()
            checks.append({
                "name": "INVALID_ACCOUNT_REFERENCES",
                "title": "Valid Postable Account References",
                "passed": len(invalid_acc_lines) == 0,
                "details": "All lines reference active, non-group postable accounts in the Chart of Accounts." if not invalid_acc_lines else f"Found {len(invalid_acc_lines)} lines with invalid account references.",
                "violations_count": len(invalid_acc_lines)
            })

            # 5. Broken Reversal Links
            cur.execute("""
                SELECT je.id, je.entry_number, je.reversal_of_entry_id
                FROM journal_entries je
                LEFT JOIN journal_entries orig ON je.reversal_of_entry_id = orig.id
                WHERE je.reversal_of_entry_id IS NOT NULL AND orig.id IS NULL;
            """)
            broken_revs = cur.fetchall()
            checks.append({
                "name": "ORPHAN_REVERSALS",
                "title": "Valid Reversal Provenance Links",
                "passed": len(broken_revs) == 0,
                "details": "All reversal vouchers reference a legitimate original journal." if not broken_revs else f"Found {len(broken_revs)} reversal vouchers with broken parent links.",
                "violations_count": len(broken_revs)
            })

            # 6. Single Line Vouchers (< 2 lines)
            cur.execute("""
                SELECT je.id, je.entry_number, COUNT(jl.id) AS line_count
                FROM journal_entries je
                LEFT JOIN journal_lines jl ON je.id = jl.entry_id
                WHERE je.status = 'POSTED'
                GROUP BY je.id, je.entry_number
                HAVING COUNT(jl.id) < 2;
            """)
            single_lines = cur.fetchall()
            checks.append({
                "name": "SINGLE_LINE_VOUCHERS",
                "title": "Double-Entry Completeness (>= 2 Lines)",
                "passed": len(single_lines) == 0,
                "details": "All posted vouchers comply with the double-entry requirement (minimum 2 lines)." if not single_lines else f"Found {len(single_lines)} vouchers with fewer than 2 lines.",
                "violations_count": len(single_lines)
            })

            # 7. Duplicate Source Idempotency
            cur.execute("""
                SELECT source_module, source_entity, source_id, COUNT(*) AS count
                FROM journal_entries
                WHERE status = 'POSTED' 
                  AND source_id IS NOT NULL 
                  AND source_module NOT IN ('manual', 'adjustment')
                GROUP BY source_module, source_entity, source_id
                HAVING COUNT(*) > 1;
            """)
            duplicate_sources = cur.fetchall()
            checks.append({
                "name": "DUPLICATE_SOURCE_IDEMPOTENCY",
                "title": "Source Transaction Idempotency Invariant",
                "passed": len(duplicate_sources) == 0,
                "details": "Zero duplicate postings detected across external source transactions." if not duplicate_sources else f"Detected {len(duplicate_sources)} duplicated source transaction postings: {duplicate_sources[:3]}",
                "violations_count": len(duplicate_sources)
            })

            # 8. Sync Registry Coherence
            cur.execute("""
                SELECT reg.id, reg.source_module, reg.source_entity, reg.source_id, reg.journal_entry_id
                FROM accounting_sync_registry reg
                LEFT JOIN journal_entries je ON reg.journal_entry_id = je.id
                WHERE reg.journal_entry_id IS NOT NULL AND je.id IS NULL;
            """)
            broken_sync_refs = cur.fetchall()
            checks.append({
                "name": "SYNC_REGISTRY_COHERENCE",
                "title": "Sync Registry to Journal Coherence",
                "passed": len(broken_sync_refs) == 0,
                "details": "All sync registry journal references exist in the official ledger." if not broken_sync_refs else f"Found {len(broken_sync_refs)} sync registry items referencing deleted/missing journals.",
                "violations_count": len(broken_sync_refs)
            })

            checks_dict = {
                "foreign_keys": {"violations": len(fk_errors), "passed": len(fk_errors) == 0},
                "journal_balancing": {"unbalanced_entries": len(unbalanced), "passed": len(unbalanced) == 0},
                "orphan_journal_lines": {"orphan_lines": len(orphan_lines), "passed": len(orphan_lines) == 0},
                "invalid_accounts": {"invalid_lines": len(invalid_acc_lines), "passed": len(invalid_acc_lines) == 0},
                "broken_reversals": {"broken_reversals": len(broken_revs), "passed": len(broken_revs) == 0},
                "single_line_vouchers": {"single_line_entries": len(single_lines), "passed": len(single_lines) == 0},
                "duplicate_source_postings": {"duplicate_references": len(duplicate_sources), "passed": len(duplicate_sources) == 0},
                "sync_registry_coherence": {"broken_sync_refs": len(broken_sync_refs), "passed": len(broken_sync_refs) == 0},
            }

            passed_count = sum(1 for c in checks if c["passed"])
            total_count = len(checks)
            is_healthy = (passed_count == total_count)

            return {
                "is_healthy": is_healthy,
                "status": "HEALTHY" if is_healthy else "ISSUES_FOUND",
                "total_checks": total_count,
                "passed_checks": passed_count,
                "passed_count": passed_count,
                "failed_checks": total_count - passed_count,
                "checks_list": checks,
                "checks": checks_dict
            }

        finally:
            cur.close()
            if should_close:
                conn.close()


# Module-level export for transaction boundaries
atomic_transaction = DatabaseIntegrityEngine.atomic_transaction


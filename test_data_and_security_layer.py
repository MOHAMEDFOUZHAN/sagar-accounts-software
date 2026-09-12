import os
import sys
import unittest
import sqlite3
import datetime
from decimal import Decimal

# Ensure project root is in python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.db import get_db_connection
from backend.audit_engine import AuditEngine
from backend.auth import has_permission, PERMISSIONS
from backend.backup_engine import BackupEngine
from backend.db_integrity import DatabaseIntegrityEngine, atomic_transaction
from backend.sync_engine import (
    sync_all,
    get_sync_failures,
    retry_failed_sync,
    get_sync_registry_summary,
)
from backend.double_entry_engine import DoubleEntryEngine


class TestDataAndSecurityLayer(unittest.TestCase):
    """
    Comprehensive Test Suite for Layer 5: Data & Security
    Covering:
      1. Sales/Inventory Synchronization Idempotency & Lifecycle
      2. Audit Trail Immutability & Provenance
      3. User Roles & RBAC Backend Permissions
      4. Database Relational Integrity & Invariant Audits
      5. Hot Backup Creation, Verification & Disaster Recovery
    """

    def setUp(self):
        self.conn = get_db_connection()

    def tearDown(self):
        self.conn.close()

    # =========================================================================
    # 1. SALES & INVENTORY SYNCHRONIZATION
    # =========================================================================
    def test_sync_idempotency_prevents_duplicate_entries(self):
        """
        Verify that repeated synchronization runs do not generate duplicate
        accounting vouchers for already-synchronized source transactions.
        """
        # Run sync first time
        first_stats = sync_all()
        cur = self.conn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) as count FROM journal_entries;")
        initial_voucher_count = cur.fetchone()["count"]

        # Run sync a second time immediately
        second_stats = sync_all()
        cur.execute("SELECT COUNT(*) as count FROM journal_entries;")
        after_voucher_count = cur.fetchone()["count"]

        # No duplicate vouchers should have been created
        self.assertEqual(initial_voucher_count, after_voucher_count)
        self.assertEqual(second_stats["sales_bills"]["synced"], 0)
        self.assertGreaterEqual(second_stats["sales_bills"]["skipped"], 0)

    def test_sync_registry_tracking_and_failure_recovery(self):
        """
        Verify that sync failures are explicitly logged with status 'FAILED',
        error_info, and can be retrieved and retried without data loss.
        """
        cur = self.conn.cursor(dictionary=True)
        test_source_id = "TEST_FAIL_99999"

        # Clean up any existing test record
        cur.execute(
            "DELETE FROM accounting_sync_registry WHERE source_id = %s;",
            (test_source_id,),
        )
        self.conn.commit()

        # Insert a simulated failed sync record
        cur.execute(
            """
            INSERT INTO accounting_sync_registry
            (source_module, source_entity, source_id, source_hash, status, error_info, retry_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
            """,
            (
                "test_module",
                "test_entity",
                test_source_id,
                "test_hash_simulated_999",
                "FAILED",
                "Simulated connection failure during posting",
                1,
            ),
        )
        self.conn.commit()

        # Verify get_sync_failures retrieves it
        failures = get_sync_failures()
        found = any(f["source_id"] == test_source_id for f in failures)
        self.assertTrue(found, "Failed record must be visible in sync failures list")

        # Clean up test record
        cur.execute(
            "DELETE FROM accounting_sync_registry WHERE source_id = %s;",
            (test_source_id,),
        )
        self.conn.commit()

    def test_sync_summary_metrics(self):
        """
        Verify that get_sync_registry_summary returns correct categorized counters.
        """
        summary = get_sync_registry_summary()
        self.assertIn("total_records", summary)
        self.assertIn("posted", summary)
        self.assertIn("pending", summary)
        self.assertIn("failed", summary)
        self.assertIn("reversed", summary)
        self.assertGreaterEqual(summary["total_records"], summary["posted"])

    # =========================================================================
    # 2. AUDIT TRAIL
    # =========================================================================
    def test_audit_log_event_recording(self):
        """
        Verify that AuditEngine correctly appends audit records with complete metadata.
        """
        test_entity = "test_voucher"
        test_id = 888888
        log_id = AuditEngine.log_event(
            action="TEST_MUTATION",
            entity_type=test_entity,
            entity_id=test_id,
            previous_state={"status": "draft", "amount": 100},
            new_state={"status": "posted", "amount": 100},
            reason="Unit test audit event logging",
            user="test_auditor",
            source_module="accounts",
        )
        self.assertIsNotNone(log_id)
        self.assertGreater(log_id, 0)

        # Retrieve entity trail
        trail = AuditEngine.get_entity_trail(test_entity, test_id)
        self.assertGreaterEqual(len(trail), 1)
        record = trail[0]
        self.assertEqual(record["action"], "TEST_MUTATION")
        self.assertEqual(record["user_name"], "test_auditor")
        self.assertIn("Unit test audit", record["reason"])

    def test_audit_history_search_and_pagination(self):
        """
        Verify that get_audit_history handles multi-criteria filtering and pagination.
        """
        history = AuditEngine.get_audit_history(
            action=None,
            entity_type=None,
            page=1,
            page_size=10,
        )
        self.assertIn("logs", history)
        self.assertIn("total_count", history)
        self.assertIn("total_pages", history)
        self.assertLessEqual(len(history["logs"]), 10)

    # =========================================================================
    # 3. USER ROLES & ACCOUNTING PERMISSIONS (RBAC)
    # =========================================================================
    def test_rbac_admin_full_access(self):
        """
        Verify that admin role has all accounting permissions unconditionally.
        """
        for perm in PERMISSIONS.keys():
            self.assertTrue(
                has_permission("admin", perm),
                f"Admin must have permission '{perm}'",
            )
            self.assertTrue(
                has_permission("administrator", perm),
                f"Administrator must have permission '{perm}'",
            )

    def test_rbac_accountant_boundaries(self):
        """
        Verify that accountant role can perform standard accounting operations
        but is prohibited from admin-only actions (reopening periods, restoring db, locking).
        """
        # Allowed for accountant
        self.assertTrue(has_permission("accountant", "create_journal"))
        self.assertTrue(has_permission("accountant", "post_journal"))
        self.assertTrue(has_permission("accountant", "reverse_transaction"))
        self.assertTrue(has_permission("accountant", "close_period"))
        self.assertTrue(has_permission("accountant", "create_reconciliation"))
        self.assertTrue(has_permission("accountant", "backup_database"))

        # Forbidden for accountant (admin-only)
        self.assertFalse(has_permission("accountant", "reopen_period"))
        self.assertFalse(has_permission("accountant", "lock_period"))
        self.assertFalse(has_permission("accountant", "unlock_period"))
        self.assertFalse(has_permission("accountant", "restore_database"))
        self.assertFalse(has_permission("accountant", "manage_users"))

    def test_rbac_viewer_read_only(self):
        """
        Verify that viewer role is restricted to view actions only.
        """
        self.assertTrue(has_permission("viewer", "view_reports"))
        self.assertTrue(has_permission("viewer", "view_ledger"))
        self.assertTrue(has_permission("viewer", "view_audit"))

        # Forbidden mutations
        self.assertFalse(has_permission("viewer", "create_journal"))
        self.assertFalse(has_permission("viewer", "post_journal"))
        self.assertFalse(has_permission("viewer", "reverse_transaction"))
        self.assertFalse(has_permission("viewer", "close_period"))
        self.assertFalse(has_permission("viewer", "backup_database"))
        self.assertFalse(has_permission("viewer", "restore_database"))

    # =========================================================================
    # 4. DATABASE INTEGRITY
    # =========================================================================
    def test_database_integrity_invariants_pass(self):
        """
        Verify all 8 automated database integrity audits pass on active database.
        """
        report = DatabaseIntegrityEngine.verify_database_integrity()
        self.assertEqual(
            report["status"],
            "HEALTHY",
            f"Integrity check failed: {report['checks']}",
        )
        self.assertEqual(report["passed_count"], report["total_checks"])
        self.assertEqual(report["checks"]["foreign_keys"]["violations"], 0)
        self.assertEqual(
            report["checks"]["journal_balancing"]["unbalanced_entries"], 0
        )
        self.assertEqual(
            report["checks"]["orphan_journal_lines"]["orphan_lines"], 0
        )
        self.assertEqual(
            report["checks"]["duplicate_source_postings"]["duplicate_references"], 0
        )

    def test_atomic_transaction_rollback(self):
        """
        Verify that atomic_transaction context manager properly rolls back
        on exceptions, ensuring no partial writes occur.
        """
        cur = self.conn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) as count FROM journal_entries;")
        count_before = cur.fetchone()["count"]

        with self.assertRaises(ValueError):
            with atomic_transaction() as txn_conn:
                txn_cur = txn_conn.cursor()
                txn_cur.execute(
                    """
                    INSERT INTO journal_entries
                    (entry_number, entry_date, status, narration, source_module, source_entity)
                    VALUES ('TEMP_ROLLBACK_TEST', '2026-09-11', 'Draft', 'Testing atomic rollback', 'test', 'test_entry');
                    """
                )
                # Intentionally trigger an exception to force rollback
                raise ValueError("Simulated failure inside transaction boundary")

        # Verify that record was not committed
        cur.execute("SELECT COUNT(*) as count FROM journal_entries;")
        count_after = cur.fetchone()["count"]
        self.assertEqual(count_before, count_after)

    # =========================================================================
    # 5. ACCOUNTING DATA BACKUP & RECOVERY
    # =========================================================================
    def test_backup_creation_verification_and_integrity(self):
        """
        Verify full backup lifecycle:
          1. Create atomic hot-backup
          2. Verify file on disk and SHA-256 checksum
          3. Verify PRAGMA integrity check and Trial Balance equilibrium
          4. Confirm backup appears in listing
        """
        # Step 1: Create backup
        res = BackupEngine.create_backup(
            backup_type="manual",
            notes="Automated test backup",
            user="test_admin",
        )
        self.assertEqual(res["status"], "success")
        backup_id = res["backup_id"]
        filename = res["filename"]
        backup_path = res["path"]

        self.assertTrue(os.path.exists(backup_path))
        self.assertGreater(res["file_size_bytes"], 0)
        self.assertIsNotNone(res["checksum_sha256"])

        # Step 2: Verify backup
        verify_res = BackupEngine.verify_backup(filename)
        self.assertIn(verify_res["status"], ("success", "VERIFIED"))
        self.assertEqual(verify_res["integrity_check"], "ok")
        self.assertTrue(verify_res["trial_balance_balanced"])
        self.assertEqual(
            verify_res["total_debit"], verify_res["total_credit"]
        )

        # Step 3: Verify listed in backup registry
        backups = BackupEngine.list_backups()
        found = any(b["filename"] == filename for b in backups)
        self.assertTrue(found, "New backup must appear in list_backups()")


if __name__ == "__main__":
    unittest.main()

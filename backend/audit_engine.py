import json
import datetime
import logging
from backend.db import get_db_connection

logger = logging.getLogger(__name__)


class AuditEngine:
    """
    Centralized, append-only Accounting Audit Trail Engine.
    Tracks all financial data modifications, status lifecycle transitions,
    period control operations, configuration updates, and disaster recovery events.
    """

    ACTIONS = {
        "CREATE": "Created record or draft",
        "EDIT": "Modified record fields",
        "SUBMIT": "Submitted for approval / review",
        "APPROVE": "Approved transaction / entry",
        "POST": "Posted official double-entry journal voucher",
        "REVERSE": "Reversed transaction with contra voucher",
        "CANCEL": "Cancelled pending or draft transaction",
        "CORRECT": "Atomic correction pipeline (Reversal + New voucher)",
        "PERIOD_CLOSE": "Closed accounting period",
        "PERIOD_REOPEN": "Reopened closed accounting period",
        "PERIOD_LOCK": "Applied statutory lock to accounting period",
        "PERIOD_UNLOCK": "Removed statutory lock from accounting period",
        "OPENING_BALANCE_CREATE": "Recorded opening balances",
        "OPENING_BALANCE_FINALIZE": "Finalized opening balance entry",
        "RECONCILIATION_CREATE": "Initialized reconciliation worksheet",
        "RECONCILIATION_MATCH": "Matched ledger transaction against statement",
        "RECONCILIATION_ADJUST": "Posted reconciliation adjustment voucher",
        "CONFIG_CHANGE": "Modified chart of accounts or system parameters",
        "MAPPING_CHANGE": "Updated system account mapping",
        "TAX_CHANGE": "Updated statutory tax rates or classification",
        "SYNC_EXECUTE": "Executed Sales/Inventory synchronization",
        "SYNC_RETRY": "Retried failed synchronization transaction",
        "BACKUP_CREATE": "Created database backup snapshot",
        "BACKUP_RESTORE": "Restored database from verified backup"
    }

    @classmethod
    def log_event(
        cls,
        action,
        entity_type,
        entity_id,
        user="system",
        old_value=None,
        new_value=None,
        reason=None,
        ip_address="127.0.0.1",
        related_ref=None,
        source_module="accounts",
        conn=None,
        previous_state=None,
        new_state=None,
        **kwargs
    ):
        """
        Appends an immutable audit event to accounting_audit_trail.
        Atomic and safe against transaction rollbacks when conn is passed.
        """
        if old_value is None and previous_state is not None:
            old_value = previous_state
        if new_value is None and new_state is not None:
            new_value = new_state

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            # Serialize JSON payloads if dict or list
            if isinstance(old_value, (dict, list)):
                old_val_str = json.dumps(old_value, default=str)
            elif old_value is not None:
                old_val_str = str(old_value)
            else:
                old_val_str = None

            if isinstance(new_value, (dict, list)):
                new_val_str = json.dumps(new_value, default=str)
            elif new_value is not None:
                new_val_str = str(new_value)
            else:
                new_val_str = None

            user_str = str(user or "system").strip()
            action_norm = str(action or "POST").upper()
            entity_type_norm = str(entity_type or "unknown").lower()
            entity_id_str = str(entity_id or "") if entity_id is not None else None

            # Incorporate related reference or source module into reason if helpful
            reason_str = reason
            if related_ref and (not reason_str or f"Ref: {related_ref}" not in reason_str):
                prefix = f"[Ref: {related_ref}] "
                reason_str = prefix + (reason_str or "")

            cur.execute("""
                INSERT INTO accounting_audit_trail
                    (action, entity_type, entity_id, old_value, new_value, reason, user, ip_address, created_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP);
            """, (
                action_norm,
                entity_type_norm,
                entity_id_str,
                old_val_str,
                new_val_str,
                reason_str,
                user_str,
                str(ip_address or "127.0.0.1")
            ))

            if should_close:
                conn.commit()

            return True

        except Exception as e:
            logger.error(f"AuditEngine failed to log event '{action}' on {entity_type} #{entity_id}: {e}")
            return False
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_audit_history(
        cls,
        entity_type=None,
        entity_id=None,
        user=None,
        action=None,
        start_date=None,
        end_date=None,
        search=None,
        page=1,
        page_size=50,
        conn=None
    ):
        """
        Retrieves paginated audit log entries with multi-attribute filtering.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            conditions = ["1=1"]
            params = []

            if entity_type:
                conditions.append("entity_type = %s")
                params.append(str(entity_type).lower())

            if entity_id:
                conditions.append("entity_id = %s")
                params.append(str(entity_id))

            if user:
                conditions.append("LOWER(user) = %s")
                params.append(str(user).lower())

            if action:
                conditions.append("action = %s")
                params.append(str(action).upper())

            if start_date:
                conditions.append("DATE(created_at) >= %s")
                params.append(str(start_date))

            if end_date:
                conditions.append("DATE(created_at) <= %s")
                params.append(str(end_date))

            if search:
                search_term = f"%{search}%"
                conditions.append("(reason LIKE %s OR entity_id LIKE %s OR old_value LIKE %s OR new_value LIKE %s)")
                params.extend([search_term, search_term, search_term, search_term])

            where_clause = " AND ".join(conditions)

            # Count total
            cur.execute(f"SELECT COUNT(*) AS total FROM accounting_audit_trail WHERE {where_clause};", tuple(params))
            row = cur.fetchone()
            total_count = row["total"] if isinstance(row, dict) else (row[0] if row else 0)

            # Fetch page
            page = max(1, int(page))
            page_size = max(1, min(200, int(page_size)))
            offset = (page - 1) * page_size

            query_params = list(params) + [page_size, offset]
            cur.execute(f"""
                SELECT id, action, entity_type, entity_id, old_value, new_value, reason, user, ip_address, created_at
                FROM accounting_audit_trail
                WHERE {where_clause}
                ORDER BY id DESC
                LIMIT %s OFFSET %s;
            """, tuple(query_params))

            logs = cur.fetchall()

            for log in logs:
                if isinstance(log.get("created_at"), datetime.datetime):
                    log["created_at"] = log["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                log["user_name"] = log.get("user")
                log["user_id"] = log.get("user")
                log["previous_state"] = log.get("old_value")
                log["new_state"] = log.get("new_value")

            total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1

            return {
                "logs": logs,
                "total_count": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages
            }

        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_entity_trail(cls, entity_type, entity_id, conn=None):
        """
        Retrieves complete chronological lifecycle trail for a specific transaction / entity.
        Supports drill-down provenance: Transaction -> Journal Entry -> Audit History.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT id, action, entity_type, entity_id, old_value, new_value, reason, user, ip_address, created_at
                FROM accounting_audit_trail
                WHERE entity_type = %s AND entity_id = %s
                ORDER BY id ASC;
            """, (str(entity_type).lower(), str(entity_id)))

            trail = cur.fetchall()
            for t in trail:
                if isinstance(t.get("created_at"), datetime.datetime):
                    t["created_at"] = t["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                t["user_name"] = t.get("user")
                t["user_id"] = t.get("user")
                t["previous_state"] = t.get("old_value")
                t["new_state"] = t.get("new_value")

            return trail

        finally:
            cur.close()
            if should_close:
                conn.close()

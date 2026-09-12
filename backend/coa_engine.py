import logging
from backend.db import get_db_connection

logger = logging.getLogger(__name__)

VALID_MAJOR_TYPES = {
    "Asset",
    "Liability",
    "Equity",
    "Revenue",
    "Direct Expense",
    "Operating Expense",
    "Financial Cost",
    "Tax",
}

VALID_NORMAL_BALANCES = {"Debit", "Credit"}


class ChartOfAccountsError(ValueError):
    """Base exception for Chart of Accounts errors."""
    pass


class DuplicateAccountCodeError(ChartOfAccountsError):
    """Raised when an account code already exists."""
    pass


class InvalidParentAccountError(ChartOfAccountsError):
    """Raised when the specified parent account does not exist or is invalid."""
    pass


class CircularHierarchyError(ChartOfAccountsError):
    """Raised when a parent relationship creates a circular cycle."""
    pass


class InvalidAccountPostingError(ChartOfAccountsError):
    """Raised when attempting to post to an unpostable, group, or inactive account."""
    pass


class ChartOfAccountsEngine:
    """
    Central service for Hierarchical Chart of Accounts (COA) management,
    traversal, validation, and postability verification.
    """

    @staticmethod
    def get_account_by_id(account_id, conn=None):
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT id, code, name, major_type, sub_type, description, is_active,
                       parent_id, normal_balance, is_group, is_postable, system_tag,
                       tax_classification, created_at, updated_at
                FROM accounts_chart WHERE id = %s;
            """, (account_id,))
            return cur.fetchone()
        finally:
            cur.close()
            if should_close:
                conn.close()

    @staticmethod
    def get_account_by_code(code, conn=None):
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT id, code, name, major_type, sub_type, description, is_active,
                       parent_id, normal_balance, is_group, is_postable, system_tag,
                       tax_classification, created_at, updated_at
                FROM accounts_chart WHERE code = %s;
            """, (str(code).strip(),))
            return cur.fetchone()
        finally:
            cur.close()
            if should_close:
                conn.close()

    @staticmethod
    def is_account_postable(account_id_or_code, conn=None):
        """
        Enforces strict accounting invariant:
        Only valid, active, non-group ledger accounts may receive journal lines.
        Returns: (is_postable: bool, error_message: str or None, account_record: dict or None)
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            account = None
            if isinstance(account_id_or_code, int):
                cur.execute("SELECT * FROM accounts_chart WHERE id = %s;", (account_id_or_code,))
                account = cur.fetchone()
            else:
                raw_str = str(account_id_or_code).strip()
                # Check by code first (codes are often 4-digit strings like '1000', '1010')
                cur.execute("SELECT * FROM accounts_chart WHERE code = %s;", (raw_str,))
                account = cur.fetchone()
                if not account and raw_str.isdigit():
                    cur.execute("SELECT * FROM accounts_chart WHERE id = %s;", (int(raw_str),))
                    account = cur.fetchone()

            if not account:
                return False, f"Account '{account_id_or_code}' does not exist in Chart of Accounts.", None

            if not account.get("is_active", 1):
                return False, f"Account [{account['code']}] '{account['name']}' is INACTIVE and cannot receive postings.", account

            if account.get("is_group", 0) == 1:
                return False, f"Group account [{account['code']}] '{account['name']}' cannot receive direct postings.", account

            if account.get("is_postable", 1) == 0:
                return False, f"Account [{account['code']}] '{account['name']}' is marked as non-postable.", account

            return True, None, account
        finally:
            cur.close()
            if should_close:
                conn.close()

    @staticmethod
    def validate_account(code, name, major_type, normal_balance, parent_id=None, account_id=None, is_group=0, conn=None):
        """
        Validates account creation/update:
        - Unique code
        - Valid major type
        - Valid normal balance
        - Non-empty name
        - Circular parent hierarchy prevention
        """
        code = str(code).strip()
        name = str(name).strip()

        if not code:
            raise ValueError("Account code cannot be empty.")
        if not name:
            raise ValueError("Account name cannot be empty.")
        if major_type not in VALID_MAJOR_TYPES:
            raise ValueError(f"Invalid account type '{major_type}'. Must be one of: {', '.join(sorted(VALID_MAJOR_TYPES))}")
        if normal_balance not in VALID_NORMAL_BALANCES:
            raise ValueError(f"Invalid normal balance '{normal_balance}'. Must be 'Debit' or 'Credit'.")

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            # 1. Check duplicate code
            if account_id:
                cur.execute("SELECT id FROM accounts_chart WHERE code = %s AND id != %s;", (code, account_id))
            else:
                cur.execute("SELECT id FROM accounts_chart WHERE code = %s;", (code,))
            if cur.fetchone():
                raise DuplicateAccountCodeError(f"Account code '{code}' already exists. Account codes must be unique.")

            # 2. Check parent account validity & prevent circular hierarchy
            if parent_id:
                if account_id and parent_id == account_id:
                    raise CircularHierarchyError("An account cannot be its own parent.")

                cur.execute("SELECT id, code, is_group, parent_id FROM accounts_chart WHERE id = %s;", (parent_id,))
                parent = cur.fetchone()
                if not parent:
                    raise InvalidParentAccountError(f"Parent account ID '{parent_id}' does not exist.")

                # Traverse parent chain to prevent cycles
                current_p_id = parent["parent_id"]
                visited = {parent_id}
                while current_p_id:
                    if account_id and current_p_id == account_id:
                        raise CircularHierarchyError("Circular hierarchy detected: account cannot be an ancestor of its parent.")
                    if current_p_id in visited:
                        raise CircularHierarchyError("Circular hierarchy detected in existing parent tree.")
                    visited.add(current_p_id)
                    cur.execute("SELECT parent_id FROM accounts_chart WHERE id = %s;", (current_p_id,))
                    p_row = cur.fetchone()
                    current_p_id = p_row["parent_id"] if p_row else None

        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def create_account(cls, code, name, major_type, sub_type="", description="", parent_id=None,
                       normal_balance="Debit", is_group=0, is_postable=1, system_tag=None, tax_classification=None, conn=None):
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cls.validate_account(code, name, major_type, normal_balance, parent_id=parent_id, is_group=is_group, conn=conn)

            cur.execute("""
                INSERT INTO accounts_chart 
                    (code, name, major_type, sub_type, description, parent_id, normal_balance,
                     is_group, is_postable, system_tag, tax_classification, is_active)
                VALUES 
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1);
            """, (code.strip(), name.strip(), major_type, sub_type.strip(), description.strip(),
                  parent_id, normal_balance, 1 if is_group else 0, 0 if is_group else (1 if is_postable else 0),
                  system_tag, tax_classification))
            acc_id = cur.lastrowid
            conn.commit()
            logger.info(f"Created account [{code}] '{name}' (ID: {acc_id})")
            return acc_id
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def update_account(cls, account_id, name=None, sub_type=None, description=None, parent_id=None,
                       is_active=None, is_postable=None, normal_balance=None, conn=None):
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            account = cls.get_account_by_id(account_id, conn=conn)
            if not account:
                raise ValueError(f"Account ID '{account_id}' not found.")

            upd_name = name.strip() if name is not None else account["name"]
            upd_sub_type = sub_type.strip() if sub_type is not None else account["sub_type"]
            upd_desc = description.strip() if description is not None else account["description"]
            upd_parent_id = parent_id if parent_id is not None else account["parent_id"]
            upd_norm_bal = normal_balance if normal_balance is not None else account["normal_balance"]
            upd_active = 1 if is_active else 0 if is_active is not None else account["is_active"]
            upd_postable = 1 if is_postable else 0 if is_postable is not None else account["is_postable"]

            cls.validate_account(
                account["code"], upd_name, account["major_type"], upd_norm_bal,
                parent_id=upd_parent_id, account_id=account_id, is_group=account["is_group"], conn=conn
            )

            cur.execute("""
                UPDATE accounts_chart
                SET name = %s, sub_type = %s, description = %s, parent_id = %s,
                    normal_balance = %s, is_active = %s, is_postable = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
            """, (upd_name, upd_sub_type, upd_desc, upd_parent_id, upd_norm_bal, upd_active, upd_postable, account_id))
            conn.commit()
            return cls.get_account_by_id(account_id, conn=conn)
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def archive_account(cls, account_id, conn=None):
        """
        Enforces preservation of historical accounting data:
        If account has any journal lines, it CANNOT be physically deleted.
        It is soft-archived by setting is_active = 0.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            account = cls.get_account_by_id(account_id, conn=conn)
            if not account:
                raise ValueError(f"Account ID '{account_id}' not found.")

            # Check if used in journal entries
            cur.execute("SELECT COUNT(*) AS cnt FROM journal_lines WHERE account_id = %s;", (account_id,))
            jl_count = cur.fetchone()["cnt"]

            if jl_count > 0:
                # Soft deactivate to preserve audit trail
                cur.execute("UPDATE accounts_chart SET is_active = 0 WHERE id = %s;", (account_id,))
                conn.commit()
                logger.info(f"Account [{account['code']}] '{account['name']}' has {jl_count} transactions. Soft-archived (is_active=0).")
                return {"action": "archived", "message": f"Account has {jl_count} historical transactions. Deactivated safely without data loss."}
            else:
                # Safe to remove only if zero transactions exist
                cur.execute("DELETE FROM accounts_chart WHERE id = %s;", (account_id,))
                conn.commit()
                logger.info(f"Account [{account['code']}] '{account['name']}' had 0 transactions. Safely deleted.")
                return {"action": "deleted", "message": "Account had 0 transactions and was removed."}
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            if should_close:
                conn.close()

    delete_account = archive_account

    @staticmethod
    def get_accounts(is_group=None, major_type=None, is_active=True):
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            conditions = []
            params = []
            if is_active is not None:
                conditions.append("ac.is_active = %s")
                params.append(1 if is_active else 0)
            if is_group is not None:
                conditions.append("ac.is_group = %s")
                params.append(1 if is_group else 0)
            if major_type:
                conditions.append("ac.major_type = %s")
                params.append(major_type)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            query = f"""
                SELECT ac.id, ac.code, ac.name, ac.major_type, ac.sub_type, ac.description,
                       ac.parent_id, ac.normal_balance, ac.is_group, ac.is_postable,
                       ac.system_tag, ac.tax_classification, ac.is_active,
                       p.code AS parent_code, p.name AS parent_name
                FROM accounts_chart ac
                LEFT JOIN accounts_chart p ON ac.parent_id = p.id
                {where_clause}
                ORDER BY ac.code ASC;
            """
            cur.execute(query, tuple(params))
            return cur.fetchall()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_all_accounts(include_inactive=False, major_type=None):
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            conditions = []
            params = []
            if not include_inactive:
                conditions.append("ac.is_active = 1")
            if major_type:
                conditions.append("ac.major_type = %s")
                params.append(major_type)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            query = f"""
                SELECT ac.id, ac.code, ac.name, ac.major_type, ac.sub_type, ac.description,
                       ac.parent_id, ac.normal_balance, ac.is_group, ac.is_postable,
                       ac.system_tag, ac.tax_classification, ac.is_active,
                       p.code AS parent_code, p.name AS parent_name
                FROM accounts_chart ac
                LEFT JOIN accounts_chart p ON ac.parent_id = p.id
                {where_clause}
                ORDER BY ac.code ASC;
            """
            cur.execute(query, tuple(params))
            return cur.fetchall()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_postable_accounts(major_type=None):
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            params = []
            type_cond = ""
            if major_type:
                type_cond = "AND major_type = %s"
                params.append(major_type)

            cur.execute(f"""
                SELECT id, code, name, major_type, sub_type, normal_balance, system_tag
                FROM accounts_chart
                WHERE is_active = 1 AND is_group = 0 AND is_postable = 1 {type_cond}
                ORDER BY code ASC;
            """, tuple(params))
            return cur.fetchall()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_hierarchical_tree():
        """
        Builds a full nested tree structure of the Chart of Accounts:
        Groups -> Subgroups -> Leaf Postable Accounts.
        """
        all_accounts = ChartOfAccountsEngine.get_all_accounts(include_inactive=False)
        by_id = {acc["id"]: {**acc, "children": []} for acc in all_accounts}
        root_nodes = []

        for acc in all_accounts:
            node = by_id[acc["id"]]
            p_id = acc["parent_id"]
            if p_id and p_id in by_id:
                by_id[p_id]["children"].append(node)
            else:
                root_nodes.append(node)

        return root_nodes

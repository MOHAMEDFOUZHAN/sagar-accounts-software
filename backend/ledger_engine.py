import datetime
import logging
from backend.db import get_db_connection
from backend.coa_engine import ChartOfAccountsEngine

logger = logging.getLogger(__name__)


class LedgerEngine:
    """
    Central General Ledger & Account Balances Engine.
    Reads exclusively from POSTED journal entries and lines.
    Calculates Opening Balance, Chronological Running Balances, Closing Balance,
    and provides complete drill-down audit traceability back to source transactions.
    """

    @staticmethod
    def get_account_balance(account_id, as_of_date=None, conn=None):
        """
        Computes accurate net balance of an account based on its normal balance (Debit or Credit).
        Only POSTED journal entries are included.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT id, code, name, major_type, sub_type, normal_balance, is_group, is_postable
                FROM accounts_chart WHERE id = %s;
            """, (account_id,))
            acc = cur.fetchone()
            if not acc:
                return None

            date_cond = ""
            params = [account_id]
            if as_of_date:
                date_cond = "AND DATE(je.entry_date) <= %s"
                params.append(as_of_date)

            query = f"""
                SELECT COALESCE(SUM(jl.debit), 0.0) AS total_debit,
                       COALESCE(SUM(jl.credit), 0.0) AS total_credit
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE jl.account_id = %s AND je.status IN ('POSTED', 'REVERSED') {date_cond};
            """
            cur.execute(query, tuple(params))
            row = cur.fetchone()

            total_dr = round(float(row["total_debit"] or 0.0), 2)
            total_cr = round(float(row["total_credit"] or 0.0), 2)

            norm_bal = acc["normal_balance"]
            if norm_bal == "Debit":
                net_balance = round(total_dr - total_cr, 2)
            else:
                net_balance = round(total_cr - total_dr, 2)

            return {
                "account_id": acc["id"],
                "code": acc["code"],
                "name": acc["name"],
                "major_type": acc["major_type"],
                "sub_type": acc["sub_type"],
                "normal_balance": norm_bal,
                "total_debit": total_dr,
                "total_credit": total_cr,
                "balance": net_balance,
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @staticmethod
    def get_all_account_balances(as_of_date=None, include_zero=False):
        """
        Computes balances for all active postable accounts.
        """
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            date_cond = ""
            params = []
            if as_of_date:
                date_cond = "AND DATE(je.entry_date) <= %s"
                params.append(as_of_date)

            query = f"""
                SELECT ac.id, ac.code, ac.name, ac.major_type, ac.sub_type, ac.normal_balance,
                       COALESCE(SUM(jl.debit), 0.0) AS total_debit,
                       COALESCE(SUM(jl.credit), 0.0) AS total_credit
                FROM accounts_chart ac
                LEFT JOIN journal_lines jl ON ac.id = jl.account_id
                LEFT JOIN journal_entries je ON jl.entry_id = je.id AND je.status IN ('POSTED', 'REVERSED') {date_cond}
                WHERE ac.is_active = 1 AND ac.is_group = 0
                GROUP BY ac.id, ac.code, ac.name, ac.major_type, ac.sub_type, ac.normal_balance
                ORDER BY ac.code ASC;
            """
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

            balances = []
            for r in rows:
                dr = round(float(r["total_debit"] or 0.0), 2)
                cr = round(float(r["total_credit"] or 0.0), 2)
                norm_bal = r["normal_balance"]
                net = round(dr - cr if norm_bal == "Debit" else cr - dr, 2)

                if not include_zero and dr == 0.0 and cr == 0.0 and net == 0.0:
                    continue

                balances.append({
                    "account_id": r["id"],
                    "code": r["code"],
                    "name": r["name"],
                    "major_type": r["major_type"],
                    "sub_type": r["sub_type"],
                    "normal_balance": norm_bal,
                    "total_debit": dr,
                    "total_credit": cr,
                    "balance": net,
                })

            return balances
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_ledger_statement(account_id=None, start_date=None, end_date=None, filters=None):
        """
        Generates a comprehensive General Ledger statement:
        1. Opening Balance: Sum of all posted movements before start_date.
        2. Period Movements: Chronological list of debit/credit entries with Running Balance.
        3. Closing Balance = Opening Balance + Period Net Movement.
        """
        filters = filters or {}
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)

        try:
            account = None
            norm_bal = "Debit"
            if account_id:
                account = ChartOfAccountsEngine.get_account_by_id(account_id, conn=conn)
                if account:
                    norm_bal = account.get("normal_balance", "Debit")

            # 1. Calculate Opening Balance (Prior to start_date)
            opening_balance = 0.0
            total_prior_debit = 0.0
            total_prior_credit = 0.0

            if start_date and account_id:
                cur.execute("""
                    SELECT COALESCE(SUM(jl.debit), 0.0) AS prior_dr,
                           COALESCE(SUM(jl.credit), 0.0) AS prior_cr
                    FROM journal_lines jl
                    JOIN journal_entries je ON jl.entry_id = je.id
                    WHERE jl.account_id = %s AND je.status IN ('POSTED', 'REVERSED') AND DATE(je.entry_date) < %s;
                """, (account_id, start_date))
                prior_row = cur.fetchone()
                total_prior_debit = float(prior_row["prior_dr"] or 0.0)
                total_prior_credit = float(prior_row["prior_cr"] or 0.0)

                if norm_bal == "Debit":
                    opening_balance = round(total_prior_debit - total_prior_credit, 2)
                else:
                    opening_balance = round(total_prior_credit - total_prior_debit, 2)

            # 2. Fetch Period Journal Lines
            conditions = ["je.status IN ('POSTED', 'REVERSED')"]
            params = []

            if account_id:
                conditions.append("jl.account_id = %s")
                params.append(account_id)

            if start_date:
                conditions.append("DATE(je.entry_date) >= %s")
                params.append(start_date)

            if end_date:
                conditions.append("DATE(je.entry_date) <= %s")
                params.append(end_date)

            if filters.get("source_module"):
                conditions.append("je.source_module = %s")
                params.append(filters["source_module"])

            if filters.get("search"):
                s = f"%{filters['search'].strip()}%"
                conditions.append("(je.entry_number LIKE %s OR je.narration LIKE %s OR je.reference_no LIKE %s OR jl.party_name LIKE %s)")
                params.extend([s, s, s, s])

            where_clause = " AND ".join(conditions)

            query = f"""
                SELECT jl.id AS line_id, jl.entry_id, jl.account_id,
                       jl.debit, jl.credit, jl.description AS line_description,
                       jl.party_type, jl.party_id, jl.party_name, jl.tax_code, jl.tax_rate,
                       je.entry_number, je.entry_date, je.posting_date,
                       je.source_module, je.source_entity, je.source_id, je.reference_no,
                       je.narration, je.created_by,
                       ac.code AS account_code, ac.name AS account_name, ac.major_type, ac.sub_type, ac.normal_balance
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id = je.id
                JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE {where_clause}
                ORDER BY je.entry_date ASC, je.id ASC, jl.id ASC;
            """
            cur.execute(query, tuple(params))
            raw_lines = cur.fetchall()

            # 3. Compute Chronological Running Balance
            period_debit = 0.0
            period_credit = 0.0
            running_balance = opening_balance
            lines = []

            for row in raw_lines:
                dr = round(float(row["debit"] or 0.0), 2)
                cr = round(float(row["credit"] or 0.0), 2)
                period_debit += dr
                period_credit += cr

                line_norm = row.get("normal_balance") or norm_bal
                if line_norm == "Debit":
                    movement = dr - cr
                else:
                    movement = cr - dr

                running_balance = round(running_balance + movement, 2)

                entry_dt = row["entry_date"]
                if isinstance(entry_dt, (datetime.datetime, datetime.date)):
                    entry_dt_str = entry_dt.strftime("%Y-%m-%d %H:%M")
                else:
                    entry_dt_str = str(entry_dt)

                lines.append({
                    "line_id": row["line_id"],
                    "entry_id": row["entry_id"],
                    "entry_number": row["entry_number"],
                    "entry_date": entry_dt_str,
                    "source_module": row["source_module"],
                    "source_entity": row["source_entity"],
                    "source_id": row["source_id"],
                    "reference_no": row["reference_no"],
                    "narration": row["narration"],
                    "line_description": row["line_description"],
                    "account_id": row["account_id"],
                    "account_code": row["account_code"],
                    "account_name": row["account_name"],
                    "major_type": row["major_type"],
                    "party_type": row["party_type"],
                    "party_name": row["party_name"],
                    "debit": dr,
                    "credit": cr,
                    "running_balance": running_balance,
                })

            closing_balance = running_balance

            return {
                "account": account,
                "period": {
                    "start_date": start_date or "Earliest",
                    "end_date": end_date or "Present",
                },
                "opening_balance": opening_balance,
                "period_debit": round(period_debit, 2),
                "period_credit": round(period_credit, 2),
                "closing_balance": closing_balance,
                "lines_count": len(lines),
                "lines": lines,
                "movements": lines,
            }

        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_journal_transactions(filters=None, limit=200):
        """
        Fetches master journal entries with high-level summary of accounts,
        debit/credit totals, and source references for the Transactions list page.
        """
        filters = filters or {}
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)

        conditions = ["1=1"]
        params = []

        if filters.get("status"):
            conditions.append("je.status = %s")
            params.append(filters["status"])
        else:
            conditions.append("je.status IN ('POSTED', 'REVERSED')")

        if filters.get("source_module"):
            conditions.append("je.source_module = %s")
            params.append(filters["source_module"])

        if filters.get("start_date"):
            conditions.append("DATE(je.entry_date) >= %s")
            params.append(filters["start_date"])

        if filters.get("end_date"):
            conditions.append("DATE(je.entry_date) <= %s")
            params.append(filters["end_date"])

        if filters.get("search"):
            s = f"%{filters['search'].strip()}%"
            conditions.append("(je.entry_number LIKE %s OR je.narration LIKE %s OR je.reference_no LIKE %s)")
            params.extend([s, s, s])

        where_clause = " AND ".join(conditions)

        try:
            query = f"""
                SELECT je.id, je.entry_number, je.entry_date, je.posting_date,
                       je.source_module, je.source_entity, je.source_id, je.reference_no,
                       je.narration, je.total_debit, je.total_credit, je.status,
                       je.created_by, je.reversal_of_entry_id, je.reversal_reason,
                       MAX(CASE WHEN jl.debit > 0 THEN ac.name ELSE NULL END) AS primary_debit_account,
                       MAX(CASE WHEN jl.credit > 0 THEN ac.name ELSE NULL END) AS primary_credit_account,
                       MAX(jl.party_name) AS party_name
                FROM journal_entries je
                JOIN journal_lines jl ON je.id = jl.entry_id
                JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE {where_clause}
                GROUP BY je.id, je.entry_number, je.entry_date, je.posting_date,
                         je.source_module, je.source_entity, je.source_id, je.reference_no,
                         je.narration, je.total_debit, je.total_credit, je.status,
                         je.created_by, je.reversal_of_entry_id, je.reversal_reason
                ORDER BY je.entry_date DESC, je.id DESC
                LIMIT %s;
            """
            params.append(limit)
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

            for r in rows:
                r["total_debit"] = round(float(r["total_debit"] or 0.0), 2)
                r["total_credit"] = round(float(r["total_credit"] or 0.0), 2)
                if isinstance(r["entry_date"], (datetime.datetime, datetime.date)):
                    r["entry_date"] = r["entry_date"].strftime("%Y-%m-%d %H:%M")
                else:
                    r["entry_date"] = str(r["entry_date"])

            return rows
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def get_transaction_drilldown(entry_id):
        """
        Complete audit traceability for a transaction:
        Journal Master -> All Lines with accounts -> Source Business Entity metadata.
        """
        from backend.double_entry_engine import DoubleEntryEngine
        return DoubleEntryEngine.get_journal_entry(entry_id)

    get_transactions = get_journal_transactions

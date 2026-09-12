import datetime
import logging
from backend.db import get_db_connection

logger = logging.getLogger(__name__)


class PeriodControlError(Exception):
    """Base exception for accounting period and financial year control errors."""
    pass


class PeriodClosedError(PeriodControlError):
    """Raised when attempting to post into a closed accounting period."""
    pass


class PeriodLockedError(PeriodControlError):
    """Raised when attempting to modify or post into a locked accounting period."""
    pass


class InvalidAccountingDateError(PeriodControlError):
    """Raised when a transaction date does not fall within any configured Financial Year."""
    pass


class OverlappingFinancialYearError(PeriodControlError):
    """Raised when a financial year overlaps with an existing financial year."""
    pass


class PeriodControlEngine:
    """
    Central Financial Period & Financial Year Control Service.
    Enforces strict period status gates (OPEN, CLOSED, LOCKED), controls backdated
    entries, manages controlled period closings and audited reopenings, and prevents
    tampering with historical accounting periods.
    """

    @classmethod
    def record_audit_log(cls, action, entity_type, entity_id=None, old_val=None, new_val=None, reason=None, user="admin", ip_address="127.0.0.1", cur=None, old_state=None, new_state=None, conn=None):
        """Records an immutable financial control action in the accounting audit trail."""
        effective_old = str(old_val) if old_val is not None else (str(old_state) if old_state is not None else None)
        effective_new = str(new_val) if new_val is not None else (str(new_state) if new_state is not None else None)

        if cur is not None:
            cur.execute("""
                INSERT INTO accounting_audit_trail 
                    (action, entity_type, entity_id, old_value, new_value, reason, user, ip_address)
                VALUES 
                    (%s, %s, %s, %s, %s, %s, %s, %s);
            """, (
                str(action),
                str(entity_type),
                str(entity_id) if entity_id is not None else None,
                effective_old,
                effective_new,
                str(reason or "Administrative action"),
                str(user or "system"),
                str(ip_address or "127.0.0.1"),
            ))
            return

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        local_cur = conn.cursor()
        try:
            local_cur.execute("""
                INSERT INTO accounting_audit_trail 
                    (action, entity_type, entity_id, old_value, new_value, reason, user, ip_address)
                VALUES 
                    (%s, %s, %s, %s, %s, %s, %s, %s);
            """, (
                str(action),
                str(entity_type),
                str(entity_id) if entity_id is not None else None,
                effective_old,
                effective_new,
                str(reason or "Administrative action"),
                str(user or "system"),
                str(ip_address or "127.0.0.1"),
            ))
            conn.commit()
        finally:
            local_cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_financial_years(cls, conn=None):
        """Returns all configured financial years ordered chronologically."""
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM financial_years ORDER BY start_date ASC;")
            return cur.fetchall()
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_financial_year(cls, fy_id, conn=None):
        """Returns a specific financial year by ID."""
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM financial_years WHERE id = %s;", (fy_id,))
            return cur.fetchone()
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_active_financial_year(cls, conn=None):
        """Returns the open financial year matching current date or first open FY."""
        fys = cls.get_financial_years(conn=conn)
        today_str = datetime.date.today().isoformat()
        for f in fys:
            if str(f.get("start_date")) <= today_str <= str(f.get("end_date")) and f.get("status") == "OPEN":
                return f
        for f in fys:
            if f.get("status") == "OPEN":
                return f
        return fys[0] if fys else None

    @classmethod
    def create_financial_year(cls, name, start_date, end_date, auto_create_periods=True, user="admin", conn=None):
        """
        Creates a new Financial Year with non-overlapping date validation.
        Optionally generates 12 monthly accounting periods.
        """
        s_date = str(start_date).strip()
        e_date = str(end_date).strip()
        if s_date >= e_date:
            raise PeriodControlError(f"Start date ({s_date}) must be before end date ({e_date}).")

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            # 1. Overlap Check
            cur.execute("""
                SELECT id, name, start_date, end_date FROM financial_years
                WHERE (start_date <= %s AND end_date >= %s)
                   OR (start_date <= %s AND end_date >= %s)
                   OR (start_date >= %s AND end_date <= %s);
            """, (e_date, s_date, s_date, s_date, s_date, e_date))
            overlapping = cur.fetchone()
            if overlapping:
                raise OverlappingFinancialYearError(
                    f"Financial Year '{name}' ({s_date} to {e_date}) overlaps with existing Financial Year "
                    f"'{overlapping['name']}' ({overlapping['start_date']} to {overlapping['end_date']})."
                )

            # 2. Insert Financial Year
            cur.execute("""
                INSERT INTO financial_years (name, start_date, end_date, status)
                VALUES (%s, %s, %s, 'OPEN');
            """, (name.strip(), s_date, e_date))
            conn.commit()

            cur.execute("SELECT id FROM financial_years WHERE name = %s;", (name.strip(),))
            fy_row = cur.fetchone()
            fy_id = fy_row["id"]

            # 3. Auto-generate 12 monthly accounting periods if requested
            if auto_create_periods:
                s_year = int(s_date.split("-")[0])
                s_month = int(s_date.split("-")[1])
                for p_num in range(1, 13):
                    # Compute calendar month and year relative to start month
                    total_months = s_month - 1 + (p_num - 1)
                    cal_year = s_year + (total_months // 12)
                    cal_month = (total_months % 12) + 1

                    m_start = datetime.date(cal_year, cal_month, 1)
                    if cal_month in (1, 3, 5, 7, 8, 10, 12):
                        last_day = 31
                    elif cal_month in (4, 6, 9, 11):
                        last_day = 30
                    else:
                        last_day = 29 if (cal_year % 4 == 0 and (cal_year % 100 != 0 or cal_year % 400 == 0)) else 28
                    m_end = datetime.date(cal_year, cal_month, last_day)

                    p_name = m_start.strftime("%b %Y")
                    cur.execute("""
                        INSERT INTO accounting_periods 
                            (financial_year_id, period_name, period_number, start_date, end_date, status)
                        VALUES 
                            (%s, %s, %s, %s, %s, 'OPEN');
                    """, (fy_id, p_name, p_num, m_start.isoformat(), m_end.isoformat()))
                conn.commit()

            cls.record_audit_log(
                action="CREATE_FINANCIAL_YEAR",
                entity_type="financial_year",
                entity_id=fy_id,
                old_val=None,
                new_val=f"{name} ({s_date} to {e_date})",
                reason="Created new financial year",
                user=user,
                conn=conn,
            )

            return {
                "status": "success",
                "id": fy_id,
                "name": name.strip(),
                "start_date": s_date,
                "end_date": e_date,
                "periods_created": 12 if auto_create_periods else 0
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def get_periods(cls, financial_year_id=None, conn=None):
        """Returns all accounting periods, optionally filtered by financial year."""
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            if financial_year_id:
                cur.execute("""
                    SELECT ap.*, fy.name as fy_name, fy.status as fy_status
                    FROM accounting_periods ap
                    JOIN financial_years fy ON ap.financial_year_id = fy.id
                    WHERE ap.financial_year_id = %s
                    ORDER BY ap.start_date ASC;
                """, (financial_year_id,))
            else:
                cur.execute("""
                    SELECT ap.*, fy.name as fy_name, fy.status as fy_status
                    FROM accounting_periods ap
                    JOIN financial_years fy ON ap.financial_year_id = fy.id
                    ORDER BY ap.start_date ASC;
                """)
            return cur.fetchall()
        finally:
            cur.close()
            if should_close:
                conn.close()

    # Aliases for flexible integration
    get_all_financial_years = get_financial_years
    get_accounting_periods = get_periods

    @classmethod
    def get_period_by_id(cls, period_id, conn=None):
        """Returns an accounting period by ID."""
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT ap.*, fy.name as fy_name, fy.status as fy_status
                FROM accounting_periods ap
                JOIN financial_years fy ON ap.financial_year_id = fy.id
                WHERE ap.id = %s;
            """, (period_id,))
            return cur.fetchone()
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def find_period_for_date(cls, txn_date, conn=None):
        """
        Finds the accounting period and financial year corresponding to a given transaction date.
        """
        if not txn_date:
            return None

        if isinstance(txn_date, datetime.datetime):
            d_str = txn_date.strftime("%Y-%m-%d")
        elif isinstance(txn_date, datetime.date):
            d_str = txn_date.isoformat()
        else:
            d_str = str(txn_date).split("T")[0].split(" ")[0].strip()

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT ap.*, fy.name as fy_name, fy.status as fy_status
                FROM accounting_periods ap
                JOIN financial_years fy ON ap.financial_year_id = fy.id
                WHERE ap.start_date <= %s AND ap.end_date >= %s;
            """, (d_str, d_str))
            return cur.fetchone()
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def validate_transaction_date(cls, txn_date, user="admin", conn=None):
        """
        Validates whether a transaction date is allowable for posting:
        1. Must fall within a configured Financial Year & Accounting Period.
        2. Financial Year cannot be CLOSED or LOCKED.
        3. Accounting Period cannot be CLOSED or LOCKED.
        4. Rejects with clear, actionable accounting errors.
        Returns: (True, period_record)
        """
        if not txn_date:
            raise InvalidAccountingDateError("Transaction date is required.")

        period = cls.find_period_for_date(txn_date, conn=conn)
        if not period:
            raise InvalidAccountingDateError(
                f"The transaction date '{txn_date}' does not fall within any configured Financial Year or Accounting Period. "
                "Please configure an active Financial Year for this date range."
            )

        # 1. Check Financial Year Status
        fy_status = str(period.get("fy_status") or "OPEN").upper()
        if fy_status == "LOCKED":
            raise PeriodLockedError(
                f"Financial Year '{period['fy_name']}' is LOCKED. Transactions cannot be posted or altered for date '{txn_date}'."
            )
        elif fy_status == "CLOSED":
            raise PeriodClosedError(
                f"Financial Year '{period['fy_name']}' is CLOSED. Transactions cannot be posted for date '{txn_date}'."
            )

        # 2. Check Period Status
        p_status = str(period.get("status") or "OPEN").upper()
        if p_status == "LOCKED":
            raise PeriodLockedError(
                f"The accounting period '{period['period_name']}' ({period['start_date']} to {period['end_date']}) is LOCKED. "
                f"Transactions cannot be posted for date '{txn_date}'. Lock reason: {period.get('lock_reason') or 'Statutory audit lock'}."
            )
        elif p_status == "CLOSED":
            raise PeriodClosedError(
                f"This accounting period ('{period['period_name']}', {period['start_date']} to {period['end_date']}) is CLOSED. "
                f"Transactions cannot be posted for date '{txn_date}'."
            )

        return True, period

    @classmethod
    def close_period(cls, period_id, user="admin", conn=None):
        """
        Closes an accounting period after running pre-closure validation.
        Once closed, normal new postings and alterations are strictly prevented.
        """
        period = cls.get_period_by_id(period_id, conn=conn)
        if not period:
            raise PeriodControlError(f"Accounting period ID {period_id} does not exist.")

        if period["status"] == "CLOSED":
            return {"status": "already_closed", "period_id": period_id}
        if period["status"] == "LOCKED":
            raise PeriodLockedError("Cannot close an already LOCKED period.")

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            # Pre-closure integrity check: Verify no unbalanced posted entries in period
            cur.execute("""
                SELECT je.id, je.entry_number, ROUND(SUM(jl.debit), 2) as tot_dr, ROUND(SUM(jl.credit), 2) as tot_cr
                FROM journal_entries je
                JOIN journal_lines jl ON je.id = jl.entry_id
                WHERE je.status = 'POSTED' AND DATE(je.entry_date) >= %s AND DATE(je.entry_date) <= %s
                GROUP BY je.id, je.entry_number
                HAVING tot_dr != tot_cr;
            """, (period["start_date"], period["end_date"]))
            unbalanced = cur.fetchall()
            if unbalanced:
                unb_list = ", ".join(f"#{u['entry_number']}" for u in unbalanced[:3])
                raise PeriodControlError(
                    f"Cannot close period '{period['period_name']}': Unbalanced posted journals detected ({unb_list}). "
                    "Ensure all journals are in perfect balance before closing."
                )

            # Update status to CLOSED
            now_iso = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("""
                UPDATE accounting_periods
                SET status = 'CLOSED', closed_at = %s, closed_by = %s
                WHERE id = %s;
            """, (now_iso, user, period_id))
            conn.commit()

            cls.record_audit_log(
                action="PERIOD_CLOSE",
                entity_type="accounting_period",
                entity_id=period_id,
                old_val="OPEN",
                new_val="CLOSED",
                reason=f"Period closed by {user}",
                user=user,
                conn=conn,
            )

            return {
                "status": "success",
                "period_id": period_id,
                "period_name": period["period_name"],
                "closed_at": now_iso,
                "closed_by": user,
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def reopen_period(cls, period_id, reason, user="admin", conn=None):
        """
        Reopens a closed accounting period with mandatory authorization and audit reason.
        Strictly forbids reopening locked periods without unlocking.
        """
        if not reason or len(str(reason).strip()) < 5:
            raise PeriodControlError("A detailed, valid reason (minimum 5 characters) is required to reopen an accounting period.")

        period = cls.get_period_by_id(period_id, conn=conn)
        if not period:
            raise PeriodControlError(f"Accounting period ID {period_id} does not exist.")

        if period["status"] == "LOCKED":
            raise PeriodLockedError(f"Period '{period['period_name']}' is LOCKED. Reopening locked periods is strictly prohibited.")
        if period["fy_status"] == "LOCKED":
            raise PeriodLockedError(f"Parent Financial Year '{period['fy_name']}' is LOCKED.")

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE accounting_periods
                SET status = 'OPEN', closed_at = NULL, closed_by = NULL
                WHERE id = %s;
            """, (period_id,))
            conn.commit()

            cls.record_audit_log(
                action="PERIOD_REOPEN",
                entity_type="accounting_period",
                entity_id=period_id,
                old_val="CLOSED",
                new_val="OPEN",
                reason=reason.strip(),
                user=user,
                conn=conn,
            )

            return {
                "status": "success",
                "period_id": period_id,
                "period_name": period["period_name"],
                "reopened_by": user,
                "reason": reason.strip(),
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def lock_period(cls, period_id, reason="Statutory audit lock", user="admin", conn=None):
        """
        Places a period into the immutable LOCKED state.
        """
        period = cls.get_period_by_id(period_id, conn=conn)
        if not period:
            raise PeriodControlError(f"Accounting period ID {period_id} does not exist.")

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor()
        try:
            now_iso = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("""
                UPDATE accounting_periods
                SET status = 'LOCKED', locked_at = %s, locked_by = %s, lock_reason = %s
                WHERE id = %s;
            """, (now_iso, user, reason, period_id))
            conn.commit()

            cls.record_audit_log(
                action="PERIOD_LOCK",
                entity_type="accounting_period",
                entity_id=period_id,
                old_val=period["status"],
                new_val="LOCKED",
                reason=reason,
                user=user,
                conn=conn,
            )

            return {
                "status": "success",
                "period_id": period_id,
                "period_name": period["period_name"],
                "locked_at": now_iso,
                "locked_by": user,
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def unlock_period(cls, period_id, reason, user="admin", conn=None):
        """
        Unlocks a previously locked period back to CLOSED state.
        """
        if not reason or len(str(reason).strip()) < 5:
            raise PeriodControlError("An explicit audit reason is required to unlock an accounting period.")

        period = cls.get_period_by_id(period_id, conn=conn)
        if not period:
            raise PeriodControlError(f"Accounting period ID {period_id} does not exist.")

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE accounting_periods
                SET status = 'CLOSED', locked_at = NULL, locked_by = NULL, lock_reason = NULL
                WHERE id = %s;
            """, (period_id,))
            conn.commit()

            cls.record_audit_log(
                action="PERIOD_UNLOCK",
                entity_type="accounting_period",
                entity_id=period_id,
                old_val="LOCKED",
                new_val="CLOSED",
                reason=reason.strip(),
                user=user,
                conn=conn,
            )

            return {
                "status": "success",
                "period_id": period_id,
                "period_name": period["period_name"],
                "unlocked_by": user,
                "new_status": "CLOSED",
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

import os
import sqlite3
import datetime
import hashlib
import logging
from config import Config
from backend.db import (
    get_db_connection,
    get_jai_agency_db_connection,
)
from backend.accounting_rules import AccountingRules
from backend.double_entry_engine import DoubleEntryEngine
from backend.audit_engine import AuditEngine

logger = logging.getLogger(__name__)


def sync_all(force_full=False):
    """
    Orchestrates idempotent synchronization from Jai Agency (SQLite JAI_AGENCY.db),
    which unifies both Sales and Inventory systems.
    All accounting entries are validated and posted atomically through the
    central Double-Entry Accounting Engine and Accounting Rules layer.
    """
    stats = {
        "sales_bills": {"synced": 0, "skipped": 0, "errors": 0},
        "sales_expenses": {"synced": 0, "skipped": 0, "errors": 0},
        "sales_returns": {"synced": 0, "skipped": 0, "errors": 0},
        "inventory_invoices": {"synced": 0, "skipped": 0, "errors": 0},
        "customer_credit_payments": {"synced": 0, "skipped": 0, "errors": 0},
        "supplier_payments": {"synced": 0, "skipped": 0, "errors": 0},
        "synced_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        jai_conn = get_jai_agency_db_connection()
        if jai_conn:
            # 1. Sales Bills
            _sync_sales_bills(jai_conn, stats["sales_bills"])
            # 2. Customer Credit Payments
            _sync_customer_credit_payments(jai_conn, stats["customer_credit_payments"])
            # 3. Counter & Shop Expenses
            _sync_sales_expenses(jai_conn, stats["sales_expenses"])
            # 4. Sales Returns & Refunds
            _sync_sales_returns(jai_conn, stats["sales_returns"])
            # 5. Inventory Inward Purchases (Direct COGS & AP)
            _sync_inventory_invoices(jai_conn, stats["inventory_invoices"])
            # 6. Supplier Payments
            _sync_supplier_payments(jai_conn, stats["supplier_payments"])
            jai_conn.close()
        else:
            logger.warning("Could not connect to Jai Agency DB during sync.")
    except Exception as e:
        logger.error(f"Error during Jai Agency sync: {e}", exc_info=True)

    return stats


def _sync_sales_bills(jai_conn, stat):
    """Sync sales bills from Jai Agency sales_log table."""
    try:
        cur = jai_conn.cursor()
        cur.execute("""
            SELECT id, date, items_count, total, gross_total, discount, payment_method, status,
                   customer_name, customer_mobile, customer_id, amount_paid, balance
            FROM sales_log
            ORDER BY id ASC;
        """)
        bills = [dict(r) for r in cur.fetchall()]
        cur.close()
    except Exception as e:
        logger.error(f"Could not read sales_log from Jai Agency: {e}")
        return

    acc_conn = get_db_connection()
    acc_cur = acc_conn.cursor(dictionary=True)

    try:
        for bill in bills:
            try:
                b_id = str(bill["id"])
                b_total = round(float(bill.get("total") or 0.0), 2)
                gross_total = round(float(bill.get("gross_total") or 0.0), 2)
                b_mode = str(bill.get("payment_method") or "CASH").upper()
                b_status = str(bill.get("status") or "ACTIVE").upper()
                b_date_str = str(bill.get("date") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                balance = round(float(bill.get("balance") or 0.0), 2)
                amount_paid = round(float(bill.get("amount_paid") or 0.0), 2)
                cust_name = str(bill.get("customer_name") or "General Customer").strip()

                raw_hash = f"jai_sls_{b_id}_{b_total}_{gross_total}_{b_mode}_{b_status}_{b_date_str}_{balance}_{amount_paid}"
                b_hash = hashlib.sha256(raw_hash.encode()).hexdigest()

                acc_cur.execute(
                    "SELECT id, source_hash, journal_entry_id, status FROM accounting_sync_registry "
                    "WHERE source_module = 'sales' AND source_entity = 'bill' AND source_id = %s;",
                    (b_id,)
                )
                existing = acc_cur.fetchone()

                # 1. Draft lifecycle handling
                if b_status in ("DRAFT", "PENDING"):
                    if existing:
                        acc_cur.execute("""
                            UPDATE accounting_sync_registry
                            SET status = 'PENDING', source_hash = %s, error_info = NULL, last_attempt_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (b_hash, existing["id"]))
                    else:
                        acc_cur.execute("""
                            INSERT INTO accounting_sync_registry
                                (source_module, source_entity, source_id, source_hash, status, last_attempt_at)
                            VALUES
                                ('sales', 'bill', %s, %s, 'PENDING', CURRENT_TIMESTAMP);
                        """, (b_id, b_hash))
                    acc_conn.commit()
                    stat["skipped"] += 1
                    continue

                # 2. Cancelled / Void lifecycle handling
                if b_status in ("CANCELLED", "VOID"):
                    if existing and existing.get("journal_entry_id"):
                        try:
                            DoubleEntryEngine.reverse_journal_entry(
                                entry_id=existing["journal_entry_id"],
                                reason=f"Sales bill #{b_id} cancelled in Jai Agency",
                                user="sync",
                                external_conn=acc_conn
                            )
                        except Exception as rev_err:
                            logger.warning(f"Reversal note for bill #{b_id}: {rev_err}")
                        acc_cur.execute("""
                            UPDATE accounting_sync_registry
                            SET status = 'REVERSED', source_hash = %s, error_info = NULL, synced_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (b_hash, existing["id"]))
                        acc_conn.commit()
                        stat["synced"] += 1
                    else:
                        stat["skipped"] += 1
                    continue

                # 3. Idempotency & Modification handling
                if existing:
                    if existing["source_hash"] == b_hash and existing.get("status") != "FAILED":
                        stat["skipped"] += 1
                        continue
                    elif existing.get("journal_entry_id"):
                        # Hash changed: bill was edited. Reverse prior entry before posting new.
                        try:
                            DoubleEntryEngine.reverse_journal_entry(
                                entry_id=existing["journal_entry_id"],
                                reason=f"Sales bill #{b_id} modified in Jai Agency (Correction)",
                                user="sync",
                                external_conn=acc_conn
                            )
                        except Exception as rev_err:
                            logger.warning(f"Prior voucher reversal note for bill #{b_id}: {rev_err}")

                # Post via AccountingRules & DoubleEntryEngine
                res = AccountingRules.post_sales_bill(bill, conn=acc_conn)
                if not res:
                    stat["skipped"] += 1
                    continue

                if res.get("is_duplicate"):
                    stat["skipped"] += 1
                    continue

                entry_id = res["entry_id"]

                # Register in accounts_receivables table if credit
                if b_mode == "CREDIT" or balance > 0:
                    _sync_receivable_record(acc_cur, bill, bill.get("date"), b_total, balance, amount_paid, cust_name)

                # Sync Registry Entry
                if existing:
                    acc_cur.execute("""
                        UPDATE accounting_sync_registry
                        SET source_hash = %s, journal_entry_id = %s, status = 'POSTED', error_info = NULL, synced_at = CURRENT_TIMESTAMP
                        WHERE id = %s;
                    """, (b_hash, entry_id, existing["id"]))
                else:
                    acc_cur.execute("""
                        INSERT INTO accounting_sync_registry
                            (source_module, source_entity, source_id, source_hash, journal_entry_id, status, synced_at)
                        VALUES
                            ('sales', 'bill', %s, %s, %s, 'POSTED', CURRENT_TIMESTAMP);
                    """, (b_id, b_hash, entry_id))

                acc_conn.commit()
                stat["synced"] += 1

            except Exception as e:
                acc_conn.rollback()
                err_msg = str(e)
                logger.error(f"Failed syncing Jai Agency bill #{bill.get('id')}: {err_msg}")
                stat["errors"] += 1
                try:
                    b_id = str(bill.get("id") or "")
                    acc_cur.execute("""
                        SELECT id FROM accounting_sync_registry
                        WHERE source_module = 'sales' AND source_entity = 'bill' AND source_id = %s;
                    """, (b_id,))
                    f_reg = acc_cur.fetchone()
                    if f_reg:
                        acc_cur.execute("""
                            UPDATE accounting_sync_registry
                            SET status = 'FAILED', error_info = %s, retry_count = COALESCE(retry_count, 0) + 1, last_attempt_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (err_msg, f_reg["id"]))
                    else:
                        acc_cur.execute("""
                            INSERT INTO accounting_sync_registry
                                (source_module, source_entity, source_id, source_hash, status, error_info, retry_count, last_attempt_at)
                            VALUES
                                ('sales', 'bill', %s, %s, 'FAILED', %s, 1, CURRENT_TIMESTAMP);
                        """, (b_id, b_hash, err_msg))
                    acc_conn.commit()
                except Exception:
                    pass

    finally:
        acc_cur.close()
        acc_conn.close()


def _sync_receivable_record(cursor, bill, b_date, total_amt, balance, amount_paid, customer_name):
    """Upserts accounts_receivables entry for credit bills."""
    b_id = str(bill["id"])
    inv_no = f"INV-{b_id}"
    bal = balance if balance > 0 else (total_amt - amount_paid)
    paid = total_amt - bal if bal < total_amt else amount_paid
    status = "Pending" if bal >= total_amt else ("Partial" if bal > 0 else "Paid")
    due_date = b_date if isinstance(b_date, (datetime.date, datetime.datetime)) else datetime.date.today()

    cursor.execute("SELECT id FROM accounts_receivables WHERE source_bill_id = %s;", (b_id,))
    rec = cursor.fetchone()
    if rec:
        cursor.execute("""
            UPDATE accounts_receivables
            SET total_amount = %s, paid_amount = %s, remaining_balance = %s, status = %s, customer_name = %s, due_date = %s
            WHERE id = %s;
        """, (total_amt, paid, bal, status, customer_name, due_date, rec["id"]))
    else:
        cursor.execute("""
            INSERT INTO accounts_receivables
                (receivable_no, invoice_ref, customer_name, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status, source_bill_id)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (
            f"REC-{inv_no}",
            inv_no,
            customer_name,
            b_date,
            due_date,
            total_amt,
            paid,
            bal,
            status,
            b_id
        ))


def _sync_customer_credit_payments(jai_conn, stat):
    """Sync credit installment payments made by customers in credit_payments."""
    try:
        cur = jai_conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='credit_payments';")
        if not cur.fetchone():
            cur.close()
            return
        cur.execute("""
            SELECT id, bill_id, amount, payment_method, date
            FROM credit_payments
            ORDER BY id ASC;
        """)
        payments = [dict(r) for r in cur.fetchall()]
        cur.close()
    except Exception as e:
        logger.error(f"Could not read credit_payments: {e}")
        return

    if not payments:
        return

    acc_conn = get_db_connection()
    acc_cur = acc_conn.cursor(dictionary=True)

    try:
        for pay in payments:
            try:
                cp_id = str(pay["id"])
                bill_id = str(pay.get("bill_id") or "")
                cp_amt = round(float(pay.get("amount") or 0.0), 2)
                cp_mode = str(pay.get("payment_method") or "CASH").upper()
                cp_date_str = str(pay.get("date") or datetime.datetime.now().strftime("%Y-%m-%d"))

                raw_hash = f"jai_cp_{cp_id}_{bill_id}_{cp_amt}_{cp_mode}_{cp_date_str}"
                cp_hash = hashlib.sha256(raw_hash.encode()).hexdigest()

                acc_cur.execute(
                    "SELECT id, source_hash, journal_entry_id, status FROM accounting_sync_registry "
                    "WHERE source_module = 'sales' AND source_entity = 'credit_payment' AND source_id = %s;",
                    (cp_id,)
                )
                existing = acc_cur.fetchone()

                if existing:
                    if existing["source_hash"] == cp_hash and existing.get("status") != "FAILED":
                        stat["skipped"] += 1
                        continue
                    elif existing.get("journal_entry_id"):
                        try:
                            DoubleEntryEngine.reverse_journal_entry(
                                entry_id=existing["journal_entry_id"],
                                reason=f"Credit payment #{cp_id} modified in Jai Agency",
                                user="sync",
                                external_conn=acc_conn
                            )
                        except Exception as rev_err:
                            logger.warning(f"Reversal note on payment #{cp_id}: {rev_err}")

                # Post via AccountingRules
                res = AccountingRules.post_customer_payment(pay, conn=acc_conn)
                if not res:
                    stat["skipped"] += 1
                    continue

                if res.get("is_duplicate"):
                    stat["skipped"] += 1
                    continue

                entry_id = res["entry_id"]

                # Update accounts_receivables table
                if bill_id:
                    acc_cur.execute("SELECT id, total_amount, paid_amount, remaining_balance FROM accounts_receivables WHERE source_bill_id = %s;", (bill_id,))
                    rec_row = acc_cur.fetchone()
                    if rec_row:
                        new_paid = round(float(rec_row["paid_amount"] or 0) + cp_amt, 2)
                        new_bal = max(0.0, round(float(rec_row["total_amount"] or 0) - new_paid, 2))
                        new_status = "Paid" if new_bal <= 0 else "Partial"
                        acc_cur.execute("""
                            UPDATE accounts_receivables
                            SET paid_amount = %s, remaining_balance = %s, status = %s
                            WHERE id = %s;
                        """, (new_paid, new_bal, new_status, rec_row["id"]))

                if existing:
                    acc_cur.execute("""
                        UPDATE accounting_sync_registry
                        SET source_hash = %s, journal_entry_id = %s, status = 'POSTED', error_info = NULL, synced_at = CURRENT_TIMESTAMP
                        WHERE id = %s;
                    """, (cp_hash, entry_id, existing["id"]))
                else:
                    acc_cur.execute("""
                        INSERT INTO accounting_sync_registry
                            (source_module, source_entity, source_id, source_hash, journal_entry_id, status, synced_at)
                        VALUES
                            ('sales', 'credit_payment', %s, %s, %s, 'POSTED', CURRENT_TIMESTAMP);
                    """, (cp_id, cp_hash, entry_id))

                acc_conn.commit()
                stat["synced"] += 1

            except Exception as e:
                acc_conn.rollback()
                err_msg = str(e)
                logger.error(f"Failed syncing credit payment #{pay.get('id')}: {err_msg}")
                stat["errors"] += 1
                try:
                    cp_id = str(pay.get("id") or "")
                    acc_cur.execute("""
                        SELECT id FROM accounting_sync_registry
                        WHERE source_module = 'sales' AND source_entity = 'credit_payment' AND source_id = %s;
                    """, (cp_id,))
                    f_reg = acc_cur.fetchone()
                    if f_reg:
                        acc_cur.execute("""
                            UPDATE accounting_sync_registry
                            SET status = 'FAILED', error_info = %s, retry_count = COALESCE(retry_count, 0) + 1, last_attempt_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (err_msg, f_reg["id"]))
                    else:
                        acc_cur.execute("""
                            INSERT INTO accounting_sync_registry
                                (source_module, source_entity, source_id, source_hash, status, error_info, retry_count, last_attempt_at)
                            VALUES
                                ('sales', 'credit_payment', %s, %s, 'FAILED', %s, 1, CURRENT_TIMESTAMP);
                        """, (cp_id, cp_hash, err_msg))
                    acc_conn.commit()
                except Exception:
                    pass

    finally:
        acc_cur.close()
        acc_conn.close()


def _sync_sales_expenses(jai_conn, stat):
    """Sync operational counter and store expenses from Jai Agency expenses table."""
    try:
        cur = jai_conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='expenses';")
        if not cur.fetchone():
            cur.close()
            return
        cur.execute("""
            SELECT id, date, description, category, amount, payment_method
            FROM expenses
            ORDER BY id ASC;
        """)
        exps = [dict(r) for r in cur.fetchall()]
        cur.close()
    except Exception as e:
        logger.error(f"Could not read expenses from Jai Agency: {e}")
        return

    if not exps:
        return

    acc_conn = get_db_connection()
    acc_cur = acc_conn.cursor(dictionary=True)

    try:
        for exp in exps:
            try:
                e_id = str(exp["id"])
                e_amt = round(float(exp.get("amount") or 0.0), 2)
                e_cat = str(exp.get("category") or "General")
                e_date_str = str(exp.get("date") or datetime.date.today().isoformat())
                e_mode = str(exp.get("payment_method") or "CASH").upper()

                raw_hash = f"jai_exp_{e_id}_{e_amt}_{e_cat}_{e_date_str}_{e_mode}"
                e_hash = hashlib.sha256(raw_hash.encode()).hexdigest()

                acc_cur.execute(
                    "SELECT id, source_hash, journal_entry_id, status FROM accounting_sync_registry "
                    "WHERE source_module = 'sales' AND source_entity = 'expense' AND source_id = %s;",
                    (e_id,)
                )
                existing = acc_cur.fetchone()

                if existing:
                    if existing["source_hash"] == e_hash and existing.get("status") != "FAILED":
                        stat["skipped"] += 1
                        continue
                    elif existing.get("journal_entry_id"):
                        try:
                            DoubleEntryEngine.reverse_journal_entry(
                                entry_id=existing["journal_entry_id"],
                                reason=f"Expense #{e_id} modified in Jai Agency",
                                user="sync",
                                external_conn=acc_conn
                            )
                        except Exception as rev_err:
                            logger.warning(f"Reversal note on expense #{e_id}: {rev_err}")

                # Post via AccountingRules
                res = AccountingRules.post_expense(exp, conn=acc_conn)
                if not res:
                    stat["skipped"] += 1
                    continue

                if res.get("is_duplicate"):
                    stat["skipped"] += 1
                    continue

                entry_id = res["entry_id"]

                if existing:
                    acc_cur.execute("""
                        UPDATE accounting_sync_registry
                        SET source_hash = %s, journal_entry_id = %s, status = 'POSTED', error_info = NULL, synced_at = CURRENT_TIMESTAMP
                        WHERE id = %s;
                    """, (e_hash, entry_id, existing["id"]))
                else:
                    acc_cur.execute("""
                        INSERT INTO accounting_sync_registry
                            (source_module, source_entity, source_id, source_hash, journal_entry_id, status, synced_at)
                        VALUES
                            ('sales', 'expense', %s, %s, %s, 'POSTED', CURRENT_TIMESTAMP);
                    """, (e_id, e_hash, entry_id))

                acc_conn.commit()
                stat["synced"] += 1

            except Exception as e:
                acc_conn.rollback()
                err_msg = str(e)
                logger.error(f"Failed syncing expense #{exp.get('id')}: {err_msg}")
                stat["errors"] += 1
                try:
                    e_id = str(exp.get("id") or "")
                    acc_cur.execute("""
                        SELECT id FROM accounting_sync_registry
                        WHERE source_module = 'sales' AND source_entity = 'expense' AND source_id = %s;
                    """, (e_id,))
                    f_reg = acc_cur.fetchone()
                    if f_reg:
                        acc_cur.execute("""
                            UPDATE accounting_sync_registry
                            SET status = 'FAILED', error_info = %s, retry_count = COALESCE(retry_count, 0) + 1, last_attempt_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (err_msg, f_reg["id"]))
                    else:
                        acc_cur.execute("""
                            INSERT INTO accounting_sync_registry
                                (source_module, source_entity, source_id, source_hash, status, error_info, retry_count, last_attempt_at)
                            VALUES
                                ('sales', 'expense', %s, %s, 'FAILED', %s, 1, CURRENT_TIMESTAMP);
                        """, (e_id, e_hash, err_msg))
                    acc_conn.commit()
                except Exception:
                    pass

    finally:
        acc_cur.close()
        acc_conn.close()


def _sync_sales_returns(jai_conn, stat):
    """Sync sales returns and customer refunds from Jai Agency returns_log table."""
    try:
        cur = jai_conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='returns_log';")
        if not cur.fetchone():
            cur.close()
            return
        cur.execute("""
            SELECT id, date, type, bill_id, product_code, product_name, qty, refund_amount
            FROM returns_log
            ORDER BY id ASC;
        """)
        returns = [dict(r) for r in cur.fetchall()]
        cur.close()
    except Exception as e:
        logger.error(f"Could not read returns_log: {e}")
        return

    if not returns:
        return

    acc_conn = get_db_connection()
    acc_cur = acc_conn.cursor(dictionary=True)

    try:
        for ret in returns:
            try:
                r_id = str(ret["id"])
                r_amt = round(float(ret.get("refund_amount") or 0.0), 2)
                r_date_str = str(ret.get("date") or datetime.date.today().isoformat())

                raw_hash = f"jai_ret_{r_id}_{r_amt}_{ret.get('bill_id')}_{r_date_str}"
                r_hash = hashlib.sha256(raw_hash.encode()).hexdigest()

                acc_cur.execute(
                    "SELECT id, source_hash, journal_entry_id, status FROM accounting_sync_registry "
                    "WHERE source_module = 'sales' AND source_entity = 'return' AND source_id = %s;",
                    (r_id,)
                )
                existing = acc_cur.fetchone()

                if existing:
                    if existing["source_hash"] == r_hash and existing.get("status") != "FAILED":
                        stat["skipped"] += 1
                        continue
                    elif existing.get("journal_entry_id"):
                        try:
                            DoubleEntryEngine.reverse_journal_entry(
                                entry_id=existing["journal_entry_id"],
                                reason=f"Sales return #{r_id} modified in Jai Agency",
                                user="sync",
                                external_conn=acc_conn
                            )
                        except Exception as rev_err:
                            logger.warning(f"Reversal note on return #{r_id}: {rev_err}")

                # Post via AccountingRules
                res = AccountingRules.post_sales_return(ret, conn=acc_conn)
                if not res:
                    stat["skipped"] += 1
                    continue

                if res.get("is_duplicate"):
                    stat["skipped"] += 1
                    continue

                entry_id = res["entry_id"]

                if existing:
                    acc_cur.execute("""
                        UPDATE accounting_sync_registry
                        SET source_hash = %s, journal_entry_id = %s, status = 'POSTED', error_info = NULL, synced_at = CURRENT_TIMESTAMP
                        WHERE id = %s;
                    """, (r_hash, entry_id, existing["id"]))
                else:
                    acc_cur.execute("""
                        INSERT INTO accounting_sync_registry
                            (source_module, source_entity, source_id, source_hash, journal_entry_id, status, synced_at)
                        VALUES
                            ('sales', 'return', %s, %s, %s, 'POSTED', CURRENT_TIMESTAMP);
                    """, (r_id, r_hash, entry_id))

                acc_conn.commit()
                stat["synced"] += 1

            except Exception as e:
                acc_conn.rollback()
                err_msg = str(e)
                logger.error(f"Failed syncing return #{ret.get('id')}: {err_msg}")
                stat["errors"] += 1
                try:
                    r_id = str(ret.get("id") or "")
                    acc_cur.execute("""
                        SELECT id FROM accounting_sync_registry
                        WHERE source_module = 'sales' AND source_entity = 'return' AND source_id = %s;
                    """, (r_id,))
                    f_reg = acc_cur.fetchone()
                    if f_reg:
                        acc_cur.execute("""
                            UPDATE accounting_sync_registry
                            SET status = 'FAILED', error_info = %s, retry_count = COALESCE(retry_count, 0) + 1, last_attempt_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (err_msg, f_reg["id"]))
                    else:
                        acc_cur.execute("""
                            INSERT INTO accounting_sync_registry
                                (source_module, source_entity, source_id, source_hash, status, error_info, retry_count, last_attempt_at)
                            VALUES
                                ('sales', 'return', %s, %s, 'FAILED', %s, 1, CURRENT_TIMESTAMP);
                        """, (r_id, r_hash, err_msg))
                    acc_conn.commit()
                except Exception:
                    pass

    finally:
        acc_cur.close()
        acc_conn.close()


def _sync_inventory_invoices(jai_conn, stat):
    """
    Sync inventory inward purchases from Jai Agency storage table,
    grouped by invoice_no and arrival_date.
    """
    try:
        cur = jai_conn.cursor()
        cur.execute("""
            SELECT 
                s.invoice_no, 
                s.arrival_date,
                MAX(s.entry_time) as entry_time,
                SUM(s.qty * s.cost) as total_cost,
                SUM(COALESCE(s.gst_value, 0)) as total_gst,
                MAX(s.is_credit) as is_credit,
                MAX(s.amount_paid) as amount_paid,
                MAX(s.payment_mode) as payment_mode,
                MAX(sup.name) as supplier_name,
                MAX(sup.id) as supplier_id
            FROM storage s
            LEFT JOIN suppliers sup ON s.supplier_id = sup.id
            WHERE s.invoice_no != 'MANUAL-STORAGE' 
              AND s.invoice_no NOT LIKE 'RET-%' 
              AND s.invoice_no NOT LIKE 'CANCEL-%'
            GROUP BY s.invoice_no, s.arrival_date
            ORDER BY s.arrival_date ASC, s.entry_time ASC;
        """)
        invoices = [dict(r) for r in cur.fetchall()]
        cur.close()
    except Exception as e:
        logger.error(f"Could not read storage purchases from Jai Agency: {e}")
        return

    acc_conn = get_db_connection()
    acc_cur = acc_conn.cursor(dictionary=True)

    try:
        for inv in invoices:
            try:
                inv_no = str(inv["invoice_no"] or "").strip()
                arr_date_str = str(inv.get("arrival_date") or datetime.date.today().isoformat())
                cost_amt = round(float(inv.get("total_cost") or 0.0), 2)
                gst_amt = round(float(inv.get("total_gst") or 0.0), 2)
                grand_tot = round(cost_amt + gst_amt, 2)
                is_credit = int(inv.get("is_credit") or 0)
                amount_paid = round(float(inv.get("amount_paid") or 0.0), 2)
                vendor = str(inv.get("supplier_name") or f"Supplier (Inv #{inv_no})").strip()

                if grand_tot <= 0:
                    stat["skipped"] += 1
                    continue

                unique_src_id = f"{inv_no}@{arr_date_str}"
                raw_hash = f"jai_inv_{unique_src_id}_{cost_amt}_{gst_amt}_{is_credit}_{amount_paid}"
                i_hash = hashlib.sha256(raw_hash.encode()).hexdigest()

                acc_cur.execute(
                    "SELECT id, source_hash, journal_entry_id, status FROM accounting_sync_registry "
                    "WHERE source_module = 'inventory' AND source_entity = 'purchase_invoice' AND source_id = %s;",
                    (unique_src_id,)
                )
                existing = acc_cur.fetchone()

                if existing:
                    if existing["source_hash"] == i_hash and existing.get("status") != "FAILED":
                        stat["skipped"] += 1
                        continue
                    elif existing.get("journal_entry_id"):
                        try:
                            DoubleEntryEngine.reverse_journal_entry(
                                entry_id=existing["journal_entry_id"],
                                reason=f"Purchase invoice {unique_src_id} modified in Jai Agency",
                                user="sync",
                                external_conn=acc_conn
                            )
                        except Exception as rev_err:
                            logger.warning(f"Reversal note on invoice {unique_src_id}: {rev_err}")

                # Post via AccountingRules
                inv["unique_src_id"] = unique_src_id
                res = AccountingRules.post_inventory_purchase(inv, conn=acc_conn)
                if not res:
                    stat["skipped"] += 1
                    continue

                if res.get("is_duplicate"):
                    stat["skipped"] += 1
                    continue

                entry_id = res["entry_id"]

                # Sync into accounts_payables
                p_status = "Paid" if (is_credit == 0 or amount_paid >= grand_tot) else ("Partial" if amount_paid > 0 else "Pending")
                _sync_payable_record(acc_cur, unique_src_id, inv_no, vendor, arr_date_str, grand_tot, amount_paid, p_status)

                if existing:
                    acc_cur.execute("""
                        UPDATE accounting_sync_registry
                        SET source_hash = %s, journal_entry_id = %s, status = 'POSTED', error_info = NULL, synced_at = CURRENT_TIMESTAMP
                        WHERE id = %s;
                    """, (i_hash, entry_id, existing["id"]))
                else:
                    acc_cur.execute("""
                        INSERT INTO accounting_sync_registry
                            (source_module, source_entity, source_id, source_hash, journal_entry_id, status, synced_at)
                        VALUES
                            ('inventory', 'purchase_invoice', %s, %s, %s, 'POSTED', CURRENT_TIMESTAMP);
                    """, (unique_src_id, i_hash, entry_id))

                acc_conn.commit()
                stat["synced"] += 1

            except Exception as e:
                acc_conn.rollback()
                err_msg = str(e)
                logger.error(f"Failed syncing Jai Agency storage purchase #{inv.get('invoice_no')}: {err_msg}")
                stat["errors"] += 1
                try:
                    unique_src_id = f"{inv.get('invoice_no')}@{inv.get('arrival_date')}"
                    acc_cur.execute("""
                        SELECT id FROM accounting_sync_registry
                        WHERE source_module = 'inventory' AND source_entity = 'purchase_invoice' AND source_id = %s;
                    """, (unique_src_id,))
                    f_reg = acc_cur.fetchone()
                    if f_reg:
                        acc_cur.execute("""
                            UPDATE accounting_sync_registry
                            SET status = 'FAILED', error_info = %s, retry_count = COALESCE(retry_count, 0) + 1, last_attempt_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (err_msg, f_reg["id"]))
                    else:
                        acc_cur.execute("""
                            INSERT INTO accounting_sync_registry
                                (source_module, source_entity, source_id, source_hash, status, error_info, retry_count, last_attempt_at)
                            VALUES
                                ('inventory', 'purchase_invoice', %s, %s, 'FAILED', %s, 1, CURRENT_TIMESTAMP);
                        """, (unique_src_id, i_hash, err_msg))
                    acc_conn.commit()
                except Exception:
                    pass

    finally:
        acc_cur.close()
        acc_conn.close()


def _sync_payable_record(cursor, unique_id, inv_no, vendor, i_date, grand_tot, amount_paid, p_status):
    """Upserts accounts_payables record."""
    paid = amount_paid if p_status != "Paid" else grand_tot
    rem = max(0.0, round(grand_tot - paid, 2)) if p_status != "Paid" else 0.0

    cursor.execute("SELECT id FROM accounts_payables WHERE source_invoice_id = %s;", (unique_id,))
    pay = cursor.fetchone()
    if pay:
        cursor.execute("""
            UPDATE accounts_payables
            SET total_amount = %s, paid_amount = %s, remaining_balance = %s, status = %s, supplier_name = %s
            WHERE id = %s;
        """, (grand_tot, paid, rem, p_status, vendor, pay["id"]))
    else:
        cursor.execute("""
            INSERT INTO accounts_payables
                (payable_no, invoice_ref, supplier_name, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status, source_invoice_id)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (
            f"PAY-INV-{inv_no}",
            inv_no,
            vendor,
            i_date,
            i_date,
            grand_tot,
            paid,
            rem,
            p_status,
            unique_id
        ))


def _sync_supplier_payments(jai_conn, stat):
    """Sync vendor disbursements recorded in supplier_payments."""
    try:
        cur = jai_conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='supplier_payments';")
        if not cur.fetchone():
            cur.close()
            return
        cur.execute("""
            SELECT id, supplier_id, invoice_no, amount, payment_mode, date, remarks
            FROM supplier_payments
            ORDER BY id ASC;
        """)
        payments = [dict(r) for r in cur.fetchall()]
        cur.close()
    except Exception as e:
        logger.error(f"Could not read supplier_payments: {e}")
        return

    if not payments:
        return

    acc_conn = get_db_connection()
    acc_cur = acc_conn.cursor(dictionary=True)

    try:
        for pay in payments:
            try:
                sp_id = str(pay["id"])
                sp_amt = round(float(pay.get("amount") or 0.0), 2)
                sp_inv = str(pay.get("invoice_no") or "").strip()
                sp_date_str = str(pay.get("date") or datetime.date.today().isoformat())
                sp_mode = str(pay.get("payment_mode") or "BANK").upper()

                raw_hash = f"jai_sp_{sp_id}_{sp_amt}_{sp_inv}_{sp_date_str}_{sp_mode}"
                sp_hash = hashlib.sha256(raw_hash.encode()).hexdigest()

                acc_cur.execute(
                    "SELECT id, source_hash, journal_entry_id, status FROM accounting_sync_registry "
                    "WHERE source_module = 'inventory' AND source_entity = 'supplier_payment' AND source_id = %s;",
                    (sp_id,)
                )
                existing = acc_cur.fetchone()

                if existing:
                    if existing["source_hash"] == sp_hash and existing.get("status") != "FAILED":
                        stat["skipped"] += 1
                        continue
                    elif existing.get("journal_entry_id"):
                        try:
                            DoubleEntryEngine.reverse_journal_entry(
                                entry_id=existing["journal_entry_id"],
                                reason=f"Supplier payment #{sp_id} modified in Jai Agency",
                                user="sync",
                                external_conn=acc_conn
                            )
                        except Exception as rev_err:
                            logger.warning(f"Reversal note on supplier payment #{sp_id}: {rev_err}")

                # Post via AccountingRules
                res = AccountingRules.post_supplier_payment(pay, conn=acc_conn)
                if not res:
                    stat["skipped"] += 1
                    continue

                if res.get("is_duplicate"):
                    stat["skipped"] += 1
                    continue

                entry_id = res["entry_id"]

                # Update accounts_payables
                if sp_inv:
                    acc_cur.execute("SELECT id, total_amount, paid_amount, remaining_balance FROM accounts_payables WHERE invoice_ref = %s;", (sp_inv,))
                    pay_row = acc_cur.fetchone()
                    if pay_row:
                        new_paid = round(float(pay_row["paid_amount"] or 0.0) + sp_amt, 2)
                        new_rem = max(0.0, round(float(pay_row["total_amount"] or 0.0) - new_paid, 2))
                        new_status = "Paid" if new_rem <= 0 else "Partial"
                        acc_cur.execute("""
                            UPDATE accounts_payables
                            SET paid_amount = %s, remaining_balance = %s, status = %s
                            WHERE id = %s;
                        """, (new_paid, new_rem, new_status, pay_row["id"]))

                if existing:
                    acc_cur.execute("""
                        UPDATE accounting_sync_registry
                        SET source_hash = %s, journal_entry_id = %s, status = 'POSTED', error_info = NULL, synced_at = CURRENT_TIMESTAMP
                        WHERE id = %s;
                    """, (sp_hash, entry_id, existing["id"]))
                else:
                    acc_cur.execute("""
                        INSERT INTO accounting_sync_registry
                            (source_module, source_entity, source_id, source_hash, journal_entry_id, status, synced_at)
                        VALUES
                            ('inventory', 'supplier_payment', %s, %s, %s, 'POSTED', CURRENT_TIMESTAMP);
                    """, (sp_id, sp_hash, entry_id))

                acc_conn.commit()
                stat["synced"] += 1

            except Exception as e:
                acc_conn.rollback()
                err_msg = str(e)
                logger.error(f"Failed syncing supplier payment #{pay.get('id')}: {err_msg}")
                stat["errors"] += 1
                try:
                    sp_id = str(pay.get("id") or "")
                    acc_cur.execute("""
                        SELECT id FROM accounting_sync_registry
                        WHERE source_module = 'inventory' AND source_entity = 'supplier_payment' AND source_id = %s;
                    """, (sp_id,))
                    f_reg = acc_cur.fetchone()
                    if f_reg:
                        acc_cur.execute("""
                            UPDATE accounting_sync_registry
                            SET status = 'FAILED', error_info = %s, retry_count = COALESCE(retry_count, 0) + 1, last_attempt_at = CURRENT_TIMESTAMP
                            WHERE id = %s;
                        """, (err_msg, f_reg["id"]))
                    else:
                        acc_cur.execute("""
                            INSERT INTO accounting_sync_registry
                                (source_module, source_entity, source_id, source_hash, status, error_info, retry_count, last_attempt_at)
                            VALUES
                                ('inventory', 'supplier_payment', %s, %s, 'FAILED', %s, 1, CURRENT_TIMESTAMP);
                        """, (sp_id, sp_hash, err_msg))
                    acc_conn.commit()
                except Exception:
                    pass

    finally:
        acc_cur.close()
        acc_conn.close()


def get_live_inventory_valuation():
    """
    Calculates live stock valuation from Jai Agency storage table (sum of qty * cost).
    """
    inv_conn = get_jai_agency_db_connection()
    if not inv_conn:
        return 0.0
    try:
        cur = inv_conn.cursor()
        cur.execute("SELECT COALESCE(SUM(qty * cost), 0) AS total FROM storage WHERE qty > 0;")
        row = cur.fetchone()
        if not row:
            return 0.0
        val = row["total"] if isinstance(row, dict) else (row[0] if row else 0.0)
        return round(float(val or 0.0), 2)
    except Exception as e:
        logger.error(f"Error computing Jai Agency live inventory valuation: {e}")
        return 0.0
    finally:
        inv_conn.close()


# =============================================================================
# SYNCHRONIZATION FAILURE MANAGEMENT & SAFE RETRY PIPELINE
# =============================================================================
def get_sync_failures(limit=50):
    """
    Returns all failed synchronization items requiring attention / recovery.
    """
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, source_module, source_entity, source_id, source_hash,
                   status, error_info, retry_count, last_attempt_at, synced_at
            FROM accounting_sync_registry
            WHERE status = 'FAILED'
            ORDER BY last_attempt_at DESC
            LIMIT %s;
        """, (limit,))
        failures = cur.fetchall()
        for f in failures:
            if f.get("last_attempt_at"):
                f["last_attempt_at"] = str(f["last_attempt_at"])
            if f.get("synced_at"):
                f["synced_at"] = str(f["synced_at"])
        return failures
    finally:
        cur.close()
        conn.close()


def retry_failed_sync(registry_id=None, user="admin"):
    """
    Safely retries failed synchronization records.
    If registry_id is omitted, re-runs full sync to process all pending/failed items.
    """
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        if registry_id:
            cur.execute("SELECT * FROM accounting_sync_registry WHERE id = %s;", (registry_id,))
            target = cur.fetchone()
            if not target:
                return {"status": "error", "message": f"Sync registry record #{registry_id} not found."}

            # Reset status to retry
            cur.execute("""
                UPDATE accounting_sync_registry
                SET status = 'PENDING', error_info = NULL
                WHERE id = %s;
            """, (registry_id,))
            conn.commit()

        # Run synchronization
        stats = sync_all()

        AuditEngine.log_event(
            action="SYNC_RETRY",
            entity_type="sync_registry",
            entity_id=str(registry_id) if registry_id else "ALL",
            user=user,
            new_value={"stats": stats},
            reason=f"Retried synchronization (Target: {registry_id or 'All Failed'})"
        )

        return {"status": "success", "stats": stats}
    finally:
        cur.close()
        conn.close()


def get_sync_registry_summary():
    """
    Returns comprehensive pipeline health counts by status across all integration modules.
    """
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT 
                COUNT(*) AS total_records,
                SUM(CASE WHEN status = 'POSTED' THEN 1 ELSE 0 END) AS posted_count,
                SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'REVERSED' THEN 1 ELSE 0 END) AS reversed_count,
                MAX(synced_at) AS last_synced_at
            FROM accounting_sync_registry;
        """)
        row = cur.fetchone()
        summary = {
            "total_records": int(row["total_records"] or 0),
            "posted": int(row["posted_count"] or 0),
            "posted_count": int(row["posted_count"] or 0),
            "failed": int(row["failed_count"] or 0),
            "failed_count": int(row["failed_count"] or 0),
            "pending": int(row["pending_count"] or 0),
            "pending_count": int(row["pending_count"] or 0),
            "reversed": int(row["reversed_count"] or 0),
            "reversed_count": int(row["reversed_count"] or 0),
            "last_synced_at": str(row["last_synced_at"]) if row.get("last_synced_at") else None
        }
        return summary
    finally:
        cur.close()
        conn.close()


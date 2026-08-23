import datetime
from backend.db import get_db_connection

def get_dashboard_summary():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Total Revenue / Income
        cursor.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM accounts_transactions WHERE account_type = 'Revenue';")
        total_income = float(cursor.fetchone()['total'])
        
        # Total Direct Expenses (COGS)
        cursor.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM accounts_transactions WHERE account_type = 'Direct Expense';")
        total_cogs = float(cursor.fetchone()['total'])
        
        # Total Operating Expenses
        cursor.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM accounts_transactions WHERE account_type = 'Operating Expense';")
        total_opex = float(cursor.fetchone()['total'])
        
        # Formulas from accounts.pdf:
        # Gross Profit = Total Revenue - Total COGS
        # Net Profit = Gross Profit - Operating Expenses
        gross_profit = total_income - total_cogs
        net_profit = gross_profit - total_opex
        
        # Receivables summary
        cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) AS total FROM accounts_receivables WHERE status != 'Paid';")
        total_receivables = float(cursor.fetchone()['total'])
        
        # Payables summary
        cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) AS total FROM accounts_payables WHERE status != 'Paid';")
        total_payables = float(cursor.fetchone()['total'])
        
        # Cash / Bank Current Balance estimation
        # Cash/Bank = (Total Income + Initial) - (Total COGS + Total OpEx + Paid Payables) + Rec Payments
        current_balance = total_income - (total_cogs + total_opex)
        
        # Recent Transactions (Last 10)
        cursor.execute("""
            SELECT id, txn_number, txn_date, txn_type, account_type, category_name, amount, payment_method, reference_no, description
            FROM accounts_transactions
            ORDER BY txn_date DESC, id DESC
            LIMIT 10;
        """)
        recent_txns = cursor.fetchall()
        for t in recent_txns:
            t['txn_date'] = t['txn_date'].strftime('%Y-%m-%d %H:%M') if isinstance(t['txn_date'], datetime.datetime) else str(t['txn_date'])
            t['amount'] = float(t['amount'])
            
        # Urgent Alerts (Overdue Receivables & Payables)
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        cursor.execute("SELECT receivable_no, customer_name, remaining_balance, due_date FROM accounts_receivables WHERE status != 'Paid' AND due_date < %s ORDER BY due_date ASC LIMIT 5;", (today_str,))
        overdue_recs = cursor.fetchall()
        for r in overdue_recs:
            r['remaining_balance'] = float(r['remaining_balance'])
            r['due_date'] = r['due_date'].strftime('%Y-%m-%d') if isinstance(r['due_date'], datetime.date) else str(r['due_date'])
            
        cursor.execute("SELECT payable_no, supplier_name, remaining_balance, due_date FROM accounts_payables WHERE status != 'Paid' AND due_date < %s ORDER BY due_date ASC LIMIT 5;", (today_str,))
        overdue_pays = cursor.fetchall()
        for p in overdue_pays:
            p['remaining_balance'] = float(p['remaining_balance'])
            p['due_date'] = p['due_date'].strftime('%Y-%m-%d') if isinstance(p['due_date'], datetime.date) else str(p['due_date'])
            
        return {
            'total_income': total_income,
            'total_cogs': total_cogs,
            'total_opex': total_opex,
            'total_expenses': total_cogs + total_opex,
            'gross_profit': gross_profit,
            'net_profit': net_profit,
            'current_balance': current_balance,
            'total_receivables': total_receivables,
            'total_payables': total_payables,
            'recent_transactions': recent_txns,
            'overdue_receivables': overdue_recs,
            'overdue_payables': overdue_pays
        }
    finally:
        cursor.close()
        conn.close()

def add_quick_entry(payload, username):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        description = payload.get('description', '').strip()
        amount = float(payload.get('amount', 0))
        payment_method = payload.get('payment_method', 'Cash')
        account_type = payload.get('account_type', 'Operating Expense')
        category_name = payload.get('category_name', '').strip()
        
        if amount <= 0:
            raise ValueError("Amount must be greater than zero.")
        if not description:
            raise ValueError("Description is required.")
            
        # Determine Txn Type
        if account_type == 'Revenue':
            txn_type = 'Income'
        elif account_type in ['Direct Expense', 'Operating Expense']:
            txn_type = 'Expense'
        elif account_type == 'Asset':
            txn_type = 'Asset_Purchase'
        elif account_type == 'Liability':
            txn_type = 'Liability_Payment'
        else:
            txn_type = 'Equity_Transfer'
            
        # Determine category default if omitted
        if not category_name:
            if account_type == 'Operating Expense':
                category_name = 'Shop Expense'
            elif account_type == 'Direct Expense':
                category_name = 'Raw Materials'
            elif account_type == 'Revenue':
                category_name = 'Sales Revenue'
            else:
                category_name = account_type

        # Find category ID
        cursor.execute("SELECT id FROM accounts_categories WHERE name = %s AND type = %s;", (category_name, account_type))
        cat_row = cursor.fetchone()
        cat_id = cat_row['id'] if cat_row else None
            
        txn_number = payload.get('txn_number') or f"TXN-{int(datetime.datetime.now().timestamp() * 1000) % 100000000}"
        txn_date = payload.get('txn_date') or datetime.datetime.now()
        
        cursor.execute("""
            INSERT INTO accounts_transactions
                (txn_number, txn_date, txn_type, account_type, category_id, category_name, amount, payment_method, reference_no, description, created_by)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (txn_number, txn_date, txn_type, account_type, cat_id, category_name or account_type, amount, payment_method, payload.get('reference_no', 'QUICK-ENTRY'), description, username))
        
        conn.commit()
        return {'status': 'success', 'txn_number': txn_number}
    finally:
        cursor.close()
        conn.close()

def get_transactions_list(filters):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        sql = "SELECT * FROM accounts_transactions WHERE 1=1"
        params = []
        
        if filters.get('start_date'):
            sql += " AND DATE(txn_date) >= %s"
            params.append(filters['start_date'])
        if filters.get('end_date'):
            sql += " AND DATE(txn_date) <= %s"
            params.append(filters['end_date'])
        if filters.get('txn_type'):
            sql += " AND txn_type = %s"
            params.append(filters['txn_type'])
        if filters.get('account_type'):
            sql += " AND account_type = %s"
            params.append(filters['account_type'])
        if filters.get('payment_method'):
            sql += " AND payment_method = %s"
            params.append(filters['payment_method'])
        if filters.get('search'):
            search_pattern = f"%{filters['search']}%"
            sql += " AND (description LIKE %s OR reference_no LIKE %s OR category_name LIKE %s OR txn_number LIKE %s)"
            params.extend([search_pattern, search_pattern, search_pattern, search_pattern])
            
        sql += " ORDER BY txn_date DESC, id DESC LIMIT 200;"
        cursor.execute(sql, params)
        txns = cursor.fetchall()
        for t in txns:
            t['txn_date'] = t['txn_date'].strftime('%Y-%m-%d %H:%M') if isinstance(t['txn_date'], datetime.datetime) else str(t['txn_date'])
            t['amount'] = float(t['amount'])
        return txns
    finally:
        cursor.close()
        conn.close()

def create_receivable(payload, username):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        rec_no = f"REC-{int(datetime.datetime.now().timestamp())}"
        tot = float(payload['total_amount'])
        paid = float(payload.get('paid_amount', 0))
        rem = tot - paid
        status = 'Paid' if rem <= 0 else ('Partial' if paid > 0 else 'Pending')
        
        cursor.execute("""
            INSERT INTO accounts_receivables
                (receivable_no, customer_name, contact_phone, invoice_ref, total_amount, paid_amount, remaining_balance, due_date, status, notes, created_by)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (rec_no, payload['customer_name'], payload.get('contact_phone', ''), payload.get('invoice_ref', ''), tot, paid, rem, payload['due_date'], status, payload.get('notes', ''), username))
        rec_id = cursor.lastrowid
        
        if paid > 0:
            cursor.execute("""
                INSERT INTO accounts_receivable_payments (receivable_id, payment_date, amount_paid, payment_method, reference_no, notes, created_by)
                VALUES (%s, %s, %s, %s, %s, 'Initial Payment', %s);
            """, (rec_id, datetime.datetime.now(), paid, payload.get('payment_method', 'Cash'), payload.get('reference_no', ''), username))
            
        conn.commit()
        return {'status': 'success', 'receivable_no': rec_no}
    finally:
        cursor.close()
        conn.close()

def record_receivable_payment(rec_id, amount_paid, payment_method, reference_no, notes, username):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT total_amount, paid_amount, remaining_balance FROM accounts_receivables WHERE id = %s;", (rec_id,))
        rec = cursor.fetchone()
        if not rec:
            raise ValueError("Receivable record not found.")
            
        amt = float(amount_paid)
        new_paid = float(rec['paid_amount']) + amt
        new_rem = float(rec['total_amount']) - new_paid
        new_rem = max(0.0, new_rem)
        new_status = 'Paid' if new_rem <= 0 else 'Partial'
        
        cursor.execute("""
            UPDATE accounts_receivables
            SET paid_amount = %s, remaining_balance = %s, status = %s
            WHERE id = %s;
        """, (new_paid, new_rem, new_status, rec_id))
        
        cursor.execute("""
            INSERT INTO accounts_receivable_payments (receivable_id, payment_date, amount_paid, payment_method, reference_no, notes, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
        """, (rec_id, datetime.datetime.now(), amt, payment_method, reference_no, notes, username))
        
        conn.commit()
        return {'status': 'success'}
    finally:
        cursor.close()
        conn.close()

def create_payable(payload, username):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        pay_no = f"PAY-{int(datetime.datetime.now().timestamp())}"
        tot = float(payload['total_amount'])
        paid = float(payload.get('paid_amount', 0))
        rem = tot - paid
        status = 'Paid' if rem <= 0 else ('Partial' if paid > 0 else 'Pending')
        
        cursor.execute("""
            INSERT INTO accounts_payables
                (payable_no, supplier_name, contact_phone, invoice_ref, total_amount, paid_amount, remaining_balance, due_date, status, notes, created_by)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (pay_no, payload['supplier_name'], payload.get('contact_phone', ''), payload.get('invoice_ref', ''), tot, paid, rem, payload['due_date'], status, payload.get('notes', ''), username))
        pay_id = cursor.lastrowid
        
        if paid > 0:
            cursor.execute("""
                INSERT INTO accounts_payable_payments (payable_id, payment_date, amount_paid, payment_method, reference_no, notes, created_by)
                VALUES (%s, %s, %s, %s, %s, 'Initial Payment', %s);
            """, (pay_id, datetime.datetime.now(), paid, payload.get('payment_method', 'Cash'), payload.get('reference_no', ''), username))
            
        conn.commit()
        return {'status': 'success', 'payable_no': pay_no}
    finally:
        cursor.close()
        conn.close()

def record_payable_payment(pay_id, amount_paid, payment_method, reference_no, notes, username):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT total_amount, paid_amount, remaining_balance FROM accounts_payables WHERE id = %s;", (pay_id,))
        pay = cursor.fetchone()
        if not pay:
            raise ValueError("Payable record not found.")
            
        amt = float(amount_paid)
        new_paid = float(pay['paid_amount']) + amt
        new_rem = float(pay['total_amount']) - new_paid
        new_rem = max(0.0, new_rem)
        new_status = 'Paid' if new_rem <= 0 else 'Partial'
        
        cursor.execute("""
            UPDATE accounts_payables
            SET paid_amount = %s, remaining_balance = %s, status = %s
            WHERE id = %s;
        """, (new_paid, new_rem, new_status, pay_id))
        
        cursor.execute("""
            INSERT INTO accounts_payable_payments (payable_id, payment_date, amount_paid, payment_method, reference_no, notes, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
        """, (pay_id, datetime.datetime.now(), amt, payment_method, reference_no, notes, username))
        
        conn.commit()
        return {'status': 'success'}
    finally:
        cursor.close()
        conn.close()

def purge_all_transaction_data():
    """Removes all transactions, receivables, payables, and payment records while preserving users and categories."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM accounts_receivable_payments;")
        cursor.execute("DELETE FROM accounts_receivables;")
        cursor.execute("DELETE FROM accounts_payable_payments;")
        cursor.execute("DELETE FROM accounts_payables;")
        cursor.execute("DELETE FROM accounts_transactions;")
        conn.commit()
        return {'status': 'success', 'message': 'All transactions, receivables, and payables have been purged.'}
    finally:
        cursor.close()
        conn.close()

def delete_transaction(txn_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM accounts_transactions WHERE id = %s;", (txn_id,))
        conn.commit()
        return {'status': 'success'}
    finally:
        cursor.close()
        conn.close()

def delete_receivable(rec_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM accounts_receivable_payments WHERE receivable_id = %s;", (rec_id,))
        cursor.execute("DELETE FROM accounts_receivables WHERE id = %s;", (rec_id,))
        conn.commit()
        return {'status': 'success'}
    finally:
        cursor.close()
        conn.close()

def delete_payable(pay_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM accounts_payable_payments WHERE payable_id = %s;", (pay_id,))
        cursor.execute("DELETE FROM accounts_payables WHERE id = %s;", (pay_id,))
        conn.commit()
        return {'status': 'success'}
    finally:
        cursor.close()
        conn.close()


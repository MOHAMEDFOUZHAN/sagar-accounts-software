import os
import sys
import datetime
import csv
import io
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, Response, send_from_directory

from config import Config
from backend.auth import authenticate_user
from backend.db import get_db_connection
from backend.accounts_engine import (
    get_dashboard_summary,
    add_quick_entry,
    get_transactions_list,
    create_receivable,
    record_receivable_payment,
    create_payable,
    record_payable_payment,
    purge_all_transaction_data,
    delete_transaction,
    delete_receivable,
    delete_payable
)
from backend.reports_engine import generate_profit_and_loss, generate_cash_flow
from backend.simulation_engine import run_accounting_simulation, restore_default_demo_dataset

app = Flask(
    __name__,
    template_folder='frontend',
    static_folder='frontend'
)
app.secret_key = Config.SECRET_KEY

# ----------------------------------------------------
# AUTHENTICATION DECORATOR & MIDDLEWARE
# ----------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('frontend', filename)

# ----------------------------------------------------
# AUTH ROUTES
# ----------------------------------------------------
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard_page'))
    return redirect(url_for('login_page'))

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = authenticate_user(username, password)
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['full_name'] = user['full_name']
            session['role'] = user['role']
            return redirect(url_for('dashboard_page'))
        else:
            return render_template('login.html', error="Invalid username or password.")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

# ----------------------------------------------------
# PAGE ROUTES
# ----------------------------------------------------
@app.route('/dashboard')
@login_required
def dashboard_page():
    summary = get_dashboard_summary()
    return render_template('dashboard.html', active_page='dashboard', summary=summary)

@app.route('/entry')
@login_required
def entry_page():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, name, type FROM accounts_categories ORDER BY type, name;")
    categories = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('entry.html', active_page='entry', categories=categories)

@app.route('/income')
@login_required
def income_page():
    search = request.args.get('search', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    
    filters = {'txn_type': 'Income', 'search': search, 'start_date': start_date, 'end_date': end_date}
    income_list = get_transactions_list(filters)
    total_income_sum = sum(t['amount'] for t in income_list)
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, name FROM accounts_categories WHERE type = 'Revenue' ORDER BY name;")
    income_categories = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return render_template(
        'income.html',
        active_page='income',
        income_list=income_list,
        total_income_sum=total_income_sum,
        income_categories=income_categories,
        filters=filters
    )

@app.route('/expenses')
@login_required
def expenses_page():
    search = request.args.get('search', '').strip()
    account_type = request.args.get('account_type', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    
    filters = {'txn_type': 'Expense', 'search': search, 'account_type': account_type, 'start_date': start_date, 'end_date': end_date}
    expenses_list = get_transactions_list(filters)
    
    total_cogs = sum(t['amount'] for t in expenses_list if t['account_type'] == 'Direct Expense')
    total_opex = sum(t['amount'] for t in expenses_list if t['account_type'] == 'Operating Expense')
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, name, type FROM accounts_categories WHERE type IN ('Direct Expense', 'Operating Expense') ORDER BY type, name;")
    categories = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return render_template(
        'expenses.html',
        active_page='expenses',
        expenses_list=expenses_list,
        total_cogs=total_cogs,
        total_opex=total_opex,
        categories=categories,
        filters=filters
    )

@app.route('/transactions')
@login_required
def transactions_page():
    filters = {
        'search': request.args.get('search', '').strip(),
        'txn_type': request.args.get('txn_type', '').strip(),
        'account_type': request.args.get('account_type', '').strip(),
        'payment_method': request.args.get('payment_method', '').strip(),
        'start_date': request.args.get('start_date', '').strip(),
        'end_date': request.args.get('end_date', '').strip()
    }
    txns = get_transactions_list(filters)
    return render_template('transactions.html', active_page='transactions', transactions=txns, filters=filters)

@app.route('/receivables')
@login_required
def receivables_page():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM accounts_receivables ORDER BY status DESC, due_date ASC;")
    receivables = cursor.fetchall()
    for r in receivables:
        r['due_date'] = r['due_date'].strftime('%Y-%m-%d') if isinstance(r['due_date'], datetime.date) else str(r['due_date'])
        r['total_amount'] = float(r['total_amount'])
        r['paid_amount'] = float(r['paid_amount'])
        r['remaining_balance'] = float(r['remaining_balance'])
    cursor.close()
    conn.close()
    return render_template('receivables.html', active_page='receivables', receivables=receivables)

@app.route('/payables')
@login_required
def payables_page():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM accounts_payables ORDER BY status DESC, due_date ASC;")
    payables = cursor.fetchall()
    for p in payables:
        p['due_date'] = p['due_date'].strftime('%Y-%m-%d') if isinstance(p['due_date'], datetime.date) else str(p['due_date'])
        p['total_amount'] = float(p['total_amount'])
        p['paid_amount'] = float(p['paid_amount'])
        p['remaining_balance'] = float(p['remaining_balance'])
    cursor.close()
    conn.close()
    return render_template('payables.html', active_page='payables', payables=payables)

@app.route('/reports')
@login_required
def reports_page():
    start_date = request.args.get('start_date', '').strip() or None
    end_date = request.args.get('end_date', '').strip() or None
    
    pnl = generate_profit_and_loss(start_date, end_date)
    cash_flow = generate_cash_flow(start_date, end_date)
    
    return render_template(
        'reports.html',
        active_page='reports',
        pnl=pnl,
        cash_flow=cash_flow,
        filters={'start_date': start_date, 'end_date': end_date}
    )

@app.route('/categories')
@login_required
def categories_page():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM accounts_categories ORDER BY type, name;")
    categories = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('categories.html', active_page='categories', categories=categories)

# ----------------------------------------------------
# REST API ENDPOINTS
# ----------------------------------------------------
@app.route('/api/quick-entry', methods=['POST'])
@login_required
def api_quick_entry():
    try:
        data = request.get_json()
        res = add_quick_entry(data, session.get('username', 'admin'))
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/receivables/add', methods=['POST'])
@login_required
def api_add_receivable():
    try:
        data = request.get_json()
        res = create_receivable(data, session.get('username', 'admin'))
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/receivables/pay', methods=['POST'])
@login_required
def api_pay_receivable():
    try:
        data = request.get_json()
        res = record_receivable_payment(
            int(data['rec_id']),
            float(data['amount_paid']),
            data['payment_method'],
            data.get('reference_no', ''),
            'Installment Payment Received',
            session.get('username', 'admin')
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/payables/add', methods=['POST'])
@login_required
def api_add_payable():
    try:
        data = request.get_json()
        res = create_payable(data, session.get('username', 'admin'))
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/payables/pay', methods=['POST'])
@login_required
def api_pay_payable():
    try:
        data = request.get_json()
        res = record_payable_payment(
            int(data['pay_id']),
            float(data['amount_paid']),
            data['payment_method'],
            data.get('reference_no', ''),
            'Disbursement Payment Settled',
            session.get('username', 'admin')
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/categories/add', methods=['POST'])
@login_required
def api_add_category():
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        cat_type = data.get('type', '').strip()
        desc = data.get('description', '').strip()
        if not name or not cat_type:
            raise ValueError("Category name and classification type are required.")
            
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO accounts_categories (name, type, description) VALUES (%s, %s, %s);", (name, cat_type, desc))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/reports/export-csv')
@login_required
def api_export_csv():
    start_date = request.args.get('start_date', '').strip() or None
    end_date = request.args.get('end_date', '').strip() or None
    
    filters = {'start_date': start_date, 'end_date': end_date}
    txns = get_transactions_list(filters)
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Txn Number', 'Date', 'Type', 'Account Type', 'Category', 'Amount (INR)', 'Payment Method', 'Reference', 'Description'])
    for t in txns:
        writer.writerow([t['txn_number'], t['txn_date'], t['txn_type'], t['account_type'], t['category_name'], t['amount'], t['payment_method'], t['reference_no'], t['description']])
        
    output.seek(0)
@app.route('/test-center')
@login_required
def test_center_page():
    return render_template('test_center.html', active_page='test_center')

# ----------------------------------------------------
# REST API ENDPOINTS — SYSTEM & MAINTENANCE
# ----------------------------------------------------
@app.route('/api/system/run-test-simulation', methods=['POST'])
@login_required
def api_run_test_simulation():
    try:
        res = run_accounting_simulation(purge_first=True)
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/system/purge-dummy-data', methods=['POST'])
@login_required
def api_purge_dummy_data():
    try:
        res = purge_all_transaction_data()
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/system/reset-demo-data', methods=['POST'])
@login_required
def api_reset_demo_data():
    try:
        res = restore_default_demo_dataset()
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/transactions/delete', methods=['POST'])
@login_required
def api_delete_transaction():
    try:
        data = request.get_json()
        txn_id = int(data['txn_id'])
        res = delete_transaction(txn_id)
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/receivables/delete', methods=['POST'])
@login_required
def api_delete_receivable():
    try:
        data = request.get_json()
        rec_id = int(data['rec_id'])
        res = delete_receivable(rec_id)
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/payables/delete', methods=['POST'])
@login_required
def api_delete_payable():
    try:
        data = request.get_json()
        pay_id = int(data['pay_id'])
        res = delete_payable(pay_id)
        return jsonify(res)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400


if __name__ == '__main__':
    print(f"[*] Starting Standalone Sagar Accounts Server on http://127.0.0.1:{Config.PORT}")
    app.run(host='0.0.0.0', port=Config.PORT, debug=True)

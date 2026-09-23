import os
import sys
import datetime
import io
import csv
import logging
from functools import wraps

logger = logging.getLogger(__name__)
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    jsonify,
    session,
    Response,
    send_from_directory,
)

from config import Config
from backend.auth import authenticate_user, has_permission, permission_required
from backend.db import get_db_connection
from backend.audit_engine import AuditEngine
from backend.backup_engine import BackupEngine
from backend.db_integrity import DatabaseIntegrityEngine
from backend.sync_engine import (
    sync_all,
    get_live_inventory_valuation,
    get_sync_failures,
    retry_failed_sync,
    get_sync_registry_summary,
)
from backend.accounts_engine import (
    get_dashboard_summary,
    add_manual_entry,
    get_transactions_list,
    get_general_ledger,
    get_chart_of_accounts,
    get_receivables,
    record_receivable_payment,
    get_payables,
    record_payable_payment,
    get_fixed_assets,
    create_fixed_asset,
    get_liabilities_list,
    create_liability,
)
from backend.reports_engine import (
    generate_profit_and_loss,
    generate_balance_sheet,
    generate_tax_report,
    generate_gst_report,
    generate_trial_balance,
    generate_cash_flow,
    get_account_drilldown,
    verify_report_consistency,
    resolve_report_date_range,
)
from backend.coa_engine import ChartOfAccountsEngine
from backend.double_entry_engine import DoubleEntryEngine, DoubleEntryError
from backend.ledger_engine import LedgerEngine
from backend.accounting_rules import AccountingRules
from backend.period_engine import PeriodControlEngine, PeriodClosedError, PeriodLockedError, InvalidAccountingDateError
from backend.opening_balance_engine import OpeningBalanceEngine
from backend.reconciliation_engine import ReconciliationEngine
from backend.health_check import HealthCheckEngine
from backend.validation_engine import AccountingValidator, AccountingValidationError
from backend.business_accounting import BusinessAccountingService

def get_resource_path(relative_path):
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

frontend_folder = get_resource_path('frontend')

app = Flask(
    __name__,
    template_folder=frontend_folder,
    static_folder=frontend_folder
)
app.secret_key = Config.SECRET_KEY
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.jinja_env.globals['has_permission'] = has_permission


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
    return send_from_directory(get_resource_path('frontend'), filename)


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
        client_ip = request.remote_addr or "unknown"
        user, err = authenticate_user(username, password, client_ip)
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['full_name'] = user['full_name']
            session['role'] = user['role']
            return redirect(url_for('dashboard_page'))
        else:
            return render_template('login.html', error=err or "Invalid username or password.")
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
    date_filter = request.args.get('filter', 'all')
    summary = get_dashboard_summary(date_filter if date_filter != 'all' else None)
    return render_template('dashboard.html', active_page='dashboard', summary=summary, current_filter=date_filter)


@app.route('/entry')
@login_required
def entry_page():
    accounts = get_chart_of_accounts()
    # Group accounts by major_type for structured dropdown
    grouped_accounts = {}
    for a in accounts:
        mt = a["major_type"]
        if mt not in grouped_accounts:
            grouped_accounts[mt] = []
        grouped_accounts[mt].append(a)

    return render_template(
        'entry.html',
        active_page='entry',
        grouped_accounts=grouped_accounts,
        current_date=datetime.date.today().strftime('%Y-%m-%d'),
        current_time=datetime.datetime.now().strftime('%H:%M')
    )


@app.route('/sales-ledger')
@login_required
def sales_ledger_page():
    txns = get_transactions_list({"source_module": "sales"})
    total_sales_sum = sum(t['amount'] for t in txns if 'SLS' in t['entry_number'])
    return render_template(
        'sales_ledger.html',
        active_page='sales',
        transactions=txns,
        total_sales_sum=total_sales_sum
    )


@app.route('/payables')
@login_required
def payables_page():
    status = request.args.get('status', '').strip()
    payables_list = get_payables(status if status else None)
    total_outstanding = sum(p['remaining_balance'] for p in payables_list if p['status'] != 'Paid')
    total_paid = sum(p['paid_amount'] for p in payables_list)
    ap_ageing = BusinessAccountingService.get_ap_ageing()
    ap_reconciliation = BusinessAccountingService.get_ap_reconciliation()
    return render_template(
        'payables.html',
        active_page='payables',
        payables=payables_list,
        total_outstanding=total_outstanding,
        total_paid=total_paid,
        current_status=status,
        ap_ageing=ap_ageing,
        ap_reconciliation=ap_reconciliation
    )


@app.route('/receivables')
@login_required
def receivables_page():
    status = request.args.get('status', '').strip()
    recs_list = get_receivables(status if status else None)
    total_receivable = sum(r['total_amount'] for r in recs_list)
    total_paid = sum(r['paid_amount'] for r in recs_list)
    total_pending = sum(r['remaining_balance'] for r in recs_list if r['status'] != 'Paid')
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    total_overdue = sum(r['remaining_balance'] for r in recs_list if (r.get('status') == 'Overdue') or (r['remaining_balance'] > 0 and r.get('due_date') and r['due_date'] < today_str))
    ar_ageing = BusinessAccountingService.get_ar_ageing()
    ar_reconciliation = BusinessAccountingService.get_ar_reconciliation()

    return render_template(
        'receivables.html',
        active_page='receivables',
        receivables=recs_list,
        total_receivable=total_receivable,
        total_paid=total_paid,
        total_pending=total_pending,
        total_overdue=total_overdue,
        total_outstanding=total_pending,
        total_collected=total_paid,
        current_status=status,
        ar_ageing=ar_ageing,
        ar_reconciliation=ar_reconciliation
    )


@app.route('/banking')
@login_required
def banking_page():
    as_of = request.args.get('as_of_date')
    summary = BusinessAccountingService.get_cash_bank_summary(as_of_date=as_of)
    worksheet = ReconciliationEngine.get_bank_reconciliation_worksheet(as_of_date=as_of)
    return render_template(
        'banking.html',
        active_page='banking',
        summary=summary,
        worksheet=worksheet
    )


@app.route('/expenses')
@login_required
def expenses_page():
    # Direct Expenses vs Operating Expenses
    direct_txns = get_transactions_list()
    pnl = generate_profit_and_loss()
    return render_template(
        'expenses.html',
        active_page='expenses',
        cogs_items=pnl['cogs']['lines'],
        total_cogs=pnl['cogs']['total'],
        opex_items=pnl['operating_expenses']['lines'],
        total_opex=pnl['operating_expenses']['total']
    )


@app.route('/assets')
@app.route('/assets-liabilities')
@login_required
def assets_liabilities_page():
    assets = get_fixed_assets()
    liabilities = get_liabilities_list()
    bs = generate_balance_sheet()
    total_fixed_assets = sum(a['current_value'] for a in assets)
    total_loans = sum(l['outstanding_balance'] for l in liabilities if l['status'] == 'Active')
    asset_register = BusinessAccountingService.get_asset_register()
    loan_subledger = BusinessAccountingService.get_loan_subledger()
    return render_template(
        'assets_liabilities.html',
        active_page='assets',
        focus_tab='assets',
        assets=assets,
        liabilities=liabilities,
        total_fixed_assets=total_fixed_assets,
        total_loans=total_loans,
        bs_assets=bs.get('assets', {}),
        bs_liabilities=bs.get('liabilities', {}),
        asset_register=asset_register,
        loan_subledger=loan_subledger
    )


@app.route('/liabilities')
@login_required
def liabilities_page():
    assets = get_fixed_assets()
    liabilities = get_liabilities_list()
    bs = generate_balance_sheet()
    total_fixed_assets = sum(a['current_value'] for a in assets)
    total_loans = sum(l['outstanding_balance'] for l in liabilities if l['status'] == 'Active')
    asset_register = BusinessAccountingService.get_asset_register()
    loan_subledger = BusinessAccountingService.get_loan_subledger()
    return render_template(
        'assets_liabilities.html',
        active_page='liabilities',
        focus_tab='liabilities',
        assets=assets,
        liabilities=liabilities,
        total_fixed_assets=total_fixed_assets,
        total_loans=total_loans,
        bs_assets=bs.get('assets', {}),
        bs_liabilities=bs.get('liabilities', {}),
        asset_register=asset_register,
        loan_subledger=loan_subledger
    )



@app.route('/equity')
@login_required
def equity_page():
    bs = generate_balance_sheet()
    return render_template(
        'equity.html',
        active_page='equity',
        equity_items=bs['equity']['lines'],
        total_equity=bs['equity']['total_equity']
    )


def parse_date_filter(req):
    """
    Parses standardized date range filter for accounting statements.
    Supports:
    - Financial Year (fy_id / financial_year_id)
    - Accounting Period (period_id)
    - Presets: 'today', 'this_week', 'this_month', 'this_year', 'all', 'custom'
    - Explicit start_date / end_date
    Returns (start_date, end_date, active_filter, selected_fy_id, selected_period_id).
    """
    today = datetime.date.today()
    f = req.args.get('filter', '').strip().lower()
    start_date = req.args.get('start_date', '').strip()
    end_date = req.args.get('end_date', '').strip()
    fy_id = req.args.get('fy_id', '').strip() or req.args.get('financial_year_id', '').strip() or None
    period_id = req.args.get('period_id', '').strip() or None

    if period_id:
        f = f'period_{period_id}'
        resolved = resolve_report_date_range(period_id=period_id)
        start_date = resolved['start_date']
        end_date = resolved['end_date']
    elif fy_id:
        f = f'fy_{fy_id}'
        resolved = resolve_report_date_range(financial_year_id=fy_id)
        start_date = resolved['start_date']
        end_date = resolved['end_date']
    elif f == 'today':
        start_date = today.strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
    elif f == 'this_week':
        start_of_week = today - datetime.timedelta(days=today.weekday())
        start_date = start_of_week.strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
    elif f == 'this_month':
        start_date = today.replace(day=1).strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
    elif f == 'this_year':
        # Standard Indian Financial Year (starts April 1st)
        fy_year = today.year if today.month >= 4 else (today.year - 1)
        start_date = f"{fy_year}-04-01"
        end_date = today.strftime('%Y-%m-%d')
    elif f == 'all':
        start_date = None
        end_date = None
    elif start_date or end_date:
        f = 'custom'
    else:
        f = 'all'
        start_date = None
        end_date = None

    return start_date, end_date, f, fy_id, period_id


def get_report_period_context(selected_fy_id=None):
    """Fetches financial years and accounting periods for report dropdown filters."""
    try:
        fys = PeriodControlEngine.get_financial_years()
        periods = PeriodControlEngine.get_periods(financial_year_id=selected_fy_id) if selected_fy_id else PeriodControlEngine.get_periods()
        return fys, periods
    except Exception as e:
        logger.warning(f"Error fetching period context: {e}")
        return [], []


# ====================================================
# 4 MAIN ACCOUNTING SECTIONS
# ====================================================
@app.route('/profit-loss')
@app.route('/pnl')
@login_required
def profit_loss_page():
    start_date, end_date, active_filter, fy_id, period_id = parse_date_filter(request)
    pnl = generate_profit_and_loss(start_date, end_date, financial_year_id=fy_id, period_id=period_id)
    fys, periods = get_report_period_context(fy_id)
    return render_template(
        'profit_loss.html',
        active_page='profit_loss',
        pnl=pnl,
        start_date=start_date or '',
        end_date=end_date or '',
        current_filter=active_filter,
        financial_years=fys,
        accounting_periods=periods,
        selected_fy_id=fy_id,
        selected_period_id=period_id
    )


@app.route('/trial-balance')
@app.route('/tb')
@login_required
def trial_balance_page():
    start_date, end_date, active_filter, fy_id, period_id = parse_date_filter(request)
    as_of = end_date or None
    view_mode = request.args.get('view_mode', 'closing').lower()
    tb = generate_trial_balance(as_of_date=as_of, start_date=start_date, end_date=end_date, financial_year_id=fy_id, period_id=period_id, view_mode=view_mode)
    fys, periods = get_report_period_context(fy_id)
    return render_template(
        'trial_balance.html',
        active_page='trial_balance',
        tb=tb,
        as_of_date=as_of or datetime.date.today().strftime('%Y-%m-%d'),
        start_date=start_date or '',
        end_date=end_date or '',
        current_filter=active_filter,
        view_mode=view_mode,
        financial_years=fys,
        accounting_periods=periods,
        selected_fy_id=fy_id,
        selected_period_id=period_id
    )


@app.route('/balance-sheet')
@app.route('/bs')
@login_required
def balance_sheet_page():
    start_date, end_date, active_filter, fy_id, period_id = parse_date_filter(request)
    as_of = end_date or None
    bs = generate_balance_sheet(as_of_date=as_of, start_date=start_date, financial_year_id=fy_id, period_id=period_id)
    fys, periods = get_report_period_context(fy_id)
    return render_template(
        'balance_sheet.html',
        active_page='balance_sheet',
        bs=bs,
        as_of_date=as_of or datetime.date.today().strftime('%Y-%m-%d'),
        start_date=start_date or '',
        end_date=end_date or '',
        current_filter=active_filter,
        financial_years=fys,
        accounting_periods=periods,
        selected_fy_id=fy_id,
        selected_period_id=period_id
    )


@app.route('/tax')
@login_required
def tax_page():
    start_date, end_date, active_filter, fy_id, period_id = parse_date_filter(request)
    tax_data = generate_tax_report(start_date, end_date, financial_year_id=fy_id, period_id=period_id)
    fys, periods = get_report_period_context(fy_id)
    return render_template(
        'tax.html',
        active_page='tax',
        tax_data=tax_data,
        gst=tax_data['gst'],
        tds=tax_data['tds'],
        start_date=start_date or '',
        end_date=end_date or '',
        current_filter=active_filter,
        financial_years=fys,
        accounting_periods=periods,
        selected_fy_id=fy_id,
        selected_period_id=period_id
    )


@app.route('/api/reports/pnl')
@login_required
@permission_required('view_reports')
def api_reports_pnl():
    start_date, end_date, active_filter, fy_id, period_id = parse_date_filter(request)
    data = generate_profit_and_loss(start_date, end_date, financial_year_id=fy_id, period_id=period_id)
    return jsonify(data)


@app.route('/api/reports/trial-balance')
@login_required
@permission_required('view_reports')
def api_reports_trial_balance():
    start_date, end_date, active_filter, fy_id, period_id = parse_date_filter(request)
    as_of = end_date or None
    view_mode = request.args.get('view_mode', 'closing').lower()
    data = generate_trial_balance(as_of_date=as_of, start_date=start_date, end_date=end_date, financial_year_id=fy_id, period_id=period_id, view_mode=view_mode)
    return jsonify(data)


@app.route('/api/reports/balance-sheet')
@login_required
@permission_required('view_reports')
def api_reports_balance_sheet():
    start_date, end_date, active_filter, fy_id, period_id = parse_date_filter(request)
    as_of = end_date or None
    data = generate_balance_sheet(as_of_date=as_of, start_date=start_date, financial_year_id=fy_id, period_id=period_id)
    return jsonify(data)


@app.route('/api/reports/tax')
@login_required
@permission_required('view_reports')
def api_reports_tax():
    start_date, end_date, active_filter, fy_id, period_id = parse_date_filter(request)
    data = generate_tax_report(start_date=start_date, end_date=end_date, financial_year_id=fy_id, period_id=period_id)
    return jsonify(data)


@app.route('/api/reports/consistency')
@login_required
@permission_required('view_reports')
def api_reports_consistency():
    start_date, end_date, active_filter, fy_id, period_id = parse_date_filter(request)
    as_of = end_date or None
    data = verify_report_consistency(start_date=start_date, end_date=end_date, as_of_date=as_of, financial_year_id=fy_id, period_id=period_id)
    return jsonify(data)


@app.route('/api/reports/drilldown')
@login_required
@permission_required('view_reports')
def api_reports_drilldown():
    code = request.args.get('account_code', '').strip()
    if not code:
        return jsonify({"status": "error", "message": "Account code parameter is required"}), 400
    start_date = request.args.get('start_date', '').strip() or None
    end_date = request.args.get('end_date', '').strip() or None
    fy_id = request.args.get('fy_id', '').strip() or None
    period_id = request.args.get('period_id', '').strip() or None
    source_module = request.args.get('source_module', '').strip() or None
    data = get_account_drilldown(code, start_date=start_date, end_date=end_date, financial_year_id=fy_id, period_id=period_id, source_module=source_module)
    if not data:
        return jsonify({"status": "error", "message": f"Account with code '{code}' not found"}), 404
    return jsonify({"status": "success", "data": data})



@app.route('/ledger')
@login_required
def ledger_page():
    account_id = request.args.get('account_id', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    accounts = get_chart_of_accounts()
    ledger_entries = get_general_ledger(
        account_id if account_id else None,
        start_date if start_date else None,
        end_date if end_date else None
    )

    selected_account = None
    if account_id:
        for a in accounts:
            if str(a['id']) == str(account_id):
                selected_account = a
                break

    return render_template(
        'ledger.html',
        active_page='ledger',
        accounts=accounts,
        ledger_entries=ledger_entries,
        selected_account_id=account_id,
        selected_account=selected_account,
        start_date=start_date,
        end_date=end_date
    )


@app.route('/reports')
@login_required
def reports_page():
    tab = request.args.get('tab', 'pnl')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    pnl = generate_profit_and_loss(start_date if start_date else None, end_date if end_date else None)
    bs = generate_balance_sheet(end_date if end_date else None)
    gst = generate_gst_report(start_date if start_date else None, end_date if end_date else None)
    tb = generate_trial_balance(end_date if end_date else None)

    return render_template(
        'reports.html',
        active_page='reports',
        current_tab=tab,
        pnl=pnl,
        bs=bs,
        gst=gst,
        tb=tb,
        start_date=start_date,
        end_date=end_date
    )


@app.route('/sync')
@login_required
@permission_required('run_synchronization')
def sync_page():
    stats = sync_all()
    failures = get_sync_failures(limit=25)
    summary = get_sync_registry_summary()
    return render_template(
        'sync.html',
        active_page='sync',
        stats=stats,
        failures=failures,
        summary=summary
    )


@app.route('/audit')
@login_required
@permission_required('view_audit')
def audit_page():
    page = int(request.args.get('page', 1))
    entity_type = request.args.get('entity_type')
    action = request.args.get('action')
    user = request.args.get('user')
    search = request.args.get('search')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    data = AuditEngine.get_audit_history(
        entity_type=entity_type,
        action=action,
        user=user,
        search=search,
        start_date=start_date,
        end_date=end_date,
        page=page,
        page_size=30
    )
    return render_template(
        'audit.html',
        active_page='audit',
        logs=data['logs'],
        total_count=data['total_count'],
        current_page=data['page'],
        total_pages=data['total_pages'],
        filter_entity_type=entity_type or '',
        filter_action=action or '',
        filter_user=user or '',
        filter_search=search or '',
        filter_start_date=start_date or '',
        filter_end_date=end_date or ''
    )


@app.route('/backups')
@login_required
@permission_required('backup_database')
def backups_page():
    backups = BackupEngine.list_backups()
    return render_template(
        'backups.html',
        active_page='backups',
        backups=backups
    )


# ----------------------------------------------------
# JSON API ENDPOINTS
# ----------------------------------------------------
@app.route('/api/sync/run', methods=['POST'])
@login_required
@permission_required('run_synchronization')
def api_sync_run():
    try:
        stats = sync_all()
        return jsonify({"status": "success", "stats": stats})
    except Exception as e:
        logger.error(f"Manual sync failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/entry/save', methods=['POST'])
@login_required
@permission_required('create_journal')
def api_entry_save():
    data = request.json or request.form
    try:
        desc = data.get('description', '').strip()
        amount = data.get('amount')
        payment_type = data.get('payment_type', 'Cash')
        account_id = data.get('account_id')
        ref = data.get('reference_no', '').strip()
        notes = data.get('notes', '').strip()
        username = session.get('username', 'admin')

        result = add_manual_entry(
            description=desc,
            amount=amount,
            payment_type=payment_type,
            account_id=account_id,
            reference_no=ref,
            notes=notes,
            created_by=username
        )
        return jsonify({"status": "success", "data": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/receivables/pay', methods=['POST'])
@login_required
@permission_required('create_customer_txn')
def api_receivables_pay():
    data = request.json or request.form
    try:
        rec_id = data.get('receivable_id')
        amount = data.get('amount')
        p_method = data.get('payment_method', 'Bank Transfer')
        ref = data.get('reference_no', '')
        notes = data.get('notes', '')
        username = session.get('username', 'admin')

        res = record_receivable_payment(rec_id, amount, p_method, ref, notes, username)
        return jsonify({"status": "success", "data": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/payables/pay', methods=['POST'])
@login_required
@permission_required('create_supplier_txn')
def api_payables_pay():
    data = request.json or request.form
    try:
        pay_id = data.get('payable_id')
        amount = data.get('amount')
        p_method = data.get('payment_method', 'Bank Transfer')
        ref = data.get('reference_no', '')
        notes = data.get('notes', '')
        username = session.get('username', 'admin')

        res = record_payable_payment(pay_id, amount, p_method, ref, notes, username)
        return jsonify({"status": "success", "data": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/assets/create', methods=['POST'])
@login_required
@permission_required('create_fixed_asset')
def api_assets_create():
    data = request.json or request.form
    try:
        res = create_fixed_asset(
            asset_name=data.get('asset_name'),
            category=data.get('category'),
            purchase_date=data.get('purchase_date'),
            purchase_value=data.get('purchase_value'),
            useful_life_years=data.get('useful_life_years', 5),
            supplier=data.get('supplier'),
            payment_method=data.get('payment_method', 'Bank Transfer'),
            notes=data.get('notes')
        )
        return jsonify({"status": "success", "data": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/liabilities/create', methods=['POST'])
@login_required
@permission_required('create_loan')
def api_liabilities_create():
    data = request.json or request.form
    try:
        res = create_liability(
            title=data.get('title'),
            liability_type=data.get('liability_type'),
            principal_amount=data.get('principal_amount'),
            interest_rate=data.get('interest_rate', 0.0),
            tenure_months=data.get('tenure_months', 0),
            lender=data.get('lender'),
            start_date=data.get('start_date'),
            notes=data.get('notes')
        )
        return jsonify({"status": "success", "data": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/dashboard/metrics')
@login_required
def api_dashboard_metrics():
    df = request.args.get('filter')
    metrics = get_dashboard_summary(df if df != 'all' else None)
    return jsonify({"status": "success", "metrics": metrics})


@app.route('/api/coa/tree')
@login_required
def api_coa_tree():
    """Hierarchical Chart of Accounts Tree."""
    tree = ChartOfAccountsEngine.get_hierarchical_tree()
    return jsonify({"status": "success", "tree": tree})


@app.route('/api/coa/accounts')
@login_required
def api_coa_accounts():
    """Active postable accounts."""
    major_type = request.args.get('major_type')
    accounts = ChartOfAccountsEngine.get_accounts(is_group=False, major_type=major_type, is_active=True)
    return jsonify({"status": "success", "accounts": accounts})


@app.route('/api/coa/create', methods=['POST'])
@login_required
@permission_required('manage_chart_of_accounts')
def api_coa_create():
    """Create account with hierarchy and postability validations."""
    data = request.json or request.form
    try:
        acc_id = ChartOfAccountsEngine.create_account(
            code=data.get('code'),
            name=data.get('name'),
            major_type=data.get('major_type'),
            sub_type=data.get('sub_type'),
            parent_id=data.get('parent_id'),
            normal_balance=data.get('normal_balance'),
            is_group=bool(data.get('is_group', False)),
            description=data.get('description'),
            system_tag=data.get('system_tag'),
            tax_classification=data.get('tax_classification')
        )
        return jsonify({"status": "success", "account_id": acc_id})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/journal/<int:entry_id>')
@login_required
def api_journal_get(entry_id):
    """Audited Journal voucher drilldown."""
    entry = DoubleEntryEngine.get_journal_entry(entry_id)
    if not entry:
        return jsonify({"status": "error", "message": f"Journal Entry #{entry_id} not found"}), 404
    return jsonify({"status": "success", "entry": entry})


@app.route('/api/journal/reverse', methods=['POST'])
@login_required
@permission_required('reverse_transaction')
def api_journal_reverse():
    """Audit-trailed Journal Reversal."""
    data = request.json or request.form
    try:
        entry_id = data.get('entry_id')
        if not entry_id:
            return jsonify({"status": "error", "message": "entry_id is required"}), 400
        reason = data.get('reason', 'User initiated reversal')
        user = session.get('username', 'admin')
        res = DoubleEntryEngine.reverse_journal_entry(entry_id, reason=reason, user=user)
        return jsonify({"status": "success", "reversal": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/transactions/delete', methods=['POST'])
@login_required
@permission_required('reverse_transaction')
def api_transaction_delete():
    """
    Safely reverses an entry rather than physically deleting it,
    enforcing double-entry accounting invariants with full audit trail.
    """
    data = request.json or request.form or {}
    txn_id = data.get('txn_id')
    if not txn_id:
        return jsonify({"status": "error", "error": "Missing transaction ID"}), 400
    try:
        user = session.get('username', 'accounts')
        res = DoubleEntryEngine.reverse_journal_entry(
            txn_id,
            reason="User reversed transaction from portal",
            user=user
        )
        return jsonify({"status": "success", "message": "Transaction safely reversed", "reversal": res})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400


@app.route('/api/ledger')
@login_required
@permission_required('view_ledger')
def api_ledger_statement():
    """Ledger statement with opening balance, movements, and closing balance."""
    account_id = request.args.get('account_id')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    search = request.args.get('search')
    filters = {"search": search} if search else None

    stmt = LedgerEngine.get_ledger_statement(
        account_id=account_id if account_id else None,
        start_date=start_date if start_date else None,
        end_date=end_date if end_date else None,
        filters=filters
    )
    return jsonify({"status": "success", "statement": stmt})


# ----------------------------------------------------
# REPORT EXPORT (CSV / EXCEL)
# ----------------------------------------------------
@app.route('/export/ledger.csv')
@login_required
def export_ledger_csv():
    account_id = request.args.get('account_id')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    entries = get_general_ledger(
        account_id if account_id else None,
        start_date if start_date else None,
        end_date if end_date else None
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Entry Number", "Account Code", "Account Name", "Source", "Reference", "Narration", "Debit", "Credit", "Running Balance"])

    for e in entries:
        writer.writerow([
            e["entry_date"],
            e["entry_number"],
            e["account_code"],
            e["account_name"],
            e["source_module"],
            e["reference_no"] or "",
            e["narration"],
            e["debit"],
            e["credit"],
            e["running_balance"]
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=general_ledger.csv"}
    )


# ----------------------------------------------------
# FINANCIAL CONTROL LAYER: PERIODS & FINANCIAL YEAR
# ----------------------------------------------------
@app.route('/periods')
@login_required
def periods_page():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM financial_years ORDER BY start_date ASC;")
        fys = cur.fetchall()

        req_fy_id = request.args.get('fy_id')
        active_fy = None
        if req_fy_id:
            for f in fys:
                if str(f["id"]) == str(req_fy_id):
                    active_fy = f
                    break
        if not active_fy and fys:
            today_str = datetime.date.today().isoformat()
            for f in fys:
                if str(f["start_date"]) <= today_str <= str(f["end_date"]):
                    active_fy = f
                    break
            if not active_fy:
                active_fy = fys[0]

        periods = []
        if active_fy:
            cur.execute("""
                SELECT * FROM accounting_periods
                WHERE financial_year_id = %s
                ORDER BY period_number ASC;
            """, (active_fy["id"],))
            periods = cur.fetchall()

        cur.execute("""
            SELECT * FROM accounting_audit_trail
            ORDER BY id DESC LIMIT 20;
        """)
        audit_logs = cur.fetchall()

        return render_template(
            'periods.html',
            active_page='periods',
            financial_years=fys,
            active_fy=active_fy,
            periods=periods,
            audit_logs=audit_logs
        )
    finally:
        cur.close()
        conn.close()


@app.route('/api/periods/list')
@login_required
def api_periods_list():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        fy_id = request.args.get('fy_id')
        if fy_id:
            cur.execute("SELECT * FROM accounting_periods WHERE financial_year_id = %s ORDER BY period_number ASC;", (fy_id,))
        else:
            cur.execute("SELECT * FROM accounting_periods ORDER BY start_date ASC;")
        periods = cur.fetchall()
        return jsonify({"status": "success", "periods": periods})
    finally:
        cur.close()
        conn.close()


@app.route('/api/periods/close', methods=['POST'])
@login_required
@permission_required('close_period')
def api_periods_close():
    data = request.json or request.form or {}
    period_id = data.get('period_id')
    reason = data.get('reason', 'Period closing')
    user = session.get('username', 'admin')

    if not period_id:
        return jsonify({"status": "error", "message": "period_id is required."}), 400

    try:
        res = PeriodControlEngine.close_period(period_id, user=user)
        return jsonify(res)
    except (PeriodClosedError, PeriodLockedError, DoubleEntryError, ValueError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"}), 500


@app.route('/api/periods/reopen', methods=['POST'])
@login_required
@permission_required('reopen_period')
def api_periods_reopen():
    data = request.json or request.form or {}
    period_id = data.get('period_id')
    reason = data.get('reason', '').strip()
    user = session.get('username', 'admin')
    role = str(session.get('role', 'accountant')).lower()

    # Permission check: Only admin can reopen closed accounting periods
    if role != 'admin' and user.lower() not in ('admin', 'administrator'):
        return jsonify({"status": "error", "message": "Permission Denied: Only administrators can reopen closed accounting periods."}), 403

    if not period_id or not reason:
        return jsonify({"status": "error", "message": "period_id and a valid audit reason are required."}), 400

    try:
        res = PeriodControlEngine.reopen_period(period_id, user=user, reason=reason)
        return jsonify(res)
    except (PeriodClosedError, PeriodLockedError, ValueError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"}), 500


@app.route('/api/periods/lock', methods=['POST'])
@login_required
@permission_required('lock_period')
def api_periods_lock():
    data = request.json or request.form or {}
    period_id = data.get('period_id')
    reason = data.get('reason', 'Statutory audit lock').strip()
    user = session.get('username', 'admin')
    role = str(session.get('role', 'accountant')).lower()

    if role != 'admin' and user.lower() not in ('admin', 'administrator'):
        return jsonify({"status": "error", "message": "Permission Denied: Only administrators can lock accounting periods."}), 403

    try:
        res = PeriodControlEngine.lock_period(period_id, user=user, reason=reason)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/periods/unlock', methods=['POST'])
@login_required
@permission_required('unlock_period')
def api_periods_unlock():
    data = request.json or request.form or {}
    period_id = data.get('period_id')
    reason = data.get('reason', 'Adjustment unlock').strip()
    user = session.get('username', 'admin')
    role = str(session.get('role', 'accountant')).lower()

    if role != 'admin' and user.lower() not in ('admin', 'administrator'):
        return jsonify({"status": "error", "message": "Permission Denied: Only administrators can unlock accounting periods."}), 403

    try:
        res = PeriodControlEngine.unlock_period(period_id, user=user, reason=reason)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/financial-years/create', methods=['POST'])
@login_required
@permission_required('close_period')
def api_financial_year_create():
    data = request.json or request.form or {}
    name = data.get('name', '').strip()
    start_date = data.get('start_date', '').strip()
    end_date = data.get('end_date', '').strip()
    user = session.get('username', 'admin')
    role = str(session.get('role', 'accountant')).lower()

    if role != 'admin' and user.lower() not in ('admin', 'administrator'):
        return jsonify({"status": "error", "message": "Permission Denied: Only administrators can create financial years."}), 403

    try:
        res = PeriodControlEngine.create_financial_year(name, start_date, end_date, user=user)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# FINANCIAL CONTROL LAYER: OPENING BALANCES
# ----------------------------------------------------
@app.route('/opening-balances')
@login_required
def opening_balances_page():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        # Fetch postable leaf accounts
        cur.execute("""
            SELECT id, code, name, major_type, sub_type, normal_balance
            FROM accounts_chart
            WHERE is_postable = 1 AND is_active = 1 AND is_group = 0
            ORDER BY code ASC;
        """)
        postable_accounts = cur.fetchall()

        # Coordinated live inventory
        try:
            suggested_inv = OpeningBalanceEngine.get_suggested_inventory_opening()
            suggested_inv_val = float(suggested_inv.get("stock_valuation", 0.0))
        except Exception:
            suggested_inv_val = 0.0

        # Existing posted opening vouchers
        cur.execute("""
            SELECT id, entry_number, entry_date, narration, total_debit, total_credit, posted_by, status
            FROM journal_entries
            WHERE is_opening = 1
            ORDER BY id DESC;
        """)
        posted_openings = cur.fetchall()

        return render_template(
            'opening_balances.html',
            active_page='opening_balances',
            postable_accounts=postable_accounts,
            suggested_inventory_val=suggested_inv_val,
            posted_openings=posted_openings
        )
    finally:
        cur.close()
        conn.close()


@app.route('/api/opening-balances/post', methods=['POST'])
@login_required
@permission_required('finalize_opening_balance')
def api_opening_balances_post():
    data = request.json or {}
    lines = data.get('lines', [])
    opening_date = data.get('opening_date')
    narration = data.get('narration', 'Baseline Opening Balances')
    user = session.get('username', 'admin')

    try:
        res = OpeningBalanceEngine.post_opening_balances(
            opening_date=opening_date,
            lines_data=lines,
            narration=narration,
            user=user
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# FINANCIAL CONTROL LAYER: RECONCILIATION
# ----------------------------------------------------
@app.route('/reconciliation')
@login_required
def reconciliation_page():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT r.*, ac.code as account_code, ac.name as account_name
            FROM reconciliations r
            JOIN accounts_chart ac ON r.account_id = ac.id
            ORDER BY r.id DESC LIMIT 20;
        """)
        recon_history = cur.fetchall()
        return render_template(
            'reconciliation.html',
            active_page='reconciliation',
            recon_history=recon_history
        )
    finally:
        cur.close()
        conn.close()


@app.route('/api/reconciliation/run')
@login_required
def api_reconciliation_run():
    target = request.args.get('target', 'bank').lower().strip()
    try:
        if target == 'bank':
            res = ReconciliationEngine.reconcile_bank()
        elif target == 'cash':
            res = ReconciliationEngine.reconcile_cash()
        elif target == 'customer':
            res = ReconciliationEngine.reconcile_customers()
        elif target == 'supplier':
            res = ReconciliationEngine.reconcile_suppliers()
        elif target == 'inventory':
            res = ReconciliationEngine.reconcile_inventory()
        elif target == 'gst':
            res = ReconciliationEngine.reconcile_gst()
        elif target == 'tds':
            res = ReconciliationEngine.reconcile_tds()
        else:
            return jsonify({"status": "error", "message": f"Unknown target '{target}'."}), 400

        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/reconciliation/adjust', methods=['POST'])
@login_required
@permission_required('create_reconciliation_adjustment')
def api_reconciliation_adjust():
    data = request.json or {}
    account_code = data.get('account_code')
    offset_code = data.get('offset_code')
    amount = data.get('amount')
    direction = data.get('direction', 'DEBIT_TARGET')
    narration = data.get('narration', 'Reconciliation Adjustment')
    user = session.get('username', 'admin')

    try:
        res = ReconciliationEngine.post_reconciliation_adjustment(
            account_code=account_code,
            offset_code=offset_code,
            amount=amount,
            direction=direction,
            narration=narration,
            user=user
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# FINANCIAL CONTROL LAYER: HEALTH CHECK & CORRECTIONS
# ----------------------------------------------------
@app.route('/health-check')
@login_required
def health_check_page():
    return render_template('health_check.html', active_page='health_check')


@app.route('/api/health-check/run')
@login_required
def api_health_check_run():
    try:
        res = HealthCheckEngine.run_full_health_check()
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/journal/correct', methods=['POST'])
@login_required
@permission_required('correct_transaction')
def api_journal_correct():
    data = request.json or {}
    entry_id = data.get('entry_id')
    reason = data.get('reason', 'Transaction correction')
    user = session.get('username', 'admin')
    reversal_date = data.get('reversal_date')
    corrected_entry_data = data.get('corrected_entry_data')
    corrected_lines_data = data.get('corrected_lines_data')

    if not entry_id or not corrected_lines_data:
        return jsonify({"status": "error", "message": "entry_id and corrected_lines_data are required."}), 400

    try:
        res = DoubleEntryEngine.correct_journal_entry(
            entry_id=entry_id,
            corrected_entry_data=corrected_entry_data or {},
            corrected_lines_data=corrected_lines_data,
            reason=reason,
            user=user,
            reversal_date=reversal_date
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ====================================================
# LAYER 4: BUSINESS ACCOUNTING APIS
# ====================================================

# ----------------------------------------------------
# 1. ACCOUNTS RECEIVABLE (AR)
# ----------------------------------------------------
@app.route('/api/ar/ageing')
@login_required
def api_ar_ageing():
    as_of = request.args.get('as_of_date')
    data = BusinessAccountingService.get_ar_ageing(as_of_date=as_of)
    return jsonify({"status": "success", "data": data})


@app.route('/api/ar/ledger')
@login_required
def api_ar_ledger():
    customer = request.args.get('customer_name', '').strip()
    if not customer:
        return jsonify({"status": "error", "message": "customer_name parameter is required"}), 400
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    data = BusinessAccountingService.get_customer_ledger(customer, start_date=start_date, end_date=end_date)
    return jsonify({"status": "success", "data": data})


@app.route('/api/ar/reconciliation')
@login_required
def api_ar_reconciliation():
    as_of = request.args.get('as_of_date')
    data = BusinessAccountingService.get_ar_reconciliation(as_of_date=as_of)
    return jsonify({"status": "success", "data": data})


@app.route('/api/ar/credit-note', methods=['POST'])
@login_required
@permission_required('create_customer_txn')
def api_ar_credit_note():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_customer_credit_note({
            "customer_name": data.get("customer_name"),
            "receivable_id": data.get("receivable_id"),
            "invoice_ref": data.get("invoice_ref"),
            "amount": data.get("amount"),
            "tax_amount": data.get("tax_amount", 0.0),
            "date": data.get("date"),
            "reason": data.get("reason"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/ar/advance', methods=['POST'])
@login_required
@permission_required('create_customer_txn')
def api_ar_advance():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_customer_advance({
            "customer_name": data.get("customer_name"),
            "amount": data.get("amount"),
            "payment_method": data.get("payment_method", "Bank Transfer"),
            "date": data.get("date"),
            "narration": data.get("narration"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/ar/writeoff', methods=['POST'])
@login_required
@permission_required('create_customer_txn')
def api_ar_writeoff():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_customer_writeoff({
            "customer_name": data.get("customer_name"),
            "receivable_id": data.get("receivable_id"),
            "amount": data.get("amount"),
            "date": data.get("date"),
            "reason": data.get("reason"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# 2. ACCOUNTS PAYABLE (AP)
# ----------------------------------------------------
@app.route('/api/ap/ageing')
@login_required
def api_ap_ageing():
    as_of = request.args.get('as_of_date')
    data = BusinessAccountingService.get_ap_ageing(as_of_date=as_of)
    return jsonify({"status": "success", "data": data})


@app.route('/api/ap/ledger')
@login_required
def api_ap_ledger():
    supplier = request.args.get('supplier_name', '').strip()
    if not supplier:
        return jsonify({"status": "error", "message": "supplier_name parameter is required"}), 400
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    data = BusinessAccountingService.get_supplier_ledger(supplier, start_date=start_date, end_date=end_date)
    return jsonify({"status": "success", "data": data})


@app.route('/api/ap/reconciliation')
@login_required
def api_ap_reconciliation():
    as_of = request.args.get('as_of_date')
    data = BusinessAccountingService.get_ap_reconciliation(as_of_date=as_of)
    return jsonify({"status": "success", "data": data})


@app.route('/api/ap/debit-note', methods=['POST'])
@login_required
@permission_required('create_supplier_txn')
def api_ap_debit_note():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_supplier_debit_note({
            "supplier_name": data.get("supplier_name"),
            "payable_id": data.get("payable_id"),
            "invoice_ref": data.get("invoice_ref"),
            "amount": data.get("amount"),
            "tax_amount": data.get("tax_amount", 0.0),
            "date": data.get("date"),
            "reason": data.get("reason"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/ap/advance', methods=['POST'])
@login_required
@permission_required('create_supplier_txn')
def api_ap_advance():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_supplier_advance({
            "supplier_name": data.get("supplier_name"),
            "amount": data.get("amount"),
            "payment_method": data.get("payment_method", "Bank Transfer"),
            "date": data.get("date"),
            "narration": data.get("narration"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# 3. CASH & BANK ACCOUNTING
# ----------------------------------------------------
@app.route('/api/banking/summary')
@login_required
def api_banking_summary():
    as_of = request.args.get('as_of_date')
    data = BusinessAccountingService.get_cash_bank_summary(as_of_date=as_of)
    return jsonify({"status": "success", "data": data})


@app.route('/api/banking/transfer', methods=['POST'])
@login_required
@permission_required('create_journal')
def api_banking_transfer():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_contra_transfer({
            "from_account_code": data.get("from_account_code"),
            "to_account_code": data.get("to_account_code"),
            "amount": data.get("amount"),
            "date": data.get("date"),
            "reference_no": data.get("reference_no"),
            "narration": data.get("narration"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/banking/charges', methods=['POST'])
@login_required
@permission_required('create_expense')
def api_banking_charges():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_bank_charges({
            "bank_account_code": data.get("bank_account_code", "1020"),
            "amount": data.get("amount"),
            "date": data.get("date"),
            "reference_no": data.get("reference_no"),
            "description": data.get("description"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# 4. BANK RECONCILIATION
# ----------------------------------------------------
@app.route('/api/banking/reconciliation/worksheet')
@login_required
def api_bank_reconciliation_worksheet():
    acc_id = request.args.get('account_id')
    as_of = request.args.get('as_of_date')
    stmt_bal = request.args.get('statement_balance')
    if stmt_bal:
        try:
            stmt_bal = float(stmt_bal)
        except Exception:
            stmt_bal = None
    data = ReconciliationEngine.get_bank_reconciliation_worksheet(
        account_id=acc_id if acc_id else None,
        as_of_date=as_of if as_of else None,
        statement_balance=stmt_bal
    )
    return jsonify({"status": "success", "data": data})


# ----------------------------------------------------
# 5. FIXED ASSETS & DEPRECIATION
# ----------------------------------------------------
@app.route('/api/assets/register')
@login_required
def api_assets_register():
    as_of = request.args.get('as_of_date')
    data = BusinessAccountingService.get_asset_register(as_of_date=as_of)
    return jsonify({"status": "success", "data": data})


@app.route('/api/assets/depreciate', methods=['POST'])
@login_required
@permission_required('post_journal')
def api_assets_depreciate():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_periodic_depreciation(
            asset_id=data.get("asset_id"),
            depreciation_date=data.get("depreciation_date"),
            depreciation_amount=data.get("amount"),
            user=user
        )
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/assets/dispose', methods=['POST'])
@login_required
@permission_required('post_journal')
def api_assets_dispose():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_asset_disposal({
            "asset_id": data.get("asset_id"),
            "disposal_date": data.get("disposal_date"),
            "disposal_proceeds": data.get("disposal_proceeds", 0.0),
            "payment_method": data.get("payment_method", "Bank Transfer"),
            "notes": data.get("notes"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# 6. LOAN ACCOUNTING
# ----------------------------------------------------
@app.route('/api/loans/subledger')
@login_required
def api_loans_subledger():
    as_of = request.args.get('as_of_date')
    data = BusinessAccountingService.get_loan_subledger(as_of_date=as_of)
    return jsonify({"status": "success", "data": data})


@app.route('/api/loans/repay', methods=['POST'])
@login_required
@permission_required('create_loan')
def api_loans_repay():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_loan_repayment_installment({
            "liability_id": data.get("liability_id"),
            "principal_amount": data.get("principal_amount"),
            "interest_amount": data.get("interest_amount", 0.0),
            "payment_method": data.get("payment_method", "Bank Transfer"),
            "payment_date": data.get("payment_date"),
            "reference_no": data.get("reference_no"),
            "notes": data.get("notes"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# 7. CAPITAL & DRAWINGS
# ----------------------------------------------------
@app.route('/api/equity/contribute', methods=['POST'])
@login_required
@permission_required('create_capital_drawings')
def api_equity_contribute():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_capital({
            "amount": data.get("amount"),
            "payment_method": data.get("payment_method", "Bank Transfer"),
            "investor_name": data.get("investor_name", "Owner"),
            "narration": data.get("narration"),
            "date": data.get("date"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/equity/drawings', methods=['POST'])
@login_required
@permission_required('create_capital_drawings')
def api_equity_drawings():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_drawings({
            "amount": data.get("amount"),
            "payment_method": data.get("payment_method", "Cash"),
            "owner_name": data.get("owner_name", "Owner"),
            "narration": data.get("narration"),
            "date": data.get("date"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# 8. EXPENSE ACCOUNTING
# ----------------------------------------------------
@app.route('/api/expenses/record', methods=['POST'])
@login_required
@permission_required('create_expense')
def api_expenses_record():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_expense({
            "amount": data.get("amount"),
            "category": data.get("category", "Office Expenses"),
            "description": data.get("description"),
            "payment_method": data.get("payment_method", "Cash"),
            "date": data.get("date"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ----------------------------------------------------
# 9. INVENTORY & COGS ACCOUNTING
# ----------------------------------------------------
@app.route('/api/inventory/reconciliation')
@login_required
def api_inventory_reconciliation():
    as_of = request.args.get('as_of_date')
    data = BusinessAccountingService.get_inventory_reconciliation(as_of_date=as_of)
    return jsonify({"status": "success", "data": data})


@app.route('/api/inventory/adjust', methods=['POST'])
@login_required
@permission_required('post_business_transaction')
def api_inventory_adjust():
    data = request.json or request.form or {}
    try:
        user = session.get('username', 'admin')
        res = AccountingRules.post_inventory_adjustment({
            "product_code": data.get("product_code", "ITEM"),
            "qty_change": data.get("qty_change"),
            "unit_cost": data.get("unit_cost"),
            "adjustment_type": data.get("adjustment_type", "damage"),
            "reason": data.get("reason"),
            "date": data.get("date"),
            "user": user
        })
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# =============================================================================
# LAYER 5: DATA & SECURITY API ENDPOINTS
# =============================================================================

# 1. AUDIT TRAIL ENDPOINTS
@app.route('/api/audit/trail')
@login_required
@permission_required('view_audit')
def api_audit_trail():
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 50))
    data = AuditEngine.get_audit_history(
        entity_type=request.args.get('entity_type'),
        action=request.args.get('action'),
        user=request.args.get('user'),
        search=request.args.get('search'),
        start_date=request.args.get('start_date'),
        end_date=request.args.get('end_date'),
        page=page,
        page_size=page_size
    )
    return jsonify({"status": "success", "data": data})


@app.route('/api/audit/entity/<entity_type>/<entity_id>')
@login_required
@permission_required('view_audit')
def api_audit_entity_trail(entity_type, entity_id):
    trail = AuditEngine.get_entity_trail(entity_type, entity_id)
    return jsonify({"status": "success", "entity_type": entity_type, "entity_id": entity_id, "trail": trail})


# 2. BACKUP & DISASTER RECOVERY ENDPOINTS
@app.route('/api/backups/list')
@login_required
@permission_required('verify_backup')
def api_backups_list():
    backups = BackupEngine.list_backups()
    return jsonify({"status": "success", "backups": backups})


@app.route('/api/backups/create', methods=['POST'])
@login_required
@permission_required('backup_database')
def api_backups_create():
    data = request.json or request.form or {}
    notes = data.get('notes')
    b_type = data.get('backup_type', 'manual')
    user = session.get('username', 'admin')
    try:
        res = BackupEngine.create_backup(backup_type=b_type, notes=notes, user=user)
        return jsonify(res)
    except Exception as e:
        logger.error(f"Backup creation failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/backups/verify', methods=['POST'])
@login_required
@permission_required('verify_backup')
def api_backups_verify():
    data = request.json or request.form or {}
    target = data.get('filename') or data.get('backup_id')
    if not target:
        return jsonify({"status": "error", "message": "filename or backup_id required."}), 400
    res = BackupEngine.verify_backup(target)
    return jsonify(res)


@app.route('/api/backups/restore', methods=['POST'])
@login_required
@permission_required('restore_database')
def api_backups_restore():
    data = request.json or request.form or {}
    target = data.get('filename') or data.get('backup_id')
    reason = data.get('reason', 'User initiated database restore')
    user = session.get('username', 'admin')
    if not target:
        return jsonify({"status": "error", "message": "filename or backup_id required."}), 400

    try:
        res = BackupEngine.restore_backup(filename_or_id=target, user=user, reason=reason)
        return jsonify(res)
    except Exception as e:
        logger.error(f"Database restore failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# 3. SYNCHRONIZATION FAILURE & RECOVERY ENDPOINTS
@app.route('/api/sync/failures')
@login_required
@permission_required('run_synchronization')
def api_sync_failures():
    failures = get_sync_failures()
    return jsonify({"status": "success", "failures": failures})


@app.route('/api/sync/retry', methods=['POST'])
@login_required
@permission_required('run_synchronization')
def api_sync_retry():
    data = request.json or request.form or {}
    reg_id = data.get('registry_id')
    user = session.get('username', 'admin')
    res = retry_failed_sync(registry_id=reg_id, user=user)
    return jsonify(res)


@app.route('/api/sync/summary')
@login_required
@permission_required('run_synchronization')
def api_sync_summary():
    summary = get_sync_registry_summary()
    return jsonify({"status": "success", "summary": summary})


# 4. DATABASE INTEGRITY ENDPOINT
@app.route('/api/system/integrity-check')
@login_required
@permission_required('view_audit')
def api_system_integrity():
    report = DatabaseIntegrityEngine.verify_database_integrity()
    return jsonify({"status": "success", "report": report})


if __name__ == '__main__':
    # 1. Initialize database schema automatically if not exists
    try:
        from init_db import init_database
        init_database()
    except Exception as e:
        logger.warning(f"Database initialization check note: {e}")

    # 2. Automatically open browser
    def _open_browser():
        import time
        import webbrowser
        time.sleep(1.2)
        webbrowser.open(f"http://127.0.0.1:{Config.PORT}")

    import threading
    threading.Thread(target=_open_browser, daemon=True).start()

    port = Config.PORT
    print("=" * 65)
    print("      SAGAR ACCOUNTS SOFTWARE - UNIFIED FINANCIAL ENGINE")
    print("=" * 65)
    print(f"[*] Server starting on port {port}...")
    print(f"[*] Dashboard URL: http://127.0.0.1:{port}")
    print("=" * 65)
    app.run(host='0.0.0.0', port=port, debug=False)

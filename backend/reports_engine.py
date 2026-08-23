import datetime
from backend.db import get_db_connection

def generate_profit_and_loss(start_date=None, end_date=None):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Date Filter clause
        date_where = ""
        params = []
        if start_date:
            date_where += " AND DATE(txn_date) >= %s"
            params.append(start_date)
        if end_date:
            date_where += " AND DATE(txn_date) <= %s"
            params.append(end_date)
            
        # 1. Total Revenue Breakdown by Category
        cursor.execute(f"""
            SELECT category_name, SUM(amount) AS total
            FROM accounts_transactions
            WHERE account_type = 'Revenue' {date_where}
            GROUP BY category_name;
        """, params)
        revenue_items = cursor.fetchall()
        total_revenue = sum(float(r['total']) for r in revenue_items)
        
        # 2. Total Direct Expenses (COGS) Breakdown by Category
        cursor.execute(f"""
            SELECT category_name, SUM(amount) AS total
            FROM accounts_transactions
            WHERE account_type = 'Direct Expense' {date_where}
            GROUP BY category_name;
        """, params)
        cogs_items = cursor.fetchall()
        total_cogs = sum(float(c['total']) for c in cogs_items)
        
        # Gross Profit = Total Revenue - Total COGS
        gross_profit = total_revenue - total_cogs
        
        # 3. Total Operating Expenses Breakdown by Category
        cursor.execute(f"""
            SELECT category_name, SUM(amount) AS total
            FROM accounts_transactions
            WHERE account_type = 'Operating Expense' {date_where}
            GROUP BY category_name;
        """, params)
        opex_items = cursor.fetchall()
        total_opex = sum(float(o['total']) for o in opex_items)
        
        # Net Profit = Gross Profit - Total Operating Expenses
        net_profit = gross_profit - total_opex
        
        return {
            'period': {
                'start_date': start_date or 'All Time',
                'end_date': end_date or 'All Time'
            },
            'revenue': {
                'categories': [{'category': r['category_name'], 'amount': float(r['total'])} for r in revenue_items],
                'total': total_revenue
            },
            'cogs': {
                'categories': [{'category': c['category_name'], 'amount': float(c['total'])} for c in cogs_items],
                'total': total_cogs
            },
            'gross_profit': gross_profit,
            'operating_expenses': {
                'categories': [{'category': o['category_name'], 'amount': float(o['total'])} for o in opex_items],
                'total': total_opex
            },
            'net_profit': net_profit
        }
    finally:
        cursor.close()
        conn.close()

def generate_cash_flow(start_date=None, end_date=None):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        date_where = ""
        params = []
        if start_date:
            date_where += " AND DATE(txn_date) >= %s"
            params.append(start_date)
        if end_date:
            date_where += " AND DATE(txn_date) <= %s"
            params.append(end_date)
            
        cursor.execute(f"""
            SELECT payment_method, txn_type, SUM(amount) AS total
            FROM accounts_transactions
            WHERE 1=1 {date_where}
            GROUP BY payment_method, txn_type;
        """, params)
        rows = cursor.fetchall()
        
        methods = {}
        for r in rows:
            pm = r['payment_method']
            tt = r['txn_type']
            amt = float(r['total'])
            if pm not in methods:
                methods[pm] = {'inflow': 0.0, 'outflow': 0.0, 'net': 0.0}
            if tt == 'Income':
                methods[pm]['inflow'] += amt
            elif tt == 'Expense':
                methods[pm]['outflow'] += amt
            methods[pm]['net'] = methods[pm]['inflow'] - methods[pm]['outflow']
            
        total_inflow = sum(m['inflow'] for m in methods.values())
        total_outflow = sum(m['outflow'] for m in methods.values())
        
        return {
            'methods': methods,
            'total_inflow': total_inflow,
            'total_outflow': total_outflow,
            'net_cash_flow': total_inflow - total_outflow
        }
    finally:
        cursor.close()
        conn.close()

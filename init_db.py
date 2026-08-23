import os
import datetime
from config import Config
from backend.db import get_db_connection, get_db_mode
from werkzeug.security import generate_password_hash

def init_database():
    mode = get_db_mode()
    print(f"[*] Initializing Database (Engine: {mode.upper()})...")
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # 1. Create tables
    tables = [
        """
        CREATE TABLE IF NOT EXISTS accounts_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            full_name VARCHAR(100),
            role VARCHAR(20) DEFAULT 'accountant',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS accounts_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL,
            type VARCHAR(50) NOT NULL,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS accounts_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_number VARCHAR(30) UNIQUE NOT NULL,
            txn_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            txn_type VARCHAR(30) NOT NULL,
            account_type VARCHAR(50) NOT NULL,
            category_id INT,
            category_name VARCHAR(100) NOT NULL,
            amount DECIMAL(12, 2) NOT NULL,
            payment_method VARCHAR(30) DEFAULT 'Cash',
            reference_no VARCHAR(100),
            description TEXT,
            created_by VARCHAR(50) DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS accounts_receivables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receivable_no VARCHAR(30) UNIQUE NOT NULL,
            customer_name VARCHAR(100) NOT NULL,
            contact_phone VARCHAR(20),
            invoice_ref VARCHAR(100),
            total_amount DECIMAL(12, 2) NOT NULL,
            paid_amount DECIMAL(12, 2) DEFAULT 0.00,
            remaining_balance DECIMAL(12, 2) NOT NULL,
            due_date DATE NOT NULL,
            status VARCHAR(20) DEFAULT 'Pending',
            notes TEXT,
            created_by VARCHAR(50) DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS accounts_receivable_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receivable_id INT NOT NULL,
            payment_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            amount_paid DECIMAL(12, 2) NOT NULL,
            payment_method VARCHAR(30) DEFAULT 'Cash',
            reference_no VARCHAR(100),
            notes TEXT,
            created_by VARCHAR(50) DEFAULT 'admin'
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS accounts_payables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payable_no VARCHAR(30) UNIQUE NOT NULL,
            supplier_name VARCHAR(100) NOT NULL,
            contact_phone VARCHAR(20),
            invoice_ref VARCHAR(100),
            total_amount DECIMAL(12, 2) NOT NULL,
            paid_amount DECIMAL(12, 2) DEFAULT 0.00,
            remaining_balance DECIMAL(12, 2) NOT NULL,
            due_date DATE NOT NULL,
            status VARCHAR(20) DEFAULT 'Pending',
            notes TEXT,
            created_by VARCHAR(50) DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS accounts_payable_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payable_id INT NOT NULL,
            payment_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            amount_paid DECIMAL(12, 2) NOT NULL,
            payment_method VARCHAR(30) DEFAULT 'Cash',
            reference_no VARCHAR(100),
            notes TEXT,
            created_by VARCHAR(50) DEFAULT 'admin'
        );
        """
    ]
    
    for t_sql in tables:
        if mode == 'mysql':
            t_sql = t_sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'INT AUTO_INCREMENT PRIMARY KEY')
        cursor.execute(t_sql)
    conn.commit()
    print("[+] All tables verified / created.")
    
    # 2. Seed Default Users
    admin_pw = generate_password_hash("admin123")
    acc_pw = generate_password_hash("account123")
    
    cursor.execute("SELECT COUNT(*) FROM accounts_users WHERE username = %s;", ('admin',))
    row = cursor.fetchone()
    count = list(row.values())[0] if isinstance(row, dict) else row[0]
    
    if count == 0:
        cursor.execute("""
            INSERT INTO accounts_users (username, password_hash, full_name, role)
            VALUES ('admin', %s, 'Accounts Admin', 'admin');
        """, (admin_pw,))
        cursor.execute("""
            INSERT INTO accounts_users (username, password_hash, full_name, role)
            VALUES ('accountant', %s, 'Senior Accountant', 'accountant');
        """, (acc_pw,))
        conn.commit()
        print("[+] Users seeded.")
    
    # 3. Seed Standard Categories as defined in PDF
    default_categories = [
        # Revenue
        ('Sales Revenue', 'Revenue', 'Income generated from core retail sales activities'),
        ('Service Revenue', 'Revenue', 'Income from technical service and maintenance'),
        ('Interest Earned', 'Revenue', 'Interest income earned on investments and bank deposits'),
        ('Other Income', 'Revenue', 'Miscellaneous non-operational income'),
        
        # Direct Expenses (COGS)
        ('Raw Materials', 'Direct Expense', 'Cost of raw materials used for production'),
        ('Direct Wages', 'Direct Expense', 'Wages paid directly to production staff'),
        ('Factory Rent', 'Direct Expense', 'Factory building rent or lease amount'),
        ('Factory Utilities', 'Direct Expense', 'Factory coal, gas, water, and power lighting'),
        ('Raw Material Freight', 'Direct Expense', 'Freight charges for transporting raw materials'),
        
        # Operating Expenses
        ('Marketing & Advertisements', 'Operating Expense', 'Expenses incurred for advertising & promotions'),
        ('Shop Rent', 'Operating Expense', 'Commercial retail shop building rent'),
        ('Stationery', 'Operating Expense', 'Office stationery, paper, printing supplies'),
        ('Postage & Courier', 'Operating Expense', 'Postal, courier, and dispatch charges'),
        ('Delivery Charges', 'Operating Expense', 'Logistics and delivery service costs'),
        ('Insurance Expenses', 'Operating Expense', 'Insurance premium payments'),
        ('Depreciation', 'Operating Expense', 'Asset depreciation charges'),
        ('Audit Fees', 'Operating Expense', 'Legal, audit, and professional services'),
        ('Office Electricity/Water', 'Operating Expense', 'Administrative office utility bills'),
        ('Repair & Maintenance', 'Operating Expense', 'Equipment, vehicle & premises maintenance'),
        ('Travelling Expense', 'Operating Expense', 'Business travel & transportation allowance'),
        ('Shop Expense', 'Operating Expense', 'Daily retail counter operational expenses'),
        ('Employee Salary', 'Operating Expense', 'Regular employee monthly payroll'),
        ('Administration Expenses', 'Operating Expense', 'General administrative costs'),
        ('Petty Expenses', 'Operating Expense', 'Small daily incidental expenses'),
        
        # Assets
        ('Accounts Receivable', 'Asset', 'Outstanding payments due from customers'),
        ('Inventory Value', 'Asset', 'Valuation of merchandise stock on hand'),
        ('Equipment & Machinery', 'Asset', 'Value of equipment owned by business'),
        ('Vehicles', 'Asset', 'Value of motor vehicles owned by business'),
        ('Bank Balance', 'Asset', 'Cash balance in corporate bank accounts'),
        
        # Liabilities
        ('Accounts Payable', 'Liability', 'Outstanding payments owed to suppliers'),
        ('Payroll Payable', 'Liability', 'Salaries yet to be disbursed to employees'),
        ('Taxes Payable', 'Liability', 'GST/Tax liabilities due to tax authority'),
        ('Business Loans', 'Liability', 'Short and long term commercial loan debts'),
        
        # Equity
        ('Capital', 'Equity', 'Funds contributed by the owner'),
        ('Drawing', 'Equity', 'Amount withdrawn by the owner for personal use')
    ]
    
    cursor.execute("SELECT COUNT(*) FROM accounts_categories;")
    row = cursor.fetchone()
    cat_count = list(row.values())[0] if isinstance(row, dict) else row[0]
    
    if cat_count == 0:
        for cat_name, cat_type, cat_desc in default_categories:
            cursor.execute("""
                INSERT INTO accounts_categories (name, type, description)
                VALUES (%s, %s, %s);
            """, (cat_name, cat_type, cat_desc))
        conn.commit()
        print("[+] Categories seeded.")
    
    # 4. Seed Realistic Demo Transactions if table is empty
    cursor.execute("SELECT COUNT(*) FROM accounts_transactions;")
    row = cursor.fetchone()
    tx_count = list(row.values())[0] if isinstance(row, dict) else row[0]
    
    if tx_count == 0:
        print("[*] Seeding demo accounting transactions...")
        today = datetime.datetime.now()
        
        demo_txns = [
            # Incomes
            ('TXN-1001', (today - datetime.timedelta(days=25)).strftime('%Y-%m-%d %H:%M:%S'), 'Income', 'Revenue', 'Sales Revenue', 145000.00, 'Bank Transfer', 'INV-2026-001', 'Monthly bulk retail sales revenue deposit'),
            ('TXN-1002', (today - datetime.timedelta(days=20)).strftime('%Y-%m-%d %H:%M:%S'), 'Income', 'Revenue', 'Service Revenue', 18500.00, 'UPI', 'SRV-8821', 'Annual technical service contract fee'),
            ('TXN-1003', (today - datetime.timedelta(days=15)).strftime('%Y-%m-%d %H:%M:%S'), 'Income', 'Revenue', 'Sales Revenue', 162000.00, 'Bank Transfer', 'INV-2026-002', 'Corporate wholesale order payment'),
            ('TXN-1004', (today - datetime.timedelta(days=10)).strftime('%Y-%m-%d %H:%M:%S'), 'Income', 'Revenue', 'Interest Earned', 4200.00, 'Bank Transfer', 'INT-Q1-2026', 'Fixed deposit quarterly interest credit'),
            ('TXN-1005', (today - datetime.timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S'),  'Income', 'Revenue', 'Sales Revenue', 198000.00, 'Card', 'POS-REC-9912', 'Counter sales weekly aggregate settlement'),
            ('TXN-1006', (today - datetime.timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),  'Income', 'Revenue', 'Other Income', 7500.00, 'Cash', 'MISC-REC-44', 'Scrap and packing material recycling sale'),
            ('TXN-1007', (today - datetime.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),  'Income', 'Revenue', 'Sales Revenue', 84000.00, 'UPI', 'POS-REC-9988', 'Daily sales counter settlement'),
            
            # Direct Expenses (COGS)
            ('TXN-2001', (today - datetime.timedelta(days=24)).strftime('%Y-%m-%d %H:%M:%S'), 'Expense', 'Direct Expense', 'Raw Materials', 65000.00, 'Bank Transfer', 'PO-RAW-401', 'Bulk procurement of primary raw materials'),
            ('TXN-2002', (today - datetime.timedelta(days=22)).strftime('%Y-%m-%d %H:%M:%S'), 'Expense', 'Direct Expense', 'Direct Wages', 22000.00, 'Cash', 'WAGE-WK-1', 'Factory processing staff weekly wages'),
            ('TXN-2003', (today - datetime.timedelta(days=18)).strftime('%Y-%m-%d %H:%M:%S'), 'Expense', 'Direct Expense', 'Factory Rent', 25000.00, 'Bank Transfer', 'RENT-FAC-FEB', 'Monthly factory premises lease rent'),
            ('TXN-2004', (today - datetime.timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S'), 'Expense', 'Direct Expense', 'Factory Utilities', 14200.00, 'UPI', 'EB-FAC-7812', 'Factory industrial electricity and water charges'),
            ('TXN-2005', (today - datetime.timedelta(days=8)).strftime('%Y-%m-%d %H:%M:%S'),  'Expense', 'Direct Expense', 'Raw Material Freight', 8500.00, 'Cash', 'FRT-LOG-102', 'Freight and transport charges for raw material batch'),
            
            # Operating Expenses
            ('TXN-3001', (today - datetime.timedelta(days=21)).strftime('%Y-%m-%d %H:%M:%S'), 'Expense', 'Operating Expense', 'Marketing & Advertisements', 15000.00, 'UPI', 'AD-META-33', 'Digital social media ad campaign'),
            ('TXN-3002', (today - datetime.timedelta(days=19)).strftime('%Y-%m-%d %H:%M:%S'), 'Expense', 'Operating Expense', 'Shop Rent', 35000.00, 'Bank Transfer', 'RENT-SHOP-FEB', 'Main retail store monthly rent'),
            ('TXN-3003', (today - datetime.timedelta(days=16)).strftime('%Y-%m-%d %H:%M:%S'), 'Expense', 'Operating Expense', 'Employee Salary', 58000.00, 'Bank Transfer', 'PAYROLL-FEB-01', 'Monthly staff salary disbursements'),
            ('TXN-3004', (today - datetime.timedelta(days=12)).strftime('%Y-%m-%d %H:%M:%S'), 'Expense', 'Operating Expense', 'Office Electricity/Water', 6400.00, 'UPI', 'UTIL-OFF-09', 'Administrative office power and water bill'),
            ('TXN-3005', (today - datetime.timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),  'Expense', 'Operating Expense', 'Stationery', 2400.00, 'Cash', 'STAT-8812', 'Thermal paper rolls and invoice books'),
            ('TXN-3006', (today - datetime.timedelta(days=4)).strftime('%Y-%m-%d %H:%M:%S'),  'Expense', 'Operating Expense', 'Repair & Maintenance', 4800.00, 'UPI', 'MNT-EQ-55', 'POS thermal printer & UPS servicing'),
            ('TXN-3007', (today - datetime.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),  'Expense', 'Operating Expense', 'Petty Expenses', 1250.00, 'Cash', 'PETTY-104', 'Daily office refreshments and staff tea')
        ]
        
        for txn_no, t_date, t_type, acc_type, cat_name, amt, p_method, ref_no, desc in demo_txns:
            cursor.execute("SELECT id FROM accounts_categories WHERE name = %s AND type = %s;", (cat_name, acc_type))
            r = cursor.fetchone()
            cat_id = (r['id'] if isinstance(r, dict) else r[0]) if r else None
            
            cursor.execute("""
                INSERT INTO accounts_transactions
                    (txn_number, txn_date, txn_type, account_type, category_id, category_name, amount, payment_method, reference_no, description, created_by)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'admin');
            """, (txn_no, t_date, t_type, acc_type, cat_id, cat_name, amt, p_method, ref_no, desc))
            
        conn.commit()
        print("[+] Demo transactions seeded.")
        
    # 5. Seed Demo Receivables if table empty
    cursor.execute("SELECT COUNT(*) FROM accounts_receivables;")
    row = cursor.fetchone()
    rec_count = list(row.values())[0] if isinstance(row, dict) else row[0]
    
    if rec_count == 0:
        print("[*] Seeding demo Accounts Receivable...")
        today = datetime.date.today()
        demo_recs = [
            ('REC-2026-001', 'Sri Raman Traders', '9842100123', 'INV-REC-501', 45000.00, 15000.00, 30000.00, (today + datetime.timedelta(days=7)).strftime('%Y-%m-%d'), 'Partial', 'Credit sale batch #1, balance due next week'),
            ('REC-2026-002', 'Apex Supermarket Chain', '9712098455', 'INV-REC-502', 88000.00, 0.00, 88000.00, (today + datetime.timedelta(days=14)).strftime('%Y-%m-%d'), 'Pending', 'Payment pending management clearance'),
            ('REC-2026-003', 'Grand Heritage Hotel', '9443211099', 'INV-REC-498', 28500.00, 28500.00, 0.00, (today - datetime.timedelta(days=3)).strftime('%Y-%m-%d'), 'Paid', 'Full payment received via NEFT')
        ]
        for r_no, c_name, phone, inv_ref, tot, paid, rem, due, stat, notes in demo_recs:
            cursor.execute("""
                INSERT INTO accounts_receivables
                    (receivable_no, customer_name, contact_phone, invoice_ref, total_amount, paid_amount, remaining_balance, due_date, status, notes, created_by)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'admin');
            """, (r_no, c_name, phone, inv_ref, tot, paid, rem, due, stat, notes))
            rec_id = cursor.lastrowid
            if paid > 0:
                cursor.execute("""
                    INSERT INTO accounts_receivable_payments (receivable_id, payment_date, amount_paid, payment_method, reference_no, notes)
                    VALUES (%s, %s, %s, 'Bank Transfer', 'NEFT-883199', 'Initial installment');
                """, (rec_id, (datetime.datetime.now() - datetime.timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S'), paid))
        conn.commit()
        print("[+] Demo receivables seeded.")

    # 6. Seed Demo Payables if table empty
    cursor.execute("SELECT COUNT(*) FROM accounts_payables;")
    row = cursor.fetchone()
    pay_count = list(row.values())[0] if isinstance(row, dict) else row[0]
    
    if pay_count == 0:
        print("[*] Seeding demo Accounts Payable...")
        today = datetime.date.today()
        demo_pays = [
            ('PAY-2026-001', 'Venkateswara Packaging Ltd', '9894012345', 'SUP-INV-991', 32000.00, 10000.00, 22000.00, (today + datetime.timedelta(days=5)).strftime('%Y-%m-%d'), 'Partial', 'Corrugated boxes & custom printed bags supply'),
            ('PAY-2026-002', 'Nilgiri Spice Mills & Farms', '9488112233', 'SUP-INV-841', 54000.00, 0.00, 54000.00, (today + datetime.timedelta(days=12)).strftime('%Y-%m-%d'), 'Pending', 'Raw spice stock batch invoice'),
            ('PAY-2026-003', 'Southern Power Corp', '0422-2451122', 'EB-BILL-FEB-01', 12500.00, 12500.00, 0.00, (today - datetime.timedelta(days=2)).strftime('%Y-%m-%d'), 'Paid', 'Factory electricity bill paid')
        ]
        for p_no, s_name, phone, inv_ref, tot, paid, rem, due, stat, notes in demo_pays:
            cursor.execute("""
                INSERT INTO accounts_payables
                    (payable_no, supplier_name, contact_phone, invoice_ref, total_amount, paid_amount, remaining_balance, due_date, status, notes, created_by)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'admin');
            """, (p_no, s_name, phone, inv_ref, tot, paid, rem, due, stat, notes))
            pay_id = cursor.lastrowid
            if paid > 0:
                cursor.execute("""
                    INSERT INTO accounts_payable_payments (payable_id, payment_date, amount_paid, payment_method, reference_no, notes)
                    VALUES (%s, %s, %s, 'UPI', 'UPI-REF-77123', 'Part payment');
                """, (pay_id, (datetime.datetime.now() - datetime.timedelta(days=4)).strftime('%Y-%m-%d %H:%M:%S'), paid))
        conn.commit()
        print("[+] Demo payables seeded.")

    cursor.close()
    conn.close()
    print("[+] Accounts Database Initialization & Seeding Complete!")

if __name__ == '__main__':
    init_database()

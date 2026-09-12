import os
import sys
import datetime
import hashlib
from werkzeug.security import generate_password_hash
from config import Config
from backend.db import get_db_connection, get_db_mode


def _column_exists(cursor, table_name, column_name, mode):
    if mode == 'sqlite':
        cursor.execute(f"PRAGMA table_info({table_name});")
        cols = [r['name'] if isinstance(r, dict) else r[1] for r in cursor.fetchall()]
        return column_name in cols
    else:
        cursor.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = %s AND COLUMN_NAME = %s;
        """, (table_name, column_name))
        return cursor.fetchone() is not None


def apply_accounting_schema_migrations(cursor, mode):
    """
    Safely upgrades existing database tables with new columns needed for the
    unified core accounting foundation without losing any historical data.
    """
    print("[*] Checking & applying core accounting schema migrations...")

    # 1. accounts_chart enhancements
    chart_cols = [
        ("parent_id", "INT DEFAULT NULL"),
        ("normal_balance", "VARCHAR(10) DEFAULT 'Debit'"),
        ("is_group", "INT DEFAULT 0"),
        ("is_postable", "INT DEFAULT 1"),
        ("system_tag", "VARCHAR(50) DEFAULT NULL"),
        ("tax_classification", "VARCHAR(50) DEFAULT NULL"),
        ("updated_at", "DATETIME DEFAULT NULL"),
    ]
    for col_name, col_def in chart_cols:
        if not _column_exists(cursor, "accounts_chart", col_name, mode):
            try:
                cursor.execute(f"ALTER TABLE accounts_chart ADD COLUMN {col_name} {col_def};")
                print(f"  [+] Added accounts_chart.{col_name}")
            except Exception as e:
                print(f"  [-] Note on accounts_chart.{col_name}: {e}")

    # 2. journal_entries enhancements
    je_cols = [
        ("posting_date", "DATETIME DEFAULT NULL"),
        ("total_debit", "DECIMAL(15, 2) DEFAULT 0.00"),
        ("total_credit", "DECIMAL(15, 2) DEFAULT 0.00"),
        ("posted_by", "VARCHAR(50) DEFAULT NULL"),
        ("posted_at", "DATETIME DEFAULT NULL"),
        ("reversal_of_entry_id", "INT DEFAULT NULL"),
        ("reversal_reason", "TEXT DEFAULT NULL"),
        ("is_opening", "INT DEFAULT 0"),
    ]
    for col_name, col_def in je_cols:
        if not _column_exists(cursor, "journal_entries", col_name, mode):
            try:
                cursor.execute(f"ALTER TABLE journal_entries ADD COLUMN {col_name} {col_def};")
                print(f"  [+] Added journal_entries.{col_name}")
            except Exception as e:
                print(f"  [-] Note on journal_entries.{col_name}: {e}")

    # 3. journal_lines enhancements
    jl_cols = [
        ("description", "TEXT DEFAULT NULL"),
        ("party_type", "VARCHAR(20) DEFAULT NULL"),
        ("party_id", "VARCHAR(50) DEFAULT NULL"),
        ("party_name", "VARCHAR(150) DEFAULT NULL"),
        ("tax_code", "VARCHAR(30) DEFAULT NULL"),
        ("tax_rate", "DECIMAL(5, 2) DEFAULT 0.00"),
        ("source_info", "TEXT DEFAULT NULL"),
    ]
    for col_name, col_def in jl_cols:
        if not _column_exists(cursor, "journal_lines", col_name, mode):
            try:
                cursor.execute(f"ALTER TABLE journal_lines ADD COLUMN {col_name} {col_def};")
                print(f"  [+] Added journal_lines.{col_name}")
            except Exception as e:
                print(f"  [-] Note on journal_lines.{col_name}: {e}")

    # 4. accounts_receivables & accounts_payables opening balance flags
    if not _column_exists(cursor, "accounts_receivables", "is_opening", mode):
        try:
            cursor.execute("ALTER TABLE accounts_receivables ADD COLUMN is_opening INT DEFAULT 0;")
            print("  [+] Added accounts_receivables.is_opening")
        except Exception as e:
            print(f"  [-] Note on accounts_receivables.is_opening: {e}")

    if not _column_exists(cursor, "accounts_payables", "is_opening", mode):
        try:
            cursor.execute("ALTER TABLE accounts_payables ADD COLUMN is_opening INT DEFAULT 0;")
            print("  [+] Added accounts_payables.is_opening")
        except Exception as e:
            print(f"  [-] Note on accounts_payables.is_opening: {e}")

    # 5. accounts_fixed_assets disposal columns
    asset_cols = [
        ("disposal_date", "DATE DEFAULT NULL"),
        ("disposal_proceeds", "DECIMAL(15, 2) DEFAULT 0.00"),
        ("disposal_gain_loss", "DECIMAL(15, 2) DEFAULT 0.00"),
        ("disposal_journal_id", "INT DEFAULT NULL")
    ]
    for col_name, col_def in asset_cols:
        if not _column_exists(cursor, "accounts_fixed_assets", col_name, mode):
            try:
                cursor.execute(f"ALTER TABLE accounts_fixed_assets ADD COLUMN {col_name} {col_def};")
                print(f"  [+] Added accounts_fixed_assets.{col_name}")
            except Exception as e:
                print(f"  [-] Note on accounts_fixed_assets.{col_name}: {e}")

    # 6. accounting_sync_registry failure and retry tracking columns
    sync_cols = [
        ("error_info", "TEXT DEFAULT NULL"),
        ("retry_count", "INT DEFAULT 0"),
        ("last_attempt_at", "DATETIME DEFAULT NULL"),
        ("created_at", "DATETIME DEFAULT NULL")
    ]
    for col_name, col_def in sync_cols:
        if not _column_exists(cursor, "accounting_sync_registry", col_name, mode):
            try:
                cursor.execute(f"ALTER TABLE accounting_sync_registry ADD COLUMN {col_name} {col_def};")
                print(f"  [+] Added accounting_sync_registry.{col_name}")
            except Exception as e:
                print(f"  [-] Note on accounting_sync_registry.{col_name}: {e}")

    # 7. accounting_backups table for data safety and recovery
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounting_backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename VARCHAR(255) NOT NULL UNIQUE,
            backup_path VARCHAR(255) NOT NULL,
            backup_type VARCHAR(50) DEFAULT 'manual',
            file_size_bytes INTEGER NOT NULL,
            sha256_hash VARCHAR(64) NOT NULL,
            total_journals INTEGER DEFAULT 0,
            total_accounts INTEGER DEFAULT 0,
            status VARCHAR(30) DEFAULT 'VERIFIED',
            notes TEXT DEFAULT NULL,
            created_by VARCHAR(50) DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            verified_at DATETIME DEFAULT NULL
        );
    """)

    # 8. High-performance database integrity indexes
    indexes = [
        ("idx_je_source", "journal_entries", "(source_module, source_entity, source_id)"),
        ("idx_je_date", "journal_entries", "(entry_date)"),
        ("idx_je_status", "journal_entries", "(status)"),
        ("idx_jl_entry", "journal_lines", "(entry_id)"),
        ("idx_jl_account", "journal_lines", "(account_id)"),
        ("idx_audit_entity", "accounting_audit_trail", "(entity_type, entity_id)"),
        ("idx_audit_created", "accounting_audit_trail", "(created_at)"),
        ("idx_sync_status", "accounting_sync_registry", "(status)")
    ]
    for idx_name, tbl_name, cols in indexes:
        try:
            cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {tbl_name} {cols};")
        except Exception as e:
            print(f"  [-] Note creating index {idx_name}: {e}")



def init_database(force_reseed=False):
    mode = get_db_mode()
    print(f"[*] Initializing Modern Accounts Database (Engine: {mode.upper()})...")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # 1. Create Core Tables
    tables = [
        # Users
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
        # Chart of Accounts
        """
        CREATE TABLE IF NOT EXISTS accounts_chart (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code VARCHAR(30) UNIQUE NOT NULL,
            name VARCHAR(100) NOT NULL,
            major_type VARCHAR(50) NOT NULL,
            sub_type VARCHAR(100) NOT NULL,
            description TEXT,
            is_active INT DEFAULT 1,
            parent_id INT DEFAULT NULL,
            normal_balance VARCHAR(10) DEFAULT 'Debit',
            is_group INT DEFAULT 0,
            is_postable INT DEFAULT 1,
            system_tag VARCHAR(50) DEFAULT NULL,
            tax_classification VARCHAR(50) DEFAULT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # Double-Entry Master Journal
        """
        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_number VARCHAR(50) UNIQUE NOT NULL,
            entry_date DATETIME NOT NULL,
            posting_date DATETIME DEFAULT NULL,
            source_module VARCHAR(30) NOT NULL,
            source_entity VARCHAR(50),
            source_id VARCHAR(100),
            reference_no VARCHAR(100),
            narration TEXT NOT NULL,
            total_debit DECIMAL(15, 2) DEFAULT 0.00,
            total_credit DECIMAL(15, 2) DEFAULT 0.00,
            status VARCHAR(20) DEFAULT 'POSTED',
            created_by VARCHAR(50) DEFAULT 'system',
            posted_by VARCHAR(50) DEFAULT NULL,
            posted_at DATETIME DEFAULT NULL,
            reversal_of_entry_id INT DEFAULT NULL,
            reversal_reason TEXT DEFAULT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # Journal Lines (Debit / Credit)
        """
        CREATE TABLE IF NOT EXISTS journal_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INT NOT NULL,
            account_id INT NOT NULL,
            debit DECIMAL(15, 2) DEFAULT 0.00,
            credit DECIMAL(15, 2) DEFAULT 0.00,
            description TEXT,
            party_type VARCHAR(20) DEFAULT NULL,
            party_id VARCHAR(50) DEFAULT NULL,
            party_name VARCHAR(150) DEFAULT NULL,
            tax_code VARCHAR(30) DEFAULT NULL,
            tax_rate DECIMAL(5, 2) DEFAULT 0.00,
            source_info TEXT DEFAULT NULL,
            FOREIGN KEY (entry_id) REFERENCES journal_entries (id) ON DELETE CASCADE,
            FOREIGN KEY (account_id) REFERENCES accounts_chart (id)
        );
        """,
        # Central Account Mappings Table
        """
        CREATE TABLE IF NOT EXISTS accounts_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mapping_key VARCHAR(50) UNIQUE NOT NULL,
            account_id INT NOT NULL,
            account_code VARCHAR(30) NOT NULL,
            description VARCHAR(255),
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts_chart (id)
        );
        """,
        # Synchronization Registry (Deduplication & Idempotency)
        """
        CREATE TABLE IF NOT EXISTS accounting_sync_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_module VARCHAR(30) NOT NULL,
            source_entity VARCHAR(50) NOT NULL,
            source_id VARCHAR(100) NOT NULL,
            source_hash VARCHAR(64) NOT NULL,
            journal_entry_id INT,
            synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(20) DEFAULT 'SYNCED',
            UNIQUE(source_module, source_entity, source_id)
        );
        """,
        # Fixed Assets
        """
        CREATE TABLE IF NOT EXISTS accounts_fixed_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_code VARCHAR(30) UNIQUE NOT NULL,
            asset_name VARCHAR(150) NOT NULL,
            category VARCHAR(50) NOT NULL,
            purchase_date DATE NOT NULL,
            purchase_value DECIMAL(15, 2) NOT NULL,
            current_value DECIMAL(15, 2) NOT NULL,
            useful_life_years INT DEFAULT 5,
            depreciation_rate DECIMAL(5, 2) DEFAULT 10.00,
            accumulated_depreciation DECIMAL(15, 2) DEFAULT 0.00,
            payment_method VARCHAR(50) DEFAULT 'Bank Transfer',
            supplier VARCHAR(100),
            status VARCHAR(20) DEFAULT 'Active',
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # Liabilities and Loans
        """
        CREATE TABLE IF NOT EXISTS accounts_liabilities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            liability_code VARCHAR(30) UNIQUE NOT NULL,
            title VARCHAR(150) NOT NULL,
            liability_type VARCHAR(50) NOT NULL,
            principal_amount DECIMAL(15, 2) NOT NULL,
            interest_rate DECIMAL(5, 2) DEFAULT 0.00,
            tenure_months INT DEFAULT 0,
            outstanding_balance DECIMAL(15, 2) NOT NULL,
            lender VARCHAR(150),
            start_date DATE NOT NULL,
            status VARCHAR(20) DEFAULT 'Active',
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # Accounts Receivable (Customer Invoices)
        """
        CREATE TABLE IF NOT EXISTS accounts_receivables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receivable_no VARCHAR(50) UNIQUE NOT NULL,
            invoice_ref VARCHAR(100) NOT NULL,
            customer_name VARCHAR(100) NOT NULL,
            contact_phone VARCHAR(20),
            invoice_date DATETIME NOT NULL,
            total_amount DECIMAL(12, 2) NOT NULL,
            paid_amount DECIMAL(12, 2) DEFAULT 0.00,
            remaining_balance DECIMAL(12, 2) NOT NULL,
            due_date DATE,
            status VARCHAR(20) DEFAULT 'Pending',
            source_bill_id VARCHAR(50),
            notes TEXT,
            created_by VARCHAR(50) DEFAULT 'system',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # Receivable Payments
        """
        CREATE TABLE IF NOT EXISTS accounts_receivable_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receivable_id INT NOT NULL,
            payment_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            amount_paid DECIMAL(12, 2) NOT NULL,
            payment_method VARCHAR(30) DEFAULT 'Cash',
            reference_no VARCHAR(100),
            notes TEXT,
            created_by VARCHAR(50) DEFAULT 'admin',
            FOREIGN KEY (receivable_id) REFERENCES accounts_receivables (id) ON DELETE CASCADE
        );
        """,
        # Accounts Payable (Supplier Invoices)
        """
        CREATE TABLE IF NOT EXISTS accounts_payables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payable_no VARCHAR(50) UNIQUE NOT NULL,
            invoice_ref VARCHAR(100) NOT NULL,
            supplier_name VARCHAR(100) NOT NULL,
            contact_phone VARCHAR(20),
            invoice_date DATETIME NOT NULL,
            total_amount DECIMAL(12, 2) NOT NULL,
            paid_amount DECIMAL(12, 2) DEFAULT 0.00,
            remaining_balance DECIMAL(12, 2) NOT NULL,
            due_date DATE,
            status VARCHAR(20) DEFAULT 'Pending',
            source_invoice_id VARCHAR(50),
            notes TEXT,
            created_by VARCHAR(50) DEFAULT 'system',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # Payable Payments
        """
        CREATE TABLE IF NOT EXISTS accounts_payable_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payable_id INT NOT NULL,
            payment_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            amount_paid DECIMAL(12, 2) NOT NULL,
            payment_method VARCHAR(30) DEFAULT 'Bank Transfer',
            reference_no VARCHAR(100),
            notes TEXT,
            created_by VARCHAR(50) DEFAULT 'admin',
            FOREIGN KEY (payable_id) REFERENCES accounts_payables (id) ON DELETE CASCADE
        );
        """,
        # Financial Years
        """
        CREATE TABLE IF NOT EXISTS financial_years (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(50) UNIQUE NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            status VARCHAR(20) DEFAULT 'OPEN',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            closed_at DATETIME DEFAULT NULL,
            closed_by VARCHAR(100) DEFAULT NULL,
            locked_at DATETIME DEFAULT NULL,
            locked_by VARCHAR(100) DEFAULT NULL,
            lock_reason TEXT DEFAULT NULL
        );
        """,
        # Accounting Periods
        """
        CREATE TABLE IF NOT EXISTS accounting_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            financial_year_id INT NOT NULL,
            period_name VARCHAR(50) NOT NULL,
            period_number INT NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            status VARCHAR(20) DEFAULT 'OPEN',
            closed_at DATETIME DEFAULT NULL,
            closed_by VARCHAR(100) DEFAULT NULL,
            locked_at DATETIME DEFAULT NULL,
            locked_by VARCHAR(100) DEFAULT NULL,
            lock_reason TEXT DEFAULT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (financial_year_id) REFERENCES financial_years (id) ON DELETE CASCADE,
            UNIQUE(financial_year_id, period_number)
        );
        """,
        # Reconciliations
        """
        CREATE TABLE IF NOT EXISTS reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reconciliation_type VARCHAR(30) NOT NULL,
            account_id INT DEFAULT NULL,
            period_id INT DEFAULT NULL,
            statement_date DATE NOT NULL,
            ledger_balance DECIMAL(15, 2) NOT NULL,
            statement_balance DECIMAL(15, 2) NOT NULL,
            difference DECIMAL(15, 2) NOT NULL,
            status VARCHAR(30) DEFAULT 'DRAFT',
            notes TEXT DEFAULT NULL,
            performed_by VARCHAR(100) DEFAULT 'admin',
            performed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            adjustment_journal_id INT DEFAULT NULL,
            FOREIGN KEY (account_id) REFERENCES accounts_chart (id)
        );
        """,
        # Reconciliation Items
        """
        CREATE TABLE IF NOT EXISTS reconciliation_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reconciliation_id INT NOT NULL,
            item_date DATE NOT NULL,
            reference_no VARCHAR(100) DEFAULT NULL,
            description VARCHAR(255) NOT NULL,
            amount DECIMAL(15, 2) NOT NULL,
            item_type VARCHAR(50) NOT NULL,
            match_status VARCHAR(20) DEFAULT 'UNMATCHED',
            journal_entry_id INT DEFAULT NULL,
            notes TEXT DEFAULT NULL,
            FOREIGN KEY (reconciliation_id) REFERENCES reconciliations (id) ON DELETE CASCADE
        );
        """,
        # Accounting Audit Trail
        """
        CREATE TABLE IF NOT EXISTS accounting_audit_trail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action VARCHAR(50) NOT NULL,
            entity_type VARCHAR(50) NOT NULL,
            entity_id VARCHAR(100) DEFAULT NULL,
            old_value TEXT DEFAULT NULL,
            new_value TEXT DEFAULT NULL,
            reason TEXT DEFAULT NULL,
            user VARCHAR(100) NOT NULL,
            ip_address VARCHAR(50) DEFAULT '127.0.0.1',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # Accounts Credit Notes (Customer Returns / Allowances)
        """
        CREATE TABLE IF NOT EXISTS accounts_credit_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credit_note_no VARCHAR(50) UNIQUE NOT NULL,
            receivable_id INT DEFAULT NULL,
            customer_name VARCHAR(100) NOT NULL,
            note_date DATE NOT NULL,
            amount DECIMAL(15, 2) NOT NULL,
            tax_amount DECIMAL(15, 2) DEFAULT 0.00,
            reason TEXT DEFAULT NULL,
            journal_entry_id INT DEFAULT NULL,
            created_by VARCHAR(50) DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (receivable_id) REFERENCES accounts_receivables (id) ON DELETE SET NULL,
            FOREIGN KEY (journal_entry_id) REFERENCES journal_entries (id) ON DELETE SET NULL
        );
        """,
        # Accounts Debit Notes (Supplier Returns / Discounts)
        """
        CREATE TABLE IF NOT EXISTS accounts_debit_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            debit_note_no VARCHAR(50) UNIQUE NOT NULL,
            payable_id INT DEFAULT NULL,
            supplier_name VARCHAR(100) NOT NULL,
            note_date DATE NOT NULL,
            amount DECIMAL(15, 2) NOT NULL,
            tax_amount DECIMAL(15, 2) DEFAULT 0.00,
            reason TEXT DEFAULT NULL,
            journal_entry_id INT DEFAULT NULL,
            created_by VARCHAR(50) DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (payable_id) REFERENCES accounts_payables (id) ON DELETE SET NULL,
            FOREIGN KEY (journal_entry_id) REFERENCES journal_entries (id) ON DELETE SET NULL
        );
        """,
        # Accounts Liability Payments (Loan Installment Principal & Interest Breakdown)
        """
        CREATE TABLE IF NOT EXISTS accounts_liability_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            liability_id INT NOT NULL,
            payment_date DATE NOT NULL,
            principal_amount DECIMAL(15, 2) NOT NULL,
            interest_amount DECIMAL(15, 2) DEFAULT 0.00,
            total_amount DECIMAL(15, 2) NOT NULL,
            payment_method VARCHAR(50) DEFAULT 'Bank Transfer',
            reference_no VARCHAR(100) DEFAULT NULL,
            journal_entry_id INT DEFAULT NULL,
            notes TEXT DEFAULT NULL,
            created_by VARCHAR(50) DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (liability_id) REFERENCES accounts_liabilities (id) ON DELETE CASCADE,
            FOREIGN KEY (journal_entry_id) REFERENCES journal_entries (id) ON DELETE SET NULL
        );
        """,
        # Fixed Asset Depreciation Log (Deduplication & Provenance)
        """
        CREATE TABLE IF NOT EXISTS accounts_asset_depreciation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INT NOT NULL,
            depreciation_date DATE NOT NULL,
            financial_year_id INT DEFAULT NULL,
            period_id INT DEFAULT NULL,
            depreciation_amount DECIMAL(15, 2) NOT NULL,
            book_value_before DECIMAL(15, 2) NOT NULL,
            book_value_after DECIMAL(15, 2) NOT NULL,
            journal_entry_id INT DEFAULT NULL,
            created_by VARCHAR(50) DEFAULT 'admin',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (asset_id) REFERENCES accounts_fixed_assets (id) ON DELETE CASCADE,
            FOREIGN KEY (journal_entry_id) REFERENCES journal_entries (id) ON DELETE SET NULL
        );
        """
    ]


    for t_sql in tables:
        if mode == 'mysql':
            t_sql = t_sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'INT AUTO_INCREMENT PRIMARY KEY')
            t_sql = t_sql.replace('DATETIME DEFAULT CURRENT_TIMESTAMP', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        cursor.execute(t_sql)

    conn.commit()
    print("[+] All database tables created / verified.")

    # Apply safe column migrations for existing tables
    apply_accounting_schema_migrations(cursor, mode)
    conn.commit()

    # 2. Seed Default Users (accounts / 1234, accountant / 1234, admin / admin)
    user_seeds = [
        ('accounts', generate_password_hash('1234'), 'Accounts Manager', 'Accounts'),
        ('accountant', generate_password_hash('1234'), 'Chief Accountant', 'accountant'),
        ('admin', generate_password_hash('admin'), 'System Administrator', 'admin'),
    ]
    for u_name, u_hash, u_full, u_role in user_seeds:
        cursor.execute("SELECT COUNT(*) FROM accounts_users WHERE username = %s;", (u_name,))
        row = cursor.fetchone()
        cnt = list(row.values())[0] if isinstance(row, dict) else row[0]
        if cnt == 0:
            cursor.execute("""
                INSERT INTO accounts_users (username, password_hash, full_name, role)
                VALUES (%s, %s, %s, %s);
            """, (u_name, u_hash, u_full, u_role))
    conn.commit()
    print("[+] Users verified.")

    # 3. Seed Comprehensive Hierarchical Chart of Accounts (COA)
    # Format: (code, name, major_type, sub_type, description, is_group, is_postable, normal_balance, parent_code, system_tag, tax_class)
    coa_tree = [
        # --- 1000 ASSETS (Group) ---
        ('1000', 'ASSETS', 'Asset', 'Asset Header', 'Main Asset Group', 1, 0, 'Debit', None, None, None),

        # 1100 Fixed Assets (Group)
        ('1100', 'Fixed Assets', 'Asset', 'Fixed Assets Group', 'Tangible and long-term capital assets', 1, 0, 'Debit', '1000', None, None),
        ('1110', 'Land & Building', 'Asset', 'Fixed Assets', 'Real estate land and office buildings', 0, 1, 'Debit', '1100', None, None),
        ('1120', 'Building & Premises', 'Asset', 'Fixed Assets', 'Commercial office & factory buildings', 0, 1, 'Debit', '1100', None, None),
        ('1130', 'Plant & Machinery', 'Asset', 'Fixed Assets', 'Industrial production and packaging machinery', 0, 1, 'Debit', '1100', None, None),
        ('1140', 'Equipment & Computers', 'Asset', 'Fixed Assets', 'POS terminals, printers, electronics', 0, 1, 'Debit', '1100', None, None),
        ('1145', 'Furniture & Fixtures', 'Asset', 'Fixed Assets', 'Desks, chairs, racks, display units', 0, 1, 'Debit', '1100', None, None),
        ('1150', 'Motor Vehicles', 'Asset', 'Fixed Assets', 'Delivery trucks and company vehicles', 0, 1, 'Debit', '1100', None, None),
        ('1155', 'Other Fixed Assets', 'Asset', 'Fixed Assets', 'Sundry capital assets', 0, 1, 'Debit', '1100', None, None),
        ('1160', 'Accumulated Depreciation', 'Asset', 'Fixed Assets', 'Contra-asset account for depreciation write-down', 0, 1, 'Credit', '1100', 'accumulated_depreciation', None),

        # 1200 Current Assets (Group)
        ('1200', 'Current Assets', 'Asset', 'Current Assets Group', 'Liquid and short-term operating assets', 1, 0, 'Debit', '1000', None, None),
        ('1010', 'Cash on Hand', 'Asset', 'Cash', 'Liquid cash in registers and counter petty cash', 0, 1, 'Debit', '1200', 'cash_default', None),
        ('1020', 'Bank Account', 'Asset', 'Bank', 'Main commercial business current account', 0, 1, 'Debit', '1200', 'bank_default', None),
        ('1030', 'UPI & Digital Clearing', 'Asset', 'Bank', 'Digital payments pending bank settlement', 0, 1, 'Debit', '1200', 'upi_default', None),
        ('1040', 'Accounts Receivable', 'Asset', 'Accounts Receivable', 'Customer credit balances due from sales', 0, 1, 'Debit', '1200', 'accounts_receivable', None),
        ('1050', 'Inventory Stock (Raw Materials)', 'Asset', 'Inventory', 'Current valuation of raw materials and ingredients', 0, 1, 'Debit', '1200', 'inventory_raw', None),
        ('1060', 'Inventory Stock (Finished Goods)', 'Asset', 'Inventory', 'Current valuation of merchandise for sale', 0, 1, 'Debit', '1200', 'inventory_finished', None),
        ('1070', 'Supplier Advances', 'Asset', 'Advances', 'Advance payments made to vendors', 0, 1, 'Debit', '1200', None, None),
        ('1080', 'Other Current Assets', 'Asset', 'Other Current Assets', 'Sundry short-term current assets', 0, 1, 'Debit', '1200', None, None),

        # 1300 Other Assets (Group)
        ('1300', 'Other Assets', 'Asset', 'Other Assets Group', 'Non-current deposits and intangibles', 1, 0, 'Debit', '1000', None, None),
        ('1210', 'Security Deposits', 'Asset', 'Other Assets', 'Electricity, building, and lease deposits', 0, 1, 'Debit', '1300', None, None),
        ('1220', 'Prepaid Expenses', 'Asset', 'Other Assets', 'Prepaid insurance premiums and prepaid rent', 0, 1, 'Debit', '1300', None, None),
        ('1230', 'Intangible Assets', 'Asset', 'Other Assets', 'Software licenses and patents', 0, 1, 'Debit', '1300', None, None),

        # --- 2000 LIABILITIES (Group) ---
        ('2000', 'LIABILITIES', 'Liability', 'Liability Header', 'Main Liability Group', 1, 0, 'Credit', None, None, None),

        # 2100 Current Liabilities (Group)
        ('2100', 'Current Liabilities', 'Liability', 'Current Liabilities Group', 'Short-term trade and statutory obligations', 1, 0, 'Credit', '2000', None, None),
        ('2010', 'Accounts Payable', 'Liability', 'Accounts Payable', 'Trade balances owed to suppliers and vendors', 0, 1, 'Credit', '2100', 'accounts_payable', None),
        ('2020', 'Payroll / Salary Payable', 'Liability', 'Salary Payable', 'Staff salaries accrued but not disbursed', 0, 1, 'Credit', '2100', None, None),
        ('2025', 'Outstanding Expenses', 'Liability', 'Other Liabilities', 'Accrued bills and expenses payable', 0, 1, 'Credit', '2100', None, None),
        ('2030', 'GST Output Payable', 'Liability', 'GST Payable', 'Statutory GST collected on customer sales', 0, 1, 'Credit', '2100', 'output_gst', 'output_gst'),
        ('2040', 'GST Input Credit', 'Asset', 'Tax', 'Eligible input GST credit from purchases (Tax Asset)', 0, 1, 'Debit', '2100', 'input_gst', 'input_gst'),
        ('2050', 'TDS Payable', 'Liability', 'TDS Payable', 'Tax Deducted at Source payable to government', 0, 1, 'Credit', '2100', 'tds_payable', 'tds_payable'),
        ('2055', 'TDS Receivable', 'Asset', 'Tax', 'TDS deducted by clients receivable (Tax Asset)', 0, 1, 'Debit', '2100', 'tds_receivable', 'tds_receivable'),
        ('2060', 'Customer Advances', 'Liability', 'Customer Advances', 'Advances collected from customers for future orders', 0, 1, 'Credit', '2100', None, None),
        ('2065', 'Provisions for Tax & Expenses', 'Liability', 'Other Liabilities', 'Provisions for income tax and liabilities', 0, 1, 'Credit', '2100', None, None),

        # 2200 Long-Term Liabilities (Group)
        ('2200', 'Long-Term Liabilities', 'Liability', 'Long-Term Liabilities Group', 'Commercial term debt and loans', 1, 0, 'Credit', '2000', None, None),
        ('2110', 'Bank Loan', 'Liability', 'Bank Loan', 'Secured commercial bank borrowings', 0, 1, 'Credit', '2200', None, None),
        ('2120', 'Other Long-Term Loans', 'Liability', 'Other Loans', 'Unsecured business loans and notes', 0, 1, 'Credit', '2200', None, None),

        # --- 3000 EQUITY / CAPITAL (Group) ---
        ('3000', 'EQUITY / CAPITAL', 'Equity', 'Equity Header', 'Owners equity and retained surplus', 1, 0, 'Credit', None, None, None),
        ('3010', 'Owner Capital', 'Equity', 'Capital', 'Funds contributed into the business by owner', 0, 1, 'Credit', '3000', 'owner_capital', None),
        ('3015', 'Additional Capital', 'Equity', 'Capital', 'Subsequent capital infusions', 0, 1, 'Credit', '3000', None, None),
        ('3020', 'Owner Drawings', 'Equity', 'Drawings', 'Funds withdrawn by owner for personal use (Contra-Equity)', 0, 1, 'Debit', '3000', 'owner_drawings', None),
        ('3030', 'Retained Earnings', 'Equity', 'Retained Earnings', 'Accumulated prior years undistributed net profit', 0, 1, 'Credit', '3000', 'retained_earnings', None),
        ('3040', 'Current Year Profit / Loss', 'Equity', 'Profit & Loss', 'Current operating year net surplus/deficit', 0, 1, 'Credit', '3000', 'current_profit', None),

        # --- 4000 INCOME / REVENUE (Group) ---
        ('4000', 'INCOME / REVENUE', 'Revenue', 'Revenue Header', 'Operating and non-operating revenue', 1, 0, 'Credit', None, None, None),
        ('4010', 'Sales Revenue', 'Revenue', 'Sales', 'Core operating revenue from billing', 0, 1, 'Credit', '4000', 'sales_revenue', None),
        ('4015', 'Sales Returns & Allowances', 'Revenue', 'Sales Returns', 'Customer refunds and returns (Contra-Revenue)', 0, 1, 'Debit', '4000', 'sales_returns', None),
        ('4020', 'Interest Income', 'Revenue', 'Interest Income', 'Interest earned on deposits and bank accounts', 0, 1, 'Credit', '4000', None, None),
        ('4030', 'Other Operating Income', 'Revenue', 'Other Income', 'Discounts received, delivery charges billed', 0, 1, 'Credit', '4000', None, None),
        ('4040', 'Other Miscellaneous Income', 'Revenue', 'Other Income', 'Scrap sales and sundry receipts', 0, 1, 'Credit', '4000', None, None),

        # --- 5000 DIRECT EXPENSES / COGS (Group) ---
        ('5000', 'DIRECT EXPENSES / COGS', 'Direct Expense', 'COGS Header', 'Cost of goods sold and manufacturing costs', 1, 0, 'Debit', None, None, None),
        ('5010', 'Raw Materials Consumption', 'Direct Expense', 'Raw Materials', 'Cost of raw materials consumed in sales (COGS)', 0, 1, 'Debit', '5000', 'cogs', None),
        ('5020', 'Direct Production Wages', 'Direct Expense', 'Direct Wages', 'Wages paid to factory/production workers', 0, 1, 'Debit', '5000', None, None),
        ('5030', 'Factory Building Rent', 'Direct Expense', 'Factory Rent', 'Rent of factory and warehouse premises', 0, 1, 'Debit', '5000', None, None),
        ('5040', 'Factory Utilities', 'Direct Expense', 'Factory Utilities', 'Electricity, water, fuel for manufacturing', 0, 1, 'Debit', '5000', None, None),
        ('5050', 'Freight & Inward Charges', 'Direct Expense', 'Raw Material Freight', 'Freight and transport on inward goods', 0, 1, 'Debit', '5000', None, None),
        ('5060', 'Other Direct Production Costs', 'Direct Expense', 'Other Direct Expenses', 'Direct packaging and handling costs', 0, 1, 'Debit', '5000', None, None),

        # --- 6000 OPERATING EXPENSES (Group) ---
        ('6000', 'OPERATING EXPENSES', 'Operating Expense', 'Operating Expense Header', 'General administrative & selling overheads', 1, 0, 'Debit', None, None, None),
        ('6010', 'Shop Expense', 'Operating Expense', 'Shop Expense', 'Daily retail shop expenses recorded from Sales counter', 0, 1, 'Debit', '6000', None, None),
        ('6020', 'Marketing & Advertising', 'Operating Expense', 'Marketing', 'Promotions, digital ads, banners, publicity', 0, 1, 'Debit', '6000', None, None),
        ('6030', 'Shop Rent', 'Operating Expense', 'Shop Rent', 'Retail store monthly lease rent', 0, 1, 'Debit', '6000', None, None),
        ('6040', 'Office Rent', 'Operating Expense', 'Office Rent', 'Administrative office rent', 0, 1, 'Debit', '6000', None, None),
        ('6050', 'Stationery & Printing', 'Operating Expense', 'Stationery', 'Thermal rolls, invoices, office paperwork', 0, 1, 'Debit', '6000', None, None),
        ('6060', 'Postage & Courier', 'Operating Expense', 'Courier', 'Postal and parcel dispatch expenses', 0, 1, 'Debit', '6000', None, None),
        ('6070', 'Delivery & Logistics', 'Operating Expense', 'Delivery', 'Customer delivery, shipping, dispatch costs', 0, 1, 'Debit', '6000', None, None),
        ('6080', 'Insurance Expenses', 'Operating Expense', 'Insurance', 'Business premises, stock, and vehicle insurance', 0, 1, 'Debit', '6000', None, None),
        ('6090', 'Depreciation Expense', 'Operating Expense', 'Depreciation', 'Depreciation write-off on fixed assets', 0, 1, 'Debit', '6000', 'depreciation_expense', None),
        ('6100', 'Audit & Legal Fees', 'Operating Expense', 'Audit Fees', 'Chartered accountant, legal, and compliance fees', 0, 1, 'Debit', '6000', None, None),
        ('6110', 'Office Electricity & Water', 'Operating Expense', 'Office Utilities', 'Administrative office power and water utilities', 0, 1, 'Debit', '6000', None, None),
        ('6120', 'Repair & Maintenance', 'Operating Expense', 'Repairs', 'Servicing of equipment, printers, maintenance', 0, 1, 'Debit', '6000', None, None),
        ('6130', 'Travelling Expense', 'Operating Expense', 'Travelling', 'Staff travel, petrol, and conveyance expenses', 0, 1, 'Debit', '6000', None, None),
        ('6140', 'Employee Salary', 'Operating Expense', 'Salary', 'Regular administrative and sales staff payroll', 0, 1, 'Debit', '6000', None, None),
        ('6150', 'Administration Expenses', 'Operating Expense', 'Administration', 'General administrative operating costs', 0, 1, 'Debit', '6000', None, None),
        ('6160', 'Petty Expenses', 'Operating Expense', 'Petty Expenses', 'Minor incidental daily office cash expenses', 0, 1, 'Debit', '6000', None, None),
        ('6170', 'Miscellaneous Expenses', 'Operating Expense', 'Miscellaneous', 'General sundry operating expenses', 0, 1, 'Debit', '6000', None, None),

        # --- 7000 FINANCIAL COSTS (Group) ---
        ('7000', 'FINANCIAL COSTS', 'Financial Cost', 'Financial Cost Header', 'Bank interest and processing charges', 1, 0, 'Debit', None, None, None),
        ('7010', 'Bank Charges & Fees', 'Financial Cost', 'Bank Charges', 'Bank account maintenance, cheque bounce, wire fees', 0, 1, 'Debit', '7000', None, None),
        ('7020', 'Loan Interest Expense', 'Financial Cost', 'Interest', 'Commercial loan interest charges', 0, 1, 'Debit', '7000', None, None),
        ('7030', 'Other Financial Charges', 'Financial Cost', 'Other Financial Charges', 'Card swipe transaction fees, processing charges', 0, 1, 'Debit', '7000', None, None),
    ]

    print("[*] Upserting Hierarchical Chart of Accounts...")
    # First pass: Upsert all accounts with code, name, major_type, sub_type, description, is_group, is_postable, normal_balance, system_tag, tax_classification
    for item in coa_tree:
        code, name, major_type, sub_type, desc, is_grp, is_post, norm_bal, parent_code, sys_tag, tax_class = item
        
        cursor.execute("SELECT id FROM accounts_chart WHERE code = %s;", (code,))
        existing = cursor.fetchone()
        if existing:
            acc_id = existing["id"] if isinstance(existing, dict) else existing[0]
            cursor.execute("""
                UPDATE accounts_chart
                SET name = %s, major_type = %s, sub_type = %s, description = %s,
                    is_group = %s, is_postable = %s, normal_balance = %s,
                    system_tag = %s, tax_classification = %s, is_active = 1
                WHERE id = %s;
            """, (name, major_type, sub_type, desc, is_grp, is_post, norm_bal, sys_tag, tax_class, acc_id))
        else:
            cursor.execute("""
                INSERT INTO accounts_chart
                    (code, name, major_type, sub_type, description, is_group, is_postable, normal_balance, system_tag, tax_classification, is_active)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1);
            """, (code, name, major_type, sub_type, desc, is_grp, is_post, norm_bal, sys_tag, tax_class))

    conn.commit()

    # Second pass: Link parent_id based on parent_code
    code_to_id = {}
    cursor.execute("SELECT id, code FROM accounts_chart;")
    for r in cursor.fetchall():
        c = r["code"] if isinstance(r, dict) else r[1]
        i = r["id"] if isinstance(r, dict) else r[0]
        code_to_id[c] = i

    for item in coa_tree:
        code, _, _, _, _, _, _, _, parent_code, _, _ = item
        if parent_code and parent_code in code_to_id:
            parent_id = code_to_id[parent_code]
            cursor.execute("UPDATE accounts_chart SET parent_id = %s WHERE code = %s;", (parent_id, code))
        elif not parent_code:
            cursor.execute("UPDATE accounts_chart SET parent_id = NULL WHERE code = %s;", (code,))

    conn.commit()
    print(f"[+] {len(coa_tree)} Hierarchical Chart of Accounts verified and linked.")

    # 4. Seed Central Account Mappings
    default_mappings = [
        ("sales_revenue", "4010", "Default revenue account for operating sales"),
        ("sales_returns", "4015", "Sales returns and allowances contra-revenue account"),
        ("accounts_receivable", "1040", "Customer credit receivable control account"),
        ("accounts_payable", "2010", "Supplier trade payable control account"),
        ("cogs", "5010", "Cost of goods sold / Raw material consumption"),
        ("inventory_raw", "1050", "Raw material inventory asset account"),
        ("inventory_finished", "1060", "Finished goods inventory asset account"),
        ("input_gst", "2040", "Input GST tax credit receivable account"),
        ("output_gst", "2030", "Output GST tax liability payable account"),
        ("tds_payable", "2050", "TDS deduction liability payable account"),
        ("tds_receivable", "2055", "TDS deducted by customers receivable asset account"),
        ("cash_default", "1010", "Default cash on hand account"),
        ("bank_default", "1020", "Default primary business bank account"),
        ("upi_default", "1030", "Default digital UPI clearing account"),
        ("owner_capital", "3010", "Owner capital equity account"),
        ("owner_drawings", "3020", "Owner personal drawings contra-equity account"),
        ("retained_earnings", "3030", "Prior years accumulated retained earnings"),
        ("current_profit", "3040", "Current period profit / loss equity line"),
        ("depreciation_expense", "6090", "Operating depreciation write-off expense"),
        ("accumulated_depreciation", "1160", "Accumulated depreciation contra-asset account"),
    ]

    print("[*] Verifying Central Account Mappings...")
    for map_key, acc_code, map_desc in default_mappings:
        if acc_code in code_to_id:
            acc_id = code_to_id[acc_code]
            cursor.execute("SELECT id FROM accounts_mappings WHERE mapping_key = %s;", (map_key,))
            row = cursor.fetchone()
            if row:
                cursor.execute("""
                    UPDATE accounts_mappings 
                    SET account_id = %s, account_code = %s, description = %s
                    WHERE mapping_key = %s;
                """, (acc_id, acc_code, map_desc, map_key))
            else:
                cursor.execute("""
                    INSERT INTO accounts_mappings (mapping_key, account_id, account_code, description)
                    VALUES (%s, %s, %s, %s);
                """, (map_key, acc_id, acc_code, map_desc))

    conn.commit()
    print(f"[+] {len(default_mappings)} Central Account Mappings initialized.")

    # 5. Seed Financial Years and Monthly Accounting Periods
    seed_financial_control_data(cursor, conn)

    cursor.close()
    conn.close()
    print("[+] Accounts Database Initialization & Financial Control Layer Rebuild Complete!")


def seed_financial_control_data(cursor, conn):
    """
    Seeds default Financial Years and monthly Accounting Periods.
    Configures standard Indian financial years (Apr 1 - Mar 31) while supporting
    dynamic expansion and non-overlapping period controls.
    """
    print("[*] Initializing Financial Years and Accounting Periods...")

    fy_defs = [
        ("FY 2025-26", "2025-04-01", "2026-03-31", "OPEN"),
        ("FY 2026-27", "2026-04-01", "2027-03-31", "OPEN"),
        ("FY 2027-28", "2027-04-01", "2028-03-31", "OPEN"),
    ]

    for fy_name, start_d, end_d, status in fy_defs:
        cursor.execute("SELECT id FROM financial_years WHERE name = %s;", (fy_name,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("""
                INSERT INTO financial_years (name, start_date, end_date, status)
                VALUES (%s, %s, %s, %s);
            """, (fy_name, start_d, end_d, status))
            cursor.execute("SELECT id FROM financial_years WHERE name = %s;", (fy_name,))
            fy_row = cursor.fetchone()
            fy_id = fy_row["id"] if isinstance(fy_row, dict) else fy_row[0]

            # Generate 12 monthly accounting periods for this financial year
            # Period 1: April, Period 2: May ... Period 12: March
            s_year = int(start_d.split("-")[0])
            for p_num in range(1, 13):
                cal_month = ((p_num + 2) % 12) + 1  # 4 for p_num=1 (April), 1 for p_num=10 (Jan)
                cal_year = s_year if p_num <= 9 else (s_year + 1)

                m_start = datetime.date(cal_year, cal_month, 1)
                if cal_month in (1, 3, 5, 7, 8, 10, 12):
                    last_day = 31
                elif cal_month in (4, 6, 9, 11):
                    last_day = 30
                else:
                    last_day = 29 if (cal_year % 4 == 0 and (cal_year % 100 != 0 or cal_year % 400 == 0)) else 28
                m_end = datetime.date(cal_year, cal_month, last_day)

                p_name = m_start.strftime("%b %Y")
                cursor.execute("""
                    INSERT INTO accounting_periods 
                        (financial_year_id, period_name, period_number, start_date, end_date, status)
                    VALUES 
                        (%s, %s, %s, %s, %s, 'OPEN');
                """, (fy_id, p_name, p_num, m_start.isoformat(), m_end.isoformat()))
        else:
            fy_id = row["id"] if isinstance(row, dict) else row[0]

    conn.commit()
    print("[+] Financial Years & 12-Month Accounting Periods initialized.")


if __name__ == '__main__':
    init_database()

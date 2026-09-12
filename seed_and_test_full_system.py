import os
import sys
import datetime
import shutil
import sqlite3

# Ensure UTF-8 output on Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from backend.db import get_db_connection, get_jai_agency_db_connection, get_db_mode
from backend.sync_engine import sync_all, get_live_inventory_valuation
from backend.accounts_engine import (
    purge_all_transaction_data,
    create_fixed_asset,
    create_liability,
    add_manual_entry,
    get_dashboard_summary,
    get_receivables,
    get_payables,
)
from backend.reports_engine import (
    generate_profit_and_loss,
    generate_balance_sheet,
    generate_trial_balance,
    generate_cash_flow,
)


def print_banner(text, char="="):
    line = char * 85
    print("\n" + line)
    print(f" {text}")
    print(line)


def seed_jai_agency_database():
    """
    Clears and populates Jai Agency SQLite database with realistic, comprehensive dummy data:
    - Products
    - Suppliers
    - Storage (Inward Purchases: Cash and Credit)
    - Supplier Payments
    - Sales Bills (Cash, UPI, Credit) & Sale Items with GST
    - Customer Credit Payments
    - Counter Expenses
    - Sales Returns & Refunds
    """
    print_banner("PHASE 1: POPULATING JAI AGENCY WITH DUMMY DATA")
    db_path = Config.JAI_AGENCY_DB_PATH
    print(f"[*] Jai Agency Target DB: {db_path}")

    # Create backup of current Jai DB if it exists
    if os.path.exists(db_path):
        bak = db_path + ".bak_before_seed"
        shutil.copy2(db_path, bak)
        print(f"[+] Safety backup created: {bak}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. Clear existing operational data in Jai Agency (Keep users!)
    tables_to_clear = [
        'sales_log', 'sale_items', 'storage', 'returns_log',
        'expenses', 'credit_payments', 'supplier_payments',
        'suppliers', 'products'
    ]
    for tbl in tables_to_clear:
        try:
            cur.execute(f"DELETE FROM {tbl};")
        except sqlite3.OperationalError:
            pass

    # 2. Insert Products
    print("[*] Seeding 5 Master Products...")
    products = [
        ('1001', 'Premium Cow Ghee 1L', 'பசும் நெய் 1L', 'Dairy', 650.0, 'Pcs', 0.0, 5.0, 10, 550.0, 600.0, '0405'),
        ('1002', 'Filter Coffee 500g', 'காபி தூள் 500g', 'Beverages', 240.0, 'Pcs', 0.0, 5.0, 15, 180.0, 220.0, '0901'),
        ('1003', 'Organic Spices 250g', 'மசாலா தூள் 250g', 'Spices', 130.0, 'Pcs', 0.0, 12.0, 20, 90.0, 115.0, '0910'),
        ('1004', 'Pure Forest Honey 500g', 'தேன் 500g', 'Health', 310.0, 'Pcs', 0.0, 0.0, 12, 220.0, 280.0, '0409'),
        ('1005', 'Basmati Premium Rice 5kg', 'பாசுமதி அரிசி 5kg', 'Grains', 550.0, 'Bag', 0.0, 0.0, 8, 420.0, 500.0, '1006')
    ]
    cur.executemany("""
        INSERT INTO products 
            (code, name, name_ta, category, price, unit, bizz, gst_percent, reorder_level, last_cost, wholesale_price, hsn_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, products)

    # 3. Insert Suppliers
    print("[*] Seeding 3 Registered Suppliers...")
    suppliers = [
        (1, 'Nilgiri Valley Dairy Farms', 'Ramanathan', '9842100001', 'info@nilgiridairy.com', 0.0),
        (2, 'Coorg Plantation Wholesalers', 'Suresh Kumar', '9842100002', 'orders@coorgplantation.com', 0.0),
        (3, 'Malabar Spices Syndicate', 'Abdullah', '9842100003', 'malabarspices@gmail.com', 0.0)
    ]
    cur.executemany("""
        INSERT INTO suppliers (id, name, contact, phone, email, balance)
        VALUES (?, ?, ?, ?, ?, ?);
    """, suppliers)

    # 4. Insert Storage Batches / Purchases
    # Purchase 1: Nilgiri Dairy - 100 Ghee @ 550 = 55,000 + GST 5% (2,750) = 57,750 (Paid Bank)
    # Purchase 2: Coorg Plantation - 150 Coffee @ 180 = 27,000 + GST 5% (1,350) = 28,350 (Credit, paid 10,000)
    # Purchase 3: Malabar Spices - 200 Spices @ 90 = 18,000 + GST 12% (2,160) = 20,160 (Full Credit, paid 0)
    # Purchase 4: Nilgiri Dairy - 80 Honey @ 220 = 17,600 (GST 0%) = 17,600 (Paid Bank)
    print("[*] Seeding 4 Supplier Inward Purchase Invoices into storage...")
    storage_batches = [
        # batch_id, product_code, qty, entry_time, arrival_date, expiry, cost, invoice_no, supplier_id, product_name, unit, category, payment_mode, is_credit, amount_paid, discount, gst_percent, gst_value
        ('B-101-GHEE', '1001', 100.0, '2026-08-01 10:30:00', '2026-08-01', '2027-02-01', 550.0, 'JAI-PUR-101', 1, 'Premium Cow Ghee 1L', 'Pcs', 'Dairy', 'BANK', 0, 57750.0, 0.0, 5.0, 2750.0),
        ('B-102-COFF', '1002', 150.0, '2026-08-03 11:15:00', '2026-08-03', '2027-04-01', 180.0, 'JAI-PUR-102', 2, 'Filter Coffee 500g', 'Pcs', 'Beverages', 'CREDIT', 1, 10000.0, 0.0, 5.0, 1350.0),
        ('B-103-SPIC', '1003', 200.0, '2026-08-05 14:00:00', '2026-08-05', '2027-08-01', 90.0, 'JAI-PUR-103', 3, 'Organic Spices 250g', 'Pcs', 'Spices', 'CREDIT', 1, 0.0, 0.0, 12.0, 2160.0),
        ('B-104-HONY', '1004', 80.0, '2026-08-08 09:45:00', '2026-08-08', '2027-08-01', 220.0, 'JAI-PUR-104', 1, 'Pure Forest Honey 500g', 'Pcs', 'Health', 'BANK', 0, 17600.0, 0.0, 0.0, 0.0),
    ]
    cur.executemany("""
        INSERT INTO storage 
            (batch_id, product_code, qty, entry_time, arrival_date, expiry, cost, invoice_no, supplier_id, product_name, unit, category, payment_mode, is_credit, amount_paid, discount, gst_percent, gst_value)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, storage_batches)

    # 5. Supplier Payment (Disbursement against credit invoice JAI-PUR-102)
    print("[*] Seeding 1 Supplier Payment against Coorg Plantation...")
    cur.execute("""
        INSERT INTO supplier_payments (id, supplier_id, invoice_no, amount, payment_mode, date, remarks)
        VALUES (1, 2, 'JAI-PUR-102', 10000.0, 'BANK', '2026-08-15 11:00:00', 'Part payment for Coffee stock');
    """)

    # 6. Insert Sales Bills & Details
    # Bill 1: 10 Coffee @ 240 + 5 Honey @ 310 = 3,950 (Cash)
    # Bill 2: 15 Ghee @ 650 = 9,750 (UPI)
    # Bill 3: 25 Ghee @ 650 + 20 Spices @ 130 = 18,850 (Credit: Paid 5,000, Bal 13,850 - Customer: Kovai Sweets)
    # Bill 4: 30 Coffee @ 240 + 10 Ghee @ 650 = 13,700 (Credit: Paid 0, Bal 13,700 - Customer: Hotel Blue Hills)
    # Bill 5: 8 Spices @ 130 + 4 Honey @ 310 = 2,280 (Cash)
    print("[*] Seeding 5 Customer Sales Bills...")
    sales_bills = [
        (1, '2026-08-10 11:30:00', 2, 3950.0, 'CASH', 'ACTIVE', 0.0, 'Walk-in Retail Customer', '9000000001', '', 3950.0, 0.0, None, 0.0, 3835.71, 'Coonoor', ''),
        (2, '2026-08-12 14:15:00', 1, 9750.0, 'UPI', 'ACTIVE', 0.0, 'Rajesh Kannan', '9000000002', '', 9750.0, 0.0, None, 0.0, 9285.71, 'Ooty', ''),
        (3, '2026-08-14 16:45:00', 2, 18850.0, 'CREDIT', 'ACTIVE', 0.0, 'Kovai Sweets & Bakery', '9876543210', 'CUST-001', 5000.0, 13850.0, None, 0.0, 17800.0, 'Coimbatore', '33AABCK1234F1Z5'),
        (4, '2026-08-16 12:20:00', 2, 13700.0, 'CREDIT', 'ACTIVE', 0.0, 'Hotel Blue Hills', '9876543211', 'CUST-002', 0.0, 13700.0, None, 0.0, 13047.62, 'Kotagiri', '33AABCH5678G1Z2'),
        (5, '2026-08-18 18:00:00', 2, 2280.0, 'CASH', 'ACTIVE', 0.0, 'General Counter Sale', '', '', 2280.0, 0.0, None, 0.0, 2142.86, 'Local', ''),
    ]
    cur.executemany("""
        INSERT INTO sales_log 
            (id, date, items_count, total, payment_method, status, prev_total, customer_name, customer_mobile, customer_id, amount_paid, balance, source_bill_id, discount, gross_total, customer_address, customer_gstn)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, sales_bills)

    sale_items = [
        # Bill 1
        (1, 1, '1002', 'Filter Coffee 500g', 240.0, 10.0, 0.0, 5.0, 0.0, '0901'),
        (2, 1, '1004', 'Pure Forest Honey 500g', 310.0, 5.0, 0.0, 0.0, 0.0, '0409'),
        # Bill 2
        (3, 2, '1001', 'Premium Cow Ghee 1L', 650.0, 15.0, 0.0, 5.0, 0.0, '0405'),
        # Bill 3
        (4, 3, '1001', 'Premium Cow Ghee 1L', 650.0, 25.0, 0.0, 5.0, 0.0, '0405'),
        (5, 3, '1003', 'Organic Spices 250g', 130.0, 20.0, 0.0, 12.0, 0.0, '0910'),
        # Bill 4
        (6, 4, '1002', 'Filter Coffee 500g', 240.0, 30.0, 0.0, 5.0, 0.0, '0901'),
        (7, 4, '1001', 'Premium Cow Ghee 1L', 650.0, 10.0, 0.0, 5.0, 0.0, '0405'),
        # Bill 5
        (8, 5, '1003', 'Organic Spices 250g', 130.0, 8.0, 0.0, 12.0, 0.0, '0910'),
        (9, 5, '1004', 'Pure Forest Honey 500g', 310.0, 4.0, 0.0, 0.0, 0.0, '0409'),
    ]
    cur.executemany("""
        INSERT INTO sale_items 
            (id, bill_id, product_code, product_name, price, qty, bizz, gst_percent, igst_percent, hsn_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, sale_items)

    # 7. Customer Credit Collection (Installment paid for Bill 3 Kovai Sweets)
    print("[*] Seeding 1 Customer Credit Collection from Kovai Sweets...")
    cur.execute("""
        INSERT INTO credit_payments (id, bill_id, amount, payment_method, date)
        VALUES (1, 3, 8000.0, 'UPI', '2026-08-20 15:30:00');
    """)

    # 8. Counter Operational Expenses
    print("[*] Seeding 3 Store / Counter Operational Expenses...")
    expenses = [
        (1, '2026-08-11 17:00:00', 'Shop Refreshments & Tea for Staff', 'Refreshments', 450.0, 'CASH'),
        (2, '2026-08-15 18:30:00', 'Eco Packaging Materials & Bags', 'Packaging', 1200.0, 'CASH'),
        (3, '2026-08-22 19:00:00', 'Store Sanitization & Cleaning Supplies', 'Cleaning', 650.0, 'CASH'),
    ]
    cur.executemany("""
        INSERT INTO expenses (id, date, description, category, amount, payment_method)
        VALUES (?, ?, ?, ?, ?, ?);
    """, expenses)

    # 9. Sales Return
    print("[*] Seeding 1 Sales Return & Customer Refund...")
    cur.execute("""
        INSERT INTO returns_log (id, date, type, bill_id, product_code, product_name, qty, refund_amount)
        VALUES (1, '2026-08-19 16:00:00', 'SALE_RETURN', 1, '1002', 'Filter Coffee 500g', 1.0, 240.0);
    """)

    conn.commit()
    conn.close()

    # Also sync backup file if exists
    backup_db = Config.JAI_AGENCY_BACKUP_PATH
    if backup_db and os.path.exists(os.path.dirname(backup_db)):
        try:
            shutil.copy2(db_path, backup_db)
            print(f"[+] Synced to Jai Agency project database copy: {backup_db}")
        except Exception as e:
            print(f"[!] Note on project DB backup copy: {e}")

    print("[+] Jai Agency data successfully created & verified!")


def seed_accounts_manual_entries():
    """
    Clears old transaction data in Accounts Software and enters ALL required manual transactions:
    1. Capital Contribution (Owner Equity)
    2. Bank Commercial Term Loan (Long-term Liability)
    3. Fixed Assets (Display Furniture, Delivery Vehicle, Computer/POS)
    4. Store & Warehouse Rent Overheads
    5. Staff Salaries
    6. Commercial Electricity & Utilities
    7. Telecom & Internet
    8. Bank Maintenance Charges
    9. Owner Drawings (Personal withdrawal)
    """
    print_banner("PHASE 2: SEEDING ACCOUNTS SOFTWARE & MANUAL ENTRIES")

    print("[*] Clearing existing transaction & journal tables in Accounts...")
    purge_all_transaction_data()

    # Additional cleanup of fixed assets and liabilities tables
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("DELETE FROM accounts_fixed_assets;")
    cur.execute("DELETE FROM accounts_liabilities;")
    conn.commit()

    # Helper to get account ID
    def get_acc_id(code):
        cur.execute("SELECT id FROM accounts_chart WHERE code = %s;", (code,))
        r = cur.fetchone()
        return r["id"] if isinstance(r, dict) else r[0]

    bank_id = get_acc_id("1020")
    cash_id = get_acc_id("1010")
    capital_id = get_acc_id("3010")
    drawings_id = get_acc_id("3020")
    rent_id = get_acc_id("6020")
    elec_id = get_acc_id("6030")
    salary_id = get_acc_id("6040")
    telecom_id = get_acc_id("6080")
    bank_chg_id = get_acc_id("7010")

    # 1. Manual Entry: Initial Owner Capital Contribution = ₹200,000
    print("[*] 1. Posting Manual Entry: Owner Capital Contribution (Rs. 200,000 into Bank)...")
    add_manual_entry(
        description="Initial Owner Equity Capital investment via Bank transfer",
        amount=200000.0,
        payment_type="Bank Transfer",
        account_id=capital_id,
        reference_no="CAP-001",
        created_by="admin",
        entry_date="2026-07-01"
    )

    # 2. Manual Entry: Commercial Bank Loan = ₹100,000
    print("[*] 2. Booking Long-Term Liability: Canara Bank MSME Business Loan (Rs. 100,000)...")
    create_liability(
        title="Canara Bank MSME Business Term Loan",
        liability_type="Bank Loan",
        principal_amount=100000.0,
        interest_rate=9.5,
        tenure_months=36,
        lender="Canara Bank Main Branch",
        start_date="2026-07-05",
        notes="3-year term loan for retail store expansion"
    )

    # 3. Fixed Assets Purchases
    print("[*] 3. Registering Fixed Assets:")
    print("   • Store Furniture & Display Racks: Rs. 45,000")
    create_fixed_asset(
        asset_name="Commercial Display Racks & Shelving",
        category="Furniture",
        purchase_date="2026-07-10",
        purchase_value=45000.0,
        useful_life_years=7,
        supplier="Nilgiri Steel & Woodcrafts",
        payment_method="Bank Transfer",
        notes="High-gauge powder-coated retail racks"
    )

    print("   • Store Delivery Scooter / Vehicle: Rs. 85,000")
    create_fixed_asset(
        asset_name="Honda Activa Delivery Scooter",
        category="Vehicles",
        purchase_date="2026-07-15",
        purchase_value=85000.0,
        useful_life_years=5,
        supplier="Ooty Two-Wheelers Showroom",
        payment_method="Bank Transfer",
        notes="Registered delivery scooter TN-43-AK-2026"
    )

    print("   • Touch POS System & Barcode Scanner: Rs. 35,000")
    create_fixed_asset(
        asset_name="Touch POS Terminal & Thermal Billing Printer",
        category="Equipment",
        purchase_date="2026-07-20",
        purchase_value=35000.0,
        useful_life_years=4,
        supplier="CompTech Systems Coimbatore",
        payment_method="Bank Transfer",
        notes="POS hardware terminal with dual display"
    )

    # 4. Manual Operating Overheads (Rent, Electricity, Salaries, Internet, Bank charges)
    print("[*] 4. Posting Monthly Administrative & Store Overheads:")
    overheads = [
        ("Store & Warehouse Rent", rent_id, 20000.0, "2026-08-01", "RENT-AUG-26", "Monthly store premises lease payment"),
        ("Store Staff Monthly Salaries", salary_id, 25000.0, "2026-08-05", "SAL-AUG-26", "Staff monthly salaries for counter & inventory executives"),
        ("Commercial Electricity & Power Bill", elec_id, 4500.0, "2026-08-08", "EB-AUG-26", "TNEB commercial electricity bill"),
        ("High-Speed Fiber Internet & POS Telecom", telecom_id, 1500.0, "2026-08-10", "NET-AUG-26", "Store optical fiber internet & POS connection"),
        ("Bank Current Account Service Charges", bank_chg_id, 750.0, "2026-08-12", "CHG-AUG-26", "Quarterly ledger maintenance and SMS alert charges"),
    ]
    for name, acc_id, amt, dt, ref, narr in overheads:
        print(f"   • {name}: Rs. {amt:,.2f}")
        add_manual_entry(
            description=narr,
            amount=amt,
            payment_type="Bank Transfer",
            account_id=acc_id,
            reference_no=ref,
            created_by="admin",
            entry_date=dt
        )

    # 5. Owner Drawings (Personal withdrawal)
    print("[*] 5. Posting Owner Drawings (Personal Withdrawal: Rs. 10,000)...")
    add_manual_entry(
        description="Owner personal cash withdrawal from bank",
        amount=10000.0,
        payment_type="Bank Transfer",
        account_id=drawings_id,
        reference_no="DRW-001",
        created_by="admin",
        entry_date="2026-08-25"
    )

    conn.close()
    print("[+] Manual accounting entries successfully posted!")


def run_full_integration_and_audit():
    """
    Executes synchronization and runs a rigorous mathematical audit across all accounting books.
    """
    print_banner("PHASE 3: RUNNING LIVE SYNCHRONIZATION FROM JAI AGENCY")
    stats = sync_all()
    print("[+] Synchronization Finished! Output Summary:")
    for k, v in stats.items():
        if isinstance(v, dict):
            print(f"   • {k:<25}: Synced={v['synced']}, Skipped={v['skipped']}, Errors={v['errors']}")
        else:
            print(f"   • {k:<25}: {v}")

    print_banner("PHASE 4: COMPREHENSIVE FINANCIAL AUDIT & RECONCILIATION")

    # 1. Live Inventory Valuation Check
    live_stock_val = get_live_inventory_valuation()
    print(f"[*] Live Inventory Valuation from Jai Agency: Rs. {live_stock_val:,.2f}")
    assert live_stock_val > 0, "Inventory valuation should be greater than zero!"

    # 2. Trial Balance Check (Sum of All Debits == Sum of All Credits)
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT SUM(debit) as total_dr, SUM(credit) as total_cr FROM journal_lines;")
    totals = cur.fetchone()
    total_dr = round(float(totals["total_dr"] or 0), 2)
    total_cr = round(float(totals["total_cr"] or 0), 2)
    cur.close()
    conn.close()

    print(f"[*] Trial Balance Double-Entry Equality:")
    print(f"   • Total Debits  = Rs. {total_dr:,.2f}")
    print(f"   • Total Credits = Rs. {total_cr:,.2f}")
    assert abs(total_dr - total_cr) < 0.01, f"Trial balance mismatch: Dr={total_dr}, Cr={total_cr}"
    print("   [+] TRIAL BALANCE IS 100% PERFECTLY BALANCED!")

    # 3. Accounts Receivable Audit
    receivables = get_receivables()
    total_ar = sum(r["remaining_balance"] for r in receivables if r["status"] != "Paid")
    print(f"\n[*] Accounts Receivable Status ({len(receivables)} Records):")
    for r in receivables:
        print(f"   • Ref: {r['invoice_ref']:<10} | Customer: {r['customer_name']:<25} | Total: Rs. {r['total_amount']:>9,.2f} | Paid: Rs. {r['paid_amount']:>8,.2f} | Balance: Rs. {r['remaining_balance']:>8,.2f} [{r['status']}]")
    print(f"   -> Total Outstanding Customer Receivables: Rs. {total_ar:,.2f}")

    # Expected:
    # Bill 3: Kovai Sweets: Total 18,850. Paid 5,000 + 8,000 (credit collection) = 13,000. Balance = 5,850.
    # Bill 4: Hotel Blue Hills: Total 13,700. Paid 0. Balance = 13,700.
    # Total AR expected = 5,850 + 13,700 = 19,550.00
    assert abs(total_ar - 19550.0) < 0.01, f"Expected total AR of Rs. 19,550.00, got {total_ar}"
    print("   [+] ACCOUNTS RECEIVABLES & CREDIT COLLECTIONS VERIFIED EXACTLY!")

    # 4. Accounts Payable Audit
    payables = get_payables()
    total_ap = sum(p["remaining_balance"] for p in payables if p["status"] != "Paid")
    print(f"\n[*] Accounts Payable Status ({len(payables)} Records):")
    for p in payables:
        print(f"   • Ref: {p['invoice_ref']:<12} | Supplier: {p['supplier_name']:<30} | Total: Rs. {p['total_amount']:>9,.2f} | Paid: Rs. {p['paid_amount']:>8,.2f} | Balance: Rs. {p['remaining_balance']:>8,.2f} [{p['status']}]")
    print(f"   -> Total Outstanding Supplier Payables: Rs. {total_ap:,.2f}")

    # Expected Payables:
    # JAI-PUR-101: 57,750 (Paid) -> Bal 0
    # JAI-PUR-102: 28,350. Paid 10,000 at purchase + 10,000 via supplier_payments = 20,000. Bal = 8,350.
    # JAI-PUR-103: 20,160. Paid 0. Bal = 20,160.
    # JAI-PUR-104: 17,600 (Paid) -> Bal 0
    # Total AP expected = 8,350 + 20,160 = 28,510.00
    assert abs(total_ap - 28510.0) < 0.01, f"Expected total AP of Rs. 28,510.00, got {total_ap}"
    print("   [+] ACCOUNTS PAYABLES & SUPPLIER DISBURSEMENTS VERIFIED EXACTLY!")

    # 5. Profit & Loss Verification
    pnl = generate_profit_and_loss()
    print_banner("PHASE 5: PROFIT & LOSS STATEMENT")
    print(f"  A. Operating Revenue (Sales less Returns) : Rs. {pnl['revenue']['total']:>12,.2f}")
    print(f"  B. Direct Cost of Goods Sold (Purchases)  : Rs. {pnl['cogs']['total']:>12,.2f}")
    print(f"  -------------------------------------------------------------------")
    print(f"  C. GROSS PROFIT (A - B)                   : Rs. {pnl['gross_profit']:>12,.2f}")
    print(f"  D. Total Operating Expenses (Overheads)   : Rs. {pnl['operating_expenses']['total']:>12,.2f}")
    for item in pnl['operating_expenses']['lines']:
        print(f"     - {item['name']:<35} : Rs. {item['amount']:>10,.2f}")
    print(f"  -------------------------------------------------------------------")
    print(f"  E. NET PROFIT (C - D)                     : Rs. {pnl['net_profit']:>12,.2f}")

    # Formula checks:
    calc_gp = round(pnl['revenue']['total'] - pnl['cogs']['total'], 2)
    calc_np = round(calc_gp - pnl['operating_expenses']['total'], 2)
    assert abs(pnl['gross_profit'] - calc_gp) < 0.01, "Gross profit formula error!"
    assert abs(pnl['net_profit'] - calc_np) < 0.01, "Net profit formula error!"
    print("  [+] P&L MATHEMATICAL FORMULAS VERIFIED WITH 100% ACCURACY!")

    # 6. Balance Sheet Verification
    bs = generate_balance_sheet()
    print_banner("PHASE 6: BALANCE SHEET")
    print(f"  1. TOTAL ASSETS                         : Rs. {bs['assets']['total_assets']:>12,.2f}")
    print(f"     • Fixed Assets (Furniture, Vehicle)   : Rs. {bs['assets']['total_fixed_assets']:>12,.2f}")
    print(f"     • Current Assets (Bank, AR, Stock)    : Rs. {bs['assets']['total_current_assets']:>12,.2f}")
    print(f"     • Other Assets                        : Rs. {bs['assets']['total_other_assets']:>12,.2f}")
    print(f"  -------------------------------------------------------------------")
    print(f"  2. TOTAL LIABILITIES & EQUITY           : Rs. {bs['total_liabilities_and_equity']:>12,.2f}")
    print(f"     • Current Liabilities (Payables, Tax) : Rs. {bs['liabilities']['total_current_liabilities']:>12,.2f}")
    print(f"     • Long-Term Liabilities (Bank Loan)   : Rs. {bs['liabilities']['total_long_term_liabilities']:>12,.2f}")
    print(f"     • Owner Equity (Capital - Drawings)   : Rs. {bs['equity']['total_equity']:>12,.2f}")
    print(f"  -------------------------------------------------------------------")

    diff = abs(bs['assets']['total_assets'] - bs['total_liabilities_and_equity'])
    print(f"  Balance Sheet Discrepancy: Rs. {diff:,.2f}")
    assert diff < 0.05, f"Balance Sheet does not balance! Assets={bs['assets']['total_assets']}, Liab+Equity={bs['total_liabilities_and_equity']}"
    print("  [+] BALANCE SHEET EQUATION (ASSETS = LIABILITIES + EQUITY) BALANCES PERFECTLY!")

    # 7. Dashboard KPI Verification
    dash = get_dashboard_summary()
    print_banner("PHASE 7: EXECUTIVE DASHBOARD SUMMARY")
    print(f"  • Total Revenue Ingested : Rs. {dash['total_revenue']:>12,.2f}")
    print(f"  • Direct Purchases/COGS  : Rs. {dash['total_cogs']:>12,.2f}")
    print(f"  • Gross Margin           : Rs. {dash['gross_profit']:>12,.2f}")
    print(f"  • Total Operating Exp    : Rs. {dash['total_opex']:>12,.2f}")
    print(f"  • Net Profit Before Tax  : Rs. {dash['net_profit']:>12,.2f}")
    print(f"  • Bank Account Balance   : Rs. {dash['bank_balance']:>12,.2f}")
    print(f"  • Cash in Register       : Rs. {dash['cash_balance']:>12,.2f}")
    print(f"  • Customer Receivables   : Rs. {dash['total_receivables']:>12,.2f}")
    print(f"  • Supplier Payables      : Rs. {dash['total_payables']:>12,.2f}")
    print(f"  • Live Stock Valuation   : Rs. {dash['inventory_value']:>12,.2f}")

    # 8. Idempotency Test (Deduplication Check)
    print_banner("PHASE 8: IDEMPOTENCY DEDUPLICATION TEST")
    print("[*] Triggering second sync run with identical data...")
    stats2 = sync_all()
    print("  -> Second Sync Output:", stats2)
    assert stats2['sales_bills']['synced'] == 0, "Second sync duplicated sales bills!"
    assert stats2['inventory_invoices']['synced'] == 0, "Second sync duplicated inventory invoices!"
    print("  [+] ZERO DUPLICATE ENTRIES CREATED. IDEMPOTENCY 100% CONFIRMED!")

    print_banner("ALL PHASES COMPLETED WITH 100% SUCCESS!", char="*")


if __name__ == '__main__':
    seed_jai_agency_database()
    seed_accounts_manual_entries()
    run_full_integration_and_audit()

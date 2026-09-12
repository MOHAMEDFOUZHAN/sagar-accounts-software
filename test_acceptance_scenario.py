import os
import sys
import datetime
import sqlite3
import mysql.connector
from config import Config
from backend.db import get_db_connection
from backend.sync_engine import sync_all, get_live_inventory_valuation
from backend.accounts_engine import (
    add_manual_entry,
    get_chart_of_accounts,
    get_dashboard_summary,
    get_receivables
)
from backend.reports_engine import generate_profit_and_loss, generate_gst_report

def run_acceptance_test():
    print("==================================================")
    print("   RUNNING SECTION 47 ACCEPTANCE TEST SCENARIO    ")
    print("==================================================")

    # 1. Setup Database Connections
    conn_acc = get_db_connection()
    acc_cur = conn_acc.cursor(dictionary=True)

    # Clean existing journal entries and sync registry for a pure, pristine test run
    print("[*] Clearing test state for clean scenario audit...")
    acc_cur.execute("DELETE FROM journal_lines;")
    acc_cur.execute("DELETE FROM journal_entries;")
    acc_cur.execute("DELETE FROM accounting_sync_registry;")
    acc_cur.execute("DELETE FROM accounts_receivables;")
    acc_cur.execute("DELETE FROM accounts_payables;")
    conn_acc.commit()

    # 2. INVENTORY: Purchase Raw Materials = ₹3,000
    print("[*] Simulating Inventory inward purchase: Rs. 3,000 Raw Materials...")
    inv_db_path = Config.INVENTORY_DB_PATH
    if not os.path.exists(inv_db_path):
        inv_db_path = Config.INVENTORY_BACKUP_PATH
    
    inv_conn = sqlite3.connect(inv_db_path)
    inv_cur = inv_conn.cursor()
    # Clean previous test invoices
    inv_cur.execute("DELETE FROM invoices WHERE invoice_no = 'TEST-ACC-PUR-3000';")
    inv_cur.execute("""
        INSERT INTO invoices 
            (invoice_no, vendor, date, total_excl_tax, total_gst, grand_total, payment_status, remarks)
        VALUES
            ('TEST-ACC-PUR-3000', 'Standard Raw Material Mills', '2026-08-01', 3000.00, 0.00, 3000.00, 'Pending', 'Raw material batch procurement');
    """)
    inv_conn.commit()
    inv_conn.close()

    # 3. SALES: Product Sold = Rs. 5,000, GST = Rs. 900, Customer Paid Rs. 5,900 via UPI
    print("[*] Simulating Sales counter bill: Rs. 5,000 Base + Rs. 900 GST = Rs. 5,900 via UPI...")
    sales_conn = mysql.connector.connect(
        host=Config.MYSQL_HOST,
        port=Config.MYSQL_PORT,
        user=Config.MYSQL_USER,
        password=Config.MYSQL_PASSWORD,
        database=Config.SALES_DB
    )
    sales_cur = sales_conn.cursor(dictionary=True)
    sales_cur.execute("DELETE FROM bills WHERE invoice_no = 'TEST-INV-5900';")
    sales_cur.execute("""
        INSERT INTO bills
            (invoice_no, bill_date, total_amount, payment_mode, status, tsc_percent, tsc_amount, discount, balance, created_by)
        VALUES
            ('TEST-INV-5900', '2026-08-01 10:30:00', 5900.00, 'UPI', 'PAID', 18.00, 900.00, 0.00, 0.00, 'sales_counter');
    """)
    test_bill_id = sales_cur.lastrowid
    sales_conn.commit()
    sales_conn.close()

    # 4. Run Synchronization
    print("[*] Running Accounts Synchronization Engine...")
    stats1 = sync_all()
    print(f"    Sync 1 Result: {stats1}")

    # 5. MANUAL ENTRIES:
    # Bank Charge = Rs. 150
    # Shop Rent = Rs. 2,000
    print("[*] Posting Manual Accounting Entries: Bank charge Rs. 150, Shop rent Rs. 2,000...")
    acc_cur.execute("SELECT id FROM accounts_chart WHERE code = '7010';") # Bank Charges
    bank_chg_acc_id = acc_cur.fetchone()["id"]

    acc_cur.execute("SELECT id FROM accounts_chart WHERE code = '6030';") # Shop Rent
    shop_rent_acc_id = acc_cur.fetchone()["id"]

    add_manual_entry(
        description="Bank quarterly ledger maintenance charges",
        amount=150.00,
        payment_type="Bank Transfer",
        account_id=bank_chg_acc_id,
        reference_no="CHG-BNK-01",
        entry_date="2026-08-01 11:00:00"
    )

    add_manual_entry(
        description="Main commercial retail shop monthly rent",
        amount=2000.00,
        payment_type="Bank Transfer",
        account_id=shop_rent_acc_id,
        reference_no="RENT-SHOP-01",
        entry_date="2026-08-01 11:00:00"
    )

    # 6. VERIFICATION OF EXACT METRICS (P&L, GST, Working Capital)
    print("\n==================================================")
    print("           VERIFYING FINANCIAL METRICS            ")
    print("==================================================")

    pnl = generate_profit_and_loss(start_date="2026-08-01", end_date="2026-08-01")
    gst = generate_gst_report(start_date="2026-08-01", end_date="2026-08-01")
    summary = get_dashboard_summary()

    revenue = pnl["revenue"]["total"]
    cogs = pnl["cogs"]["total"]
    gross_profit = pnl["gross_profit"]
    operating_expenses = pnl["operating_expenses"]["total"]
    net_profit = pnl["net_profit"]
    gst_output = gst["total_output_gst"]

    print(f"1. Revenue:               Rs. {revenue:,.2f}  (Expected: Rs. 5,000.00)")
    print(f"2. Output GST:            Rs. {gst_output:,.2f}  (Expected: Rs. 900.00)")
    print(f"3. COGS:                  Rs. {cogs:,.2f}  (Based on inventory purchase)")
    print(f"4. Gross Profit:          Rs. {gross_profit:,.2f}  (Expected: Revenue - COGS)")
    print(f"5. Operating Expenses:    Rs. {operating_expenses:,.2f}  (Expected: Rs. 150 + Rs. 2,000 = Rs. 2,150.00)")
    print(f"6. Net Profit:            Rs. {net_profit:,.2f}  (Expected: Gross Profit - Rs. 2,150.00)")

    assert revenue == 5000.00, f"Revenue mismatch: expected 5000, got {revenue}"
    assert gst_output == 900.00, f"GST Output mismatch: expected 900, got {gst_output}"
    assert operating_expenses == 2150.00, f"Operating Expenses mismatch: expected 2150, got {operating_expenses}"
    assert gross_profit == revenue - cogs, f"Gross Profit mismatch: expected {revenue - cogs}, got {gross_profit}"
    assert net_profit == gross_profit - operating_expenses, f"Net Profit mismatch: expected {gross_profit - operating_expenses}, got {net_profit}"

    # Verify Customer Receivable = Rs. 0 (Customer fully paid via UPI)
    recs = get_receivables()
    test_recs = [r for r in recs if r["invoice_ref"] == "TEST-INV-5900"]
    assert len(test_recs) == 0 or all(r["remaining_balance"] == 0 for r in test_recs), "Customer receivable should be Rs. 0 for paid bill"
    print("7. Customer Receivable:   Rs. 0.00 (Verified - Fully Paid)")

    # 7. DEDUPLICATION TEST: Re-running sync must NOT create duplicate entries!
    print("\n[*] Testing Deduplication (Re-running sync)...")
    stats2 = sync_all()
    print(f"    Sync 2 Result: {stats2}")
    assert stats2["sales_bills"]["synced"] == 0, "Duplicate sales bill was improperly synced!"
    assert stats2["inventory_invoices"]["synced"] == 0, "Duplicate inventory invoice was improperly synced!"
    print("8. Deduplication Check:   PASSED (Sale was NOT duplicated!)")

    # Clean up test rows
    sales_conn = mysql.connector.connect(host=Config.MYSQL_HOST, port=Config.MYSQL_PORT, user=Config.MYSQL_USER, password=Config.MYSQL_PASSWORD, database=Config.SALES_DB)
    c = sales_conn.cursor()
    c.execute("DELETE FROM bills WHERE invoice_no = 'TEST-INV-5900';")
    sales_conn.commit()
    sales_conn.close()

    inv_conn = sqlite3.connect(inv_db_path)
    c = inv_conn.cursor()
    c.execute("DELETE FROM invoices WHERE invoice_no = 'TEST-ACC-PUR-3000';")
    inv_conn.commit()
    inv_conn.close()

    # Re-sync real state
    sync_all()

    print("\n==================================================")
    print("   ALL ACCEPTANCE TEST ASSERTIONS PASSED 100%!   ")
    print("==================================================")

if __name__ == '__main__':
    run_acceptance_test()

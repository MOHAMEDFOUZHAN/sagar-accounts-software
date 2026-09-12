import os
import sys
import datetime
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
    add_manual_entry,
    get_dashboard_summary,
    get_receivables,
    get_payables,
    get_general_ledger,
)
from backend.reports_engine import (
    generate_profit_and_loss,
    generate_balance_sheet,
    generate_trial_balance,
    generate_gst_report,
    generate_cash_flow,
)
from app import app


def print_banner(text, char="="):
    line = char * 85
    print("\n" + line)
    print(f" {text}")
    print(line)


def run_stage2_testing():
    print_banner("STAGE 2 TESTING: ADVANCED LIFECYCLE, DATE FILTERING & COMPLETE RECONCILIATION")
    
    # -------------------------------------------------------------------------
    # STEP 1: ADD SUBSEQUENT LIFECYCLE TRANSACTIONS TO JAI AGENCY
    # -------------------------------------------------------------------------
    print_banner("STEP 1: ADDING STAGE 2 TRANSACTIONS TO JAI AGENCY")
    db_path = Config.JAI_AGENCY_DB_PATH
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. New Sale Bill 6: Credit Sale to Kovai Sweets (Total 12,000: 2,000 paid at POS, 10,000 credit)
    print("[*] 1. Adding Sale Bill #6 in Jai Agency: Kovai Sweets - Rs. 12,000 (Paid Rs. 2,000, Bal Rs. 10,000)...")
    cur.execute("""
        INSERT INTO sales_log 
            (id, date, items_count, total, payment_method, status, prev_total, customer_name, customer_mobile, customer_id, amount_paid, balance, source_bill_id, discount, gross_total, customer_address, customer_gstn)
        VALUES (6, '2026-08-25 10:15:00', 2, 12000.0, 'CREDIT', 'ACTIVE', 0.0, 'Kovai Sweets & Bakery', '9876543210', 'CUST-001', 2000.0, 10000.0, NULL, 0.0, 11690.48, 'Coimbatore', '33AABCK1234F1Z5');
    """)
    cur.execute("""
        INSERT INTO sale_items 
            (id, bill_id, product_code, product_name, price, qty, bizz, gst_percent, igst_percent, hsn_code)
        VALUES 
            (10, 6, '1001', 'Premium Cow Ghee 1L', 650.0, 10.0, 0.0, 5.0, 0.0, '0405'),
            (11, 6, '1005', 'Basmati Premium Rice 5kg', 550.0, 10.0, 0.0, 0.0, 0.0, '1006');
    """)

    # 2. Customer Credit Payment 2: Hotel Blue Hills completely clears Bill #4 (Rs. 13,700)
    print("[*] 2. Adding Customer Credit Payment #2: Hotel Blue Hills pays Rs. 13,700 via Bank (full settlement)...")
    cur.execute("""
        INSERT INTO credit_payments (id, bill_id, amount, payment_method, date)
        VALUES (2, 4, 13700.0, 'BANK', '2026-08-27 14:00:00');
    """)

    # 3. Customer Credit Payment 3: Kovai Sweets pays remaining balance of Bill #3 (Rs. 5,850)
    print("[*] 3. Adding Customer Credit Payment #3: Kovai Sweets pays Rs. 5,850 via UPI (clears Bill #3)...")
    cur.execute("""
        INSERT INTO credit_payments (id, bill_id, amount, payment_method, date)
        VALUES (3, 3, 5850.0, 'UPI', '2026-08-28 16:30:00');
    """)

    # 4. Storage Purchase 5: Inward Purchase from Malabar Spices (Inv JAI-PUR-105: 15,120, Paid 5,120, Credit 10,000)
    print("[*] 4. Adding Storage Purchase Invoice JAI-PUR-105: Malabar Spices - Rs. 15,120 (Paid Rs. 5,120, Bal Rs. 10,000)...")
    cur.execute("""
        INSERT INTO storage 
            (batch_id, product_code, qty, entry_time, arrival_date, expiry, cost, invoice_no, supplier_id, product_name, unit, category, payment_mode, is_credit, amount_paid, discount, gst_percent, gst_value)
        VALUES 
            ('B-105-SPIC', '1003', 150.0, '2026-08-24 11:00:00', '2026-08-24', '2027-08-24', 90.0, 'JAI-PUR-105', 3, 'Organic Spices 250g', 'Pcs', 'Spices', 'BANK', 1, 5120.0, 0.0, 12.0, 1620.0);
    """)

    # 5. Supplier Payment 2: Full settlement to Coorg Plantation against JAI-PUR-102 (Rs. 8,350)
    print("[*] 5. Adding Supplier Payment #2: Coorg Plantation - Rs. 8,350 via Bank (full settlement)...")
    cur.execute("""
        INSERT INTO supplier_payments (id, supplier_id, invoice_no, amount, payment_mode, date, remarks)
        VALUES (2, 2, 'JAI-PUR-102', 8350.0, 'BANK', '2026-08-29 11:30:00', 'Final settlement for Coffee invoice');
    """)

    # 6. Counter Expense 4: Store Electrical Repairs (Rs. 850)
    print("[*] 6. Adding Counter Expense #4: Store Electrical Repairs - Rs. 850 (Cash)...")
    cur.execute("""
        INSERT INTO expenses (id, date, description, category, amount, payment_method)
        VALUES (4, '2026-08-26 15:00:00', 'Store Electrical Repairs & LED Bulbs', 'Maintenance', 850.0, 'CASH');
    """)

    # 7. Sales Return 2: Customer refund 1x Ghee (Rs. 650)
    print("[*] 7. Adding Sales Return #2: 1x Ghee returned - Rs. 650 (Cash refund)...")
    cur.execute("""
        INSERT INTO returns_log (id, date, type, bill_id, product_code, product_name, qty, refund_amount)
        VALUES (2, '2026-08-27 17:00:00', 'SALE_RETURN', 2, '1001', 'Premium Cow Ghee 1L', 1.0, 650.0);
    """)

    conn.commit()
    conn.close()
    print("[+] Stage 2 transactions saved to Jai Agency DB!")

    # -------------------------------------------------------------------------
    # STEP 2: STAGE 2 MANUAL ACCOUNTING ENTRIES IN ACCOUNTS SOFTWARE
    # -------------------------------------------------------------------------
    print_banner("STEP 2: ADDING STAGE 2 MANUAL ACCOUNTING ENTRIES")
    acc_conn = get_db_connection()
    acc_cur = acc_conn.cursor(dictionary=True)

    def get_acc_id(code):
        acc_cur.execute("SELECT id FROM accounts_chart WHERE code = %s;", (code,))
        r = acc_cur.fetchone()
        return r["id"] if isinstance(r, dict) else r[0]

    bank_id = get_acc_id("1020")
    cash_id = get_acc_id("1010")
    deprec_id = get_acc_id("6080")       # Depreciation Expense
    acc_dep_id = get_acc_id("1160")      # Accumulated Depreciation
    loan_id = get_acc_id("2110")         # Bank Loan
    fin_chg_id = get_acc_id("7020")      # Loan Interest / Financial Charge

    # 1. Monthly Depreciation Write-Off on Fixed Assets = ₹2,600.00
    print("[*] 1. Booking August Depreciation on Fixed Assets: Rs. 2,600.00...")
    acc_cur.execute("""
        INSERT INTO journal_entries
            (entry_number, entry_date, source_module, source_entity, reference_no, narration, status, created_by)
        VALUES
            ('JV-DEP-202608', '2026-08-31', 'manual', 'depreciation', 'DEP-AUG-26', 'August Monthly Fixed Assets Depreciation Write-off', 'POSTED', 'admin');
    """)
    dep_entry_id = acc_cur.lastrowid
    # Dr Depreciation Expense (Operating Expense), Cr Accumulated Depreciation (Contra Asset)
    acc_cur.execute("INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, 2600.00, 0.00);", (dep_entry_id, deprec_id))
    acc_cur.execute("INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, 0.00, 2600.00);", (dep_entry_id, acc_dep_id))

    # 2. Commercial Loan Installment: ₹3,200 (Principal ₹2,400 + Interest ₹800) paid via Bank
    print("[*] 2. Booking Canara Bank Loan EMI Payment: Rs. 3,200 (Principal Rs. 2,400 + Interest Rs. 800)...")
    acc_cur.execute("""
        INSERT INTO journal_entries
            (entry_number, entry_date, source_module, source_entity, reference_no, narration, status, created_by)
        VALUES
            ('JV-EMI-202608', '2026-08-31', 'manual', 'loan_repayment', 'EMI-AUG-26', 'Canara Bank Monthly Loan EMI: Principal + Interest', 'POSTED', 'admin');
    """)
    emi_entry_id = acc_cur.lastrowid
    # Dr Bank Loan Liability (reducing debt), Dr Financial Cost (Interest), Cr Bank Account
    acc_cur.execute("INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, 2400.00, 0.00);", (emi_entry_id, loan_id))
    acc_cur.execute("INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, 800.00, 0.00);", (emi_entry_id, fin_chg_id))
    acc_cur.execute("INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, 0.00, 3200.00);", (emi_entry_id, bank_id))

    # Also update accounts_liabilities table
    acc_cur.execute("UPDATE accounts_liabilities SET outstanding_balance = outstanding_balance - 2400.00 WHERE liability_type = 'Bank Loan';")

    # 3. Surplus Cash Register Deposit into Bank Account: ₹8,000.00
    print("[*] 3. Banking Cash Register Surplus: Rs. 8,000.00 (Dr Bank, Cr Cash)...")
    acc_cur.execute("""
        INSERT INTO journal_entries
            (entry_number, entry_date, source_module, source_entity, reference_no, narration, status, created_by)
        VALUES
            ('JV-TRF-202608', '2026-08-30', 'manual', 'cash_deposit', 'DEP-TILL-01', 'Deposit of counter cash register surplus into Bank account', 'POSTED', 'admin');
    """)
    trf_entry_id = acc_cur.lastrowid
    acc_cur.execute("INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, 8000.00, 0.00);", (trf_entry_id, bank_id))
    acc_cur.execute("INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (%s, %s, 0.00, 8000.00);", (trf_entry_id, cash_id))

    acc_conn.commit()
    acc_cur.close()
    acc_conn.close()
    print("[+] Stage 2 manual entries successfully committed!")

    # -------------------------------------------------------------------------
    # STEP 3: RUN LIVE SYNC PIPELINE
    # -------------------------------------------------------------------------
    print_banner("STEP 3: RUNNING STAGE 2 AUTOMATED SYNCHRONIZATION")
    stats = sync_all()
    print("[+] Stage 2 Sync Finished! Results:")
    for k, v in stats.items():
        if isinstance(v, dict):
            print(f"   • {k:<25}: Synced={v['synced']}, Skipped={v['skipped']}, Errors={v['errors']}")
        else:
            print(f"   • {k:<25}: {v}")

    # Verify that exactly the new Stage 2 items were synced, and previous items were deduplicated
    assert stats["sales_bills"]["synced"] == 1, f"Expected 1 new sales bill synced, got {stats['sales_bills']['synced']}"
    assert stats["sales_bills"]["skipped"] == 5, f"Expected 5 old sales bills deduplicated, got {stats['sales_bills']['skipped']}"
    assert stats["customer_credit_payments"]["synced"] == 2, f"Expected 2 new credit payments synced, got {stats['customer_credit_payments']['synced']}"
    assert stats["inventory_invoices"]["synced"] == 1, f"Expected 1 new purchase invoice synced, got {stats['inventory_invoices']['synced']}"
    assert stats["supplier_payments"]["synced"] == 1, f"Expected 1 new supplier payment synced, got {stats['supplier_payments']['synced']}"
    assert stats["sales_expenses"]["synced"] == 1, f"Expected 1 new expense synced, got {stats['sales_expenses']['synced']}"
    assert stats["sales_returns"]["synced"] == 1, f"Expected 1 new return synced, got {stats['sales_returns']['synced']}"
    print("[+] STAGE 2 INCREMENTAL DEDUPLICATION AND SYNC VERIFIED 100%!")

    # -------------------------------------------------------------------------
    # STEP 4: RECONCILIATION OF RECEIVABLES AND PAYABLES
    # -------------------------------------------------------------------------
    print_banner("STEP 4: RECONCILIATION OF RECEIVABLES & PAYABLES")
    
    # Receivables check
    receivables = get_receivables()
    print(f"[*] Customer Receivables Verification ({len(receivables)} records):")
    for r in receivables:
        print(f"   • Ref: {r['invoice_ref']:<8} | Customer: {r['customer_name']:<25} | Total: Rs. {r['total_amount']:>9,.2f} | Paid: Rs. {r['paid_amount']:>9,.2f} | Balance: Rs. {r['remaining_balance']:>9,.2f} [{r['status']}]")

    # Hotel Blue Hills (Bill 4) was 13,700, now paid 13,700 -> Remaining 0.00 [Paid]
    blue_hills = next(r for r in receivables if "Hotel Blue Hills" in r["customer_name"])
    assert blue_hills["remaining_balance"] == 0.0 and blue_hills["status"] == "Paid", "Hotel Blue Hills should be fully Paid!"

    # Kovai Sweets (Bill 3) was 18,850, paid 5,000 + 8,000 + 5,850 = 18,850 -> Remaining 0.00 [Paid]
    kovai_b3 = next(r for r in receivables if "INV-3" in r["invoice_ref"])
    assert kovai_b3["remaining_balance"] == 0.0 and kovai_b3["status"] == "Paid", "Bill 3 Kovai Sweets should be fully Paid!"

    # Kovai Sweets (Bill 6) was 12,000, paid 2,000 at checkout -> Remaining 10,000 [Partial]
    kovai_b6 = next(r for r in receivables if "INV-6" in r["invoice_ref"])
    assert kovai_b6["remaining_balance"] == 10000.0 and kovai_b6["status"] == "Partial", "Bill 6 Kovai Sweets should have Rs. 10,000 remaining balance!"

    open_ar = sum(r["remaining_balance"] for r in receivables if r["status"] != "Paid")
    assert open_ar == 10000.0, f"Total open AR should be exactly Rs. 10,000.00, got {open_ar}"
    print(f"   [+] TOTAL OPEN RECEIVABLES RECONCILED EXACTLY: Rs. {open_ar:,.2f}")

    # Payables check
    payables = get_payables()
    print(f"\n[*] Supplier Payables Verification ({len(payables)} records):")
    for p in payables:
        print(f"   • Ref: {p['invoice_ref']:<12} | Supplier: {p['supplier_name']:<30} | Total: Rs. {p['total_amount']:>9,.2f} | Paid: Rs. {p['paid_amount']:>9,.2f} | Balance: Rs. {p['remaining_balance']:>9,.2f} [{p['status']}]")

    # JAI-PUR-102 (Coorg Plantation) was 28,350, paid 10,000 at arrival + 10,000 payment 1 + 8,350 payment 2 = 28,350 -> Remaining 0.00 [Paid]
    coorg_p = next(p for p in payables if "JAI-PUR-102" in p["invoice_ref"])
    assert coorg_p["remaining_balance"] == 0.0 and coorg_p["status"] == "Paid", "Coorg Plantation JAI-PUR-102 should be fully Paid!"

    # JAI-PUR-105 (Malabar Spices) was 15,120, paid 5,120 -> Remaining 10,000 [Partial]
    malabar_105 = next(p for p in payables if "JAI-PUR-105" in p["invoice_ref"])
    assert malabar_105["remaining_balance"] == 10000.0 and malabar_105["status"] == "Partial", "JAI-PUR-105 should have Rs. 10,000 remaining balance!"

    open_ap = sum(p["remaining_balance"] for p in payables if p["status"] != "Paid")
    # Open AP = JAI-PUR-103 (20,160) + JAI-PUR-105 (10,000) = 30,160.00
    assert open_ap == 30160.0, f"Total open AP should be exactly Rs. 30,160.00, got {open_ap}"
    print(f"   [+] TOTAL OPEN PAYABLES RECONCILED EXACTLY: Rs. {open_ap:,.2f}")

    # -------------------------------------------------------------------------
    # STEP 5: RIGOROUS DATE-FILTERING TESTS ACROSS REPORTING PERIODS
    # -------------------------------------------------------------------------
    print_banner("STEP 5: TESTING DATE FILTERING ACROSS PERIODS")
    
    test_periods = [
        ("Period 1: July 2026 (Pre-Trading Setup)", "2026-07-01", "2026-07-31"),
        ("Period 2: August 1-15, 2026 (Early Trading)", "2026-08-01", "2026-08-15"),
        ("Period 3: August 16-31, 2026 (Late Trading & Settlements)", "2026-08-16", "2026-08-31"),
        ("Period 4: Full August 2026", "2026-08-01", "2026-08-31"),
        ("Period 5: All Time (Complete Lifecycle)", None, None),
    ]

    for label, s_date, e_date in test_periods:
        print(f"\n[*] Evaluating {label} [From: {s_date or 'Beginning'} To: {e_date or 'Present'}]:")
        
        # P&L
        pnl_p = generate_profit_and_loss(s_date, e_date)
        rev = pnl_p["revenue"]["total"]
        cogs = pnl_p["cogs"]["total"]
        gp = pnl_p["gross_profit"]
        opex = pnl_p["operating_expenses"]["total"]
        np = pnl_p["net_profit"]
        
        print(f"   • P&L: Revenue=Rs. {rev:,.2f} | COGS=Rs. {cogs:,.2f} | Gross Profit=Rs. {gp:,.2f} | OpEx=Rs. {opex:,.2f} | Net Profit=Rs. {np:,.2f}")
        assert abs(gp - round(rev - cogs, 2)) < 0.01, f"Gross profit formula failed in {label}"
        assert abs(np - round(gp - opex, 2)) < 0.01, f"Net profit formula failed in {label}"

        # In July 2026 (before store opened), Revenue and COGS must be exactly 0
        if label.startswith("Period 1"):
            assert rev == 0.0 and cogs == 0.0, "July revenue and COGS should be 0.0!"

        # Trial Balance for this cutoff
        tb_p = generate_trial_balance(e_date)
        tb_dr = tb_p["total_debit"]
        tb_cr = tb_p["total_credit"]
        print(f"   • Trial Balance (Cutoff {e_date or 'All'}): Dr=Rs. {tb_dr:,.2f} | Cr=Rs. {tb_cr:,.2f} (Balanced: {tb_p['is_balanced']})")
        assert tb_p["is_balanced"], f"Trial balance out of balance in {label}!"

        # Balance Sheet for this cutoff
        bs_p = generate_balance_sheet(e_date)
        bs_assets = bs_p["assets"]["total_assets"]
        bs_liab_eq = bs_p["total_liabilities_and_equity"]
        diff = abs(bs_assets - bs_liab_eq)
        print(f"   • Balance Sheet (Cutoff {e_date or 'All'}): Assets=Rs. {bs_assets:,.2f} | Liab+Eq=Rs. {bs_liab_eq:,.2f} (Diff=Rs. {diff:,.2f})")
        assert diff < 0.05, f"Balance Sheet does not balance in {label}!"

        # GST Report for this period
        gst_p = generate_gst_report(s_date, e_date)
        print(f"   • GST Statement: Output GST=Rs. {gst_p['total_output_gst']:,.2f} | Input Credit=Rs. {gst_p['total_input_gst']:,.2f} | Net Payable=Rs. {gst_p['net_gst_payable']:,.2f}")

    print("\n[+] ALL DATE PERIODS AND TIME SLICES EVALUATED WITH 100% MATHEMATICAL INTEGRITY!")

    # -------------------------------------------------------------------------
    # STEP 6: VERIFY ALL WEB APPLICATION PAGES WITH FLASK TEST CLIENT
    # -------------------------------------------------------------------------
    print_banner("STEP 6: TESTING FLASK WEB APPLICATION PAGES & FILTERS")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"

    endpoints_to_test = [
        ("/dashboard", "Executive Dashboard"),
        ("/sync", "Sync Health & Deduplication Monitor"),
        ("/sales-ledger", "Sales Ledger Vouchers"),
        ("/receivables", "Accounts Receivable"),
        ("/payables", "Accounts Payable"),
        ("/tax", "Tax & GST Statement"),
        ("/equity", "Owner Equity & Capital Statement"),
        ("/assets-liabilities", "Fixed Assets & Loans Register"),
        ("/ledger?account_id=2&start_date=2026-08-01&end_date=2026-08-31", "Bank Ledger with August Date Filter"),
        ("/reports?tab=pnl&start_date=2026-08-01&end_date=2026-08-31", "August Profit & Loss Report"),
        ("/reports?tab=bs&end_date=2026-08-31", "August 31 Balance Sheet"),
        ("/reports?tab=tb&end_date=2026-08-31", "August 31 Trial Balance"),
        ("/reports?tab=gst&start_date=2026-08-01&end_date=2026-08-31", "August GST Statement"),
    ]

    for url, desc in endpoints_to_test:
        resp = client.get(url)
        print(f"   • Testing {desc:<45}: Status {resp.status_code}")
        assert resp.status_code == 200, f"Page {url} failed with status {resp.status_code}"
        # Check that page actually rendered HTML content
        assert len(resp.data) > 500, f"Page {url} returned empty content!"

    print("\n[+] ALL WEB PAGES AND DATE-FILTERED REPORTS RESPONDED 200 OK WITH VALID RENDERED CONTENT!")

    print_banner("STAGE 2 AUDIT & TESTING PASSED WITH 100% PERFECTION!", char="*")


if __name__ == '__main__':
    run_stage2_testing()

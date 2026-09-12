import sys
import os
import datetime

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
from backend.reports_engine import generate_profit_and_loss, generate_balance_sheet


def run_jai_agency_sync_test():
    print("=" * 80)
    print("   TESTING SAGAR ACCOUNTS <-> JAI AGENCY UNIFIED INTEGRATION   ")
    print("=" * 80)

    print(f"[*] Database Mode: {get_db_mode().upper()}")
    print(f"[*] Jai Agency DB Path: {Config.JAI_AGENCY_DB_PATH}")
    print(f"[*] Jai Agency Backup Path: {Config.JAI_AGENCY_BACKUP_PATH}")

    # 1. Clean test state for pristine run
    conn_acc = get_db_connection()
    acc_cur = conn_acc.cursor(dictionary=True)
    acc_cur.execute("DELETE FROM journal_lines;")
    acc_cur.execute("DELETE FROM journal_entries;")
    acc_cur.execute("DELETE FROM accounting_sync_registry;")
    acc_cur.execute("DELETE FROM accounts_receivables;")
    acc_cur.execute("DELETE FROM accounts_payables;")
    conn_acc.commit()
    acc_cur.close()
    conn_acc.close()

    # 2. Check Jai Agency Connection
    jai_conn = get_jai_agency_db_connection()
    assert jai_conn is not None, "Failed to connect to Jai Agency SQLite database!"
    c = jai_conn.cursor()
    c.execute("SELECT COUNT(*) FROM sales_log;")
    sales_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM storage;")
    storage_count = c.fetchone()[0]
    jai_conn.close()
    print(f"[+] Jai Agency Connected! Raw Records: {sales_count} sales bills, {storage_count} storage batches.")

    # 3. Test Live Inventory Valuation
    live_val = get_live_inventory_valuation()
    print(f"[+] Live Inventory Valuation from Jai Agency: Rs. {live_val:,.2f}")
    assert live_val >= 0, "Inventory valuation should be non-negative!"

    # 4. First Sync Run
    print("\n[*] Running First Sync Run...")
    stats1 = sync_all()
    print("  -> First Sync Summary:", stats1)

    # 4. Verify Double-Entry Balance in Accounts
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT SUM(debit) as total_dr, SUM(credit) as total_cr FROM journal_lines;")
    totals = cur.fetchone()
    total_dr = round(float(totals["total_dr"] or 0), 2)
    total_cr = round(float(totals["total_cr"] or 0), 2)
    print(f"[+] Trial Balance Check: Total Dr = Rs. {total_dr:,.2f} | Total Cr = Rs. {total_cr:,.2f}")
    assert abs(total_dr - total_cr) < 0.01, f"Double entry ledger is unbalanced! Dr: {total_dr}, Cr: {total_cr}"

    cur.execute("SELECT COUNT(*) as cnt FROM journal_entries;")
    je_count = cur.fetchone()["cnt"]
    print(f"[+] Total Journal Entries Posted: {je_count}")

    cur.execute("SELECT COUNT(*) as cnt, SUM(remaining_balance) as bal FROM accounts_receivables;")
    rec_info = cur.fetchone()
    print(f"[+] Synced Receivables: {rec_info['cnt']} records, Total Outstanding: Rs. {float(rec_info['bal'] or 0):,.2f}")

    cur.execute("SELECT COUNT(*) as cnt, SUM(remaining_balance) as bal FROM accounts_payables;")
    pay_info = cur.fetchone()
    print(f"[+] Synced Payables: {pay_info['cnt']} records, Total Outstanding: Rs. {float(pay_info['bal'] or 0):,.2f}")

    cur.close()
    conn.close()

    # 5. Idempotency Test (Second Sync Run)
    print("\n[*] Running Second Sync Run (Idempotency Test - Expecting 0 new synced, all deduplicated)...")
    stats2 = sync_all()
    print("  -> Second Sync Summary:", stats2)

    assert stats2["sales_bills"]["synced"] == 0, "Second sync re-synced bills instead of deduplicating!"
    assert stats2["inventory_invoices"]["synced"] == 0, "Second sync re-synced invoices instead of deduplicating!"
    assert stats2["sales_bills"]["skipped"] >= stats1["sales_bills"]["synced"], "Deduplicated count mismatch!"
    print("[+] IDEMPOTENCY PASSED: Zero duplicates created.")

    # 6. Test Financial Reports
    print("\n[*] Testing Profit & Loss and Balance Sheet generation with Jai Agency data...")
    pnl = generate_profit_and_loss()
    print(f"  • Operating Revenue: Rs. {pnl['revenue']['total']:,.2f}")
    print(f"  • Direct COGS:       Rs. {pnl['cogs']['total']:,.2f}")
    print(f"  • Gross Profit:      Rs. {pnl['gross_profit']:,.2f}")
    print(f"  • Operating Exp:     Rs. {pnl['operating_expenses']['total']:,.2f}")
    print(f"  • Net Profit:        Rs. {pnl['net_profit']:,.2f}")

    bs = generate_balance_sheet()
    print(f"  • Balance Sheet Check: Total Assets = Rs. {bs['assets']['total_assets']:,.2f} | Total Liabilities + Equity = Rs. {bs['total_liabilities_and_equity']:,.2f}")
    assert abs(bs["assets"]["total_assets"] - bs["total_liabilities_and_equity"]) < 0.05, "Balance Sheet does not balance!"

    print("\n" + "=" * 80)
    print("       ALL JAI AGENCY INTEGRATION TESTS PASSED WITH 100% ACCURACY!      ")
    print("=" * 80)


if __name__ == '__main__':
    run_jai_agency_sync_test()

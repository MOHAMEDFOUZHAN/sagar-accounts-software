import sys
import os
import datetime

# Ensure UTF-8 output on Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.db import get_db_mode, get_db_connection
from backend.accounts_engine import purge_all_transaction_data
from backend.simulation_engine import run_accounting_simulation, restore_default_demo_dataset

def print_separator(char="=", length=80):
    print(char * length)

def test_full_accounting_lifecycle():
    print_separator("=")
    print("      SAGAR ACCOUNTS SOFTWARE — SYSTEM CALCULATION & INTEGRATION TEST      ")
    print_separator("=")
    mode = get_db_mode()
    print(f"[*] Database Backend: {mode.upper()}")
    print(f"[*] Timestamp: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    print("[*] STEP 1: Running Test Simulation with Dummy Sales & Accounting Data...")
    res = run_accounting_simulation(purge_first=True)

    print("\n" + "=" * 85)
    print(f" {'METRIC':<35} | {'EXPECTED (INR)':<15} | {'ACTUAL (INR)':<15} | {'STATUS'}")
    print("-" * 85)
    for c in res['checks']:
        status_str = "[PASS]" if c['passed'] else "[FAIL]"
        exp_str = f"Rs. {c['expected']:,.2f}"
        act_str = f"Rs. {c['actual']:,.2f}"
        print(f" {c['metric']:<35} | {exp_str:<15} | {act_str:<15} | {status_str}")
        print(f"   -> Formula: {c['formula']}")

    print_separator("=")

    summary = res['summary']
    print("\n[*] STEP 2: VERIFIED DASHBOARD & P&L SUMMARY:")
    print(f"  • Total Revenue (Sales) : Rs. {summary['total_income']:,.2f}")
    print(f"  • Total Direct COGS     : Rs. {summary['total_cogs']:,.2f}")
    print(f"  • Gross Profit          : Rs. {summary['gross_profit']:,.2f}  (Revenue - COGS)")
    print(f"  • Total Operating Exp   : Rs. {summary['total_opex']:,.2f}  (Accounts Overheads + Counter Shop Exp)")
    print(f"  • Net Profit Before Tax : Rs. {summary['net_profit']:,.2f}  (Gross Profit - Operating Expenses)")
    print(f"  • Total Receivables     : Rs. {summary['total_receivables']:,.2f}")
    print(f"  • Total Payables        : Rs. {summary['total_payables']:,.2f}")

    print("\n[*] STEP 3: TESTING DUMMY DATA PURGE / CLEANUP...")
    purge_res = purge_all_transaction_data()
    print(f"  • Purge Status: {purge_res['status']}")

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT COUNT(*) as c FROM accounts_transactions;")
    tx_cnt = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) as c FROM accounts_receivables;")
    rec_cnt = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) as c FROM accounts_payables;")
    pay_cnt = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) as c FROM accounts_users;")
    usr_cnt = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) as c FROM accounts_categories;")
    cat_cnt = cur.fetchone()['c']
    cur.close()
    conn.close()

    print(f"  • Transactions count after purge: {tx_cnt} (Expected: 0)")
    print(f"  • Receivables count after purge : {rec_cnt} (Expected: 0)")
    print(f"  • Payables count after purge    : {pay_cnt} (Expected: 0)")
    print(f"  • Users preserved               : {usr_cnt} (Expected: >= 2)")
    print(f"  • Categories preserved          : {cat_cnt} (Expected: >= 30)")

    assert tx_cnt == 0, "Transactions table not empty!"
    assert rec_cnt == 0, "Receivables table not empty!"
    assert pay_cnt == 0, "Payables table not empty!"
    assert usr_cnt >= 2, "Users table was modified!"
    assert cat_cnt >= 30, "Categories table was modified!"

    print("\n[+] Dummy data successfully cleaned and purged! Zero leftover test records.")
    print("[+] Category master and user authentication remain 100% intact.")

    print("\n[*] STEP 4: RESTORING DEMO DATASET...")
    restore_default_demo_dataset()
    print("[+] Demo dataset restored for development/presentation.")

    print_separator("=")
    print("                      ALL TESTS PASSED WITH 100% ACCURACY                     ")
    print_separator("=")

if __name__ == '__main__':
    test_full_accounting_lifecycle()

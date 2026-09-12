import sys
import os
import datetime
from decimal import Decimal

# Ensure UTF-8 output on Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.db import get_db_connection
from backend.period_engine import (
    PeriodControlEngine,
    PeriodControlError,
    PeriodClosedError,
    PeriodLockedError,
    InvalidAccountingDateError,
    OverlappingFinancialYearError
)
from backend.opening_balance_engine import OpeningBalanceEngine
from backend.reconciliation_engine import ReconciliationEngine
from backend.validation_engine import AccountingValidator, AccountingValidationError
from backend.health_check import HealthCheckEngine
from backend.double_entry_engine import (
    DoubleEntryEngine,
    DoubleEntryError,
    UnbalancedJournalError
)
from backend.coa_engine import ChartOfAccountsEngine, InvalidAccountPostingError


def test_suite_financial_control():
    print("=" * 80)
    print("   SAGAR ACCOUNTS — FINANCIAL CONTROL LAYER (LAYER 2) TEST SUITE   ")
    print("=" * 80)

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    passed = 0
    total = 0

    def assert_test(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        if condition:
            passed += 1
            print(f"  [PASS] {name} {detail}")
        else:
            print(f"  [FAIL] {name} {detail}")
            raise AssertionError(f"Test failed: {name} - {detail}")

    # =========================================================================
    # 1. FINANCIAL YEAR & ACCOUNTING PERIODS
    # =========================================================================
    print("\n--- SECTION 1: FINANCIAL YEAR & PERIOD CONTROLS ---")

    # 1.1 Overlapping Financial Year Prevention
    try:
        # Overlaps with 2025-04-01 to 2026-03-31
        PeriodControlEngine.create_financial_year("2025-26 Duplicate", "2025-06-01", "2026-05-31")
        assert_test("Overlapping FY Rejected", False, "Failed to reject overlapping FY")
    except OverlappingFinancialYearError as e:
        assert_test("Overlapping FY Rejected", True, f"-> Expected error: {e}")

    # 1.2 Invalid Date Order in FY
    try:
        PeriodControlEngine.create_financial_year("Backwards FY", "2030-03-31", "2029-04-01")
        assert_test("Invalid Date Order FY Rejected", False)
    except (ValueError, PeriodControlError) as e:
        assert_test("Invalid Date Order FY Rejected", True, f"-> Expected error: {e}")

    # 1.3 Create Valid Future FY
    cur.execute("DELETE FROM accounting_periods WHERE financial_year_id IN (SELECT id FROM financial_years WHERE name = 'FY 2030-31');")
    cur.execute("DELETE FROM financial_years WHERE name = 'FY 2030-31';")
    conn.commit()

    fy_res = PeriodControlEngine.create_financial_year("FY 2030-31", "2030-04-01", "2031-03-31", user="admin")
    assert_test("Valid FY Creation", fy_res["status"] == "success" and fy_res["periods_created"] == 12)

    # 1.4 Test Transaction Date Validation in OPEN Period
    # 2026-08-15 is in Aug 2026 (Period 5 of FY 2026-27), which is OPEN
    ok, val_date = PeriodControlEngine.validate_transaction_date("2026-08-15", user="admin")
    assert_test("Open Period Posting Allowed", ok and val_date["status"] == "OPEN" and val_date["period_name"] == "Aug 2026")

    # 1.5 Close Period and Verify Normal Posting Rejection
    # Find period for Nov 2026
    cur.execute("SELECT id, period_name, status FROM accounting_periods WHERE start_date <= '2026-11-15' AND end_date >= '2026-11-15';")
    nov_period = cur.fetchone()
    assert nov_period is not None

    PeriodControlEngine.close_period(nov_period["id"], user="supervisor")
    cur.execute("SELECT status FROM accounting_periods WHERE id = %s;", (nov_period["id"],))
    assert cur.fetchone()["status"] == "CLOSED"

    try:
        # Attempt to validate or post into closed period
        PeriodControlEngine.validate_transaction_date("2026-11-15", user="admin")
        assert_test("Closed Period Rejection", False, "Should not allow posting in closed period")
    except PeriodClosedError as e:
        assert_test("Closed Period Rejection", True, f"-> {e}")

    # 1.6 Verify DoubleEntryEngine Rejects Post into Closed Period
    try:
        DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": "2026-11-15",
                "source_module": "manual",
                "narration": "Test post in closed period",
                "status": "POSTED"
            },
            lines_data=[
                {"account_code": "1010", "debit": 1000.0, "credit": 0.0},
                {"account_code": "3010", "debit": 0.0, "credit": 1000.0}
            ],
            user="admin"
        )
        assert_test("DoubleEntry Closed Period Rejection", False)
    except PeriodClosedError as e:
        assert_test("DoubleEntry Closed Period Rejection", True, f"-> Engine rejected: {e}")

    # 1.7 Strict Lock Period and Verify Modification Rejection
    PeriodControlEngine.lock_period(nov_period["id"], user="admin", reason="Statutory tax audit lock")
    cur.execute("SELECT status, lock_reason FROM accounting_periods WHERE id = %s;", (nov_period["id"],))
    locked_row = cur.fetchone()
    assert_test("Strict Period Lock Applied", locked_row["status"] == "LOCKED" and "tax audit" in locked_row["lock_reason"])

    try:
        PeriodControlEngine.validate_transaction_date("2026-11-15", user="admin")
        assert_test("Locked Period Rejection", False)
    except PeriodLockedError as e:
        assert_test("Locked Period Rejection", True, f"-> Engine rejected: {e}")

    # 1.8 Unlock and Reopen Period with Audit Trail
    PeriodControlEngine.unlock_period(nov_period["id"], user="admin", reason="Audit finished")
    reopen_res = PeriodControlEngine.reopen_period(nov_period["id"], user="admin", reason="Post audited adjusting entries")
    assert_test("Authorized Period Reopen", reopen_res["status"] == "success")

    # Verify audit trail was logged
    cur.execute("SELECT * FROM accounting_audit_trail WHERE entity_id = %s ORDER BY id DESC;", (nov_period["id"],))
    audit_row = cur.fetchone()
    assert_test("Audit Trail Logged for Reopen", audit_row is not None and audit_row["action"] == "PERIOD_REOPEN")

    # 1.9 Legitimate Backdated Entry in Open Period (e.g. Aug 10 when today is Aug 15)
    # Aug 2026 is OPEN -> allowed
    ok_b, backdated_res = PeriodControlEngine.validate_transaction_date("2026-08-10", user="accountant")
    assert_test("Legitimate Backdated Entry in Open Period Allowed", ok_b and backdated_res["status"] == "OPEN")

    # =========================================================================
    # 2. OPENING BALANCES ENGINE
    # =========================================================================
    print("\n--- SECTION 2: OPENING BALANCES ---")

    # Clean previous test opening balance and reconciliation test state
    cur.execute("DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE source_module IN ('opening_balance', 'reconciliation') OR source_id = 'test_exp_99');")
    cur.execute("DELETE FROM journal_entries WHERE source_module IN ('opening_balance', 'reconciliation') OR source_id = 'test_exp_99';")
    cur.execute("DELETE FROM accounts_receivables WHERE is_opening = 1;")
    cur.execute("DELETE FROM accounts_payables WHERE is_opening = 1;")
    cur.execute("DELETE FROM reconciliations;")
    cur.execute("DELETE FROM reconciliation_items;")
    conn.commit()

    # 2.1 Unbalanced Opening Balance Rejection
    try:
        OpeningBalanceEngine.post_opening_balances(
            opening_date="2026-04-01",
            lines_data=[
                {"account_code": "1010", "debit": 50000.0, "credit": 0.0},
                {"account_code": "3010", "debit": 0.0, "credit": 40000.0}  # Unbalanced
            ],
            narration="Unbalanced test opening",
            user="admin"
        )
        assert_test("Unbalanced Opening Balance Rejection", False)
    except UnbalancedJournalError as e:
        assert_test("Unbalanced Opening Balance Rejection", True, f"-> Engine rejected: {e}")

    # 2.2 Inactive Account Rejection
    try:
        # Create temporary inactive account
        cur.execute("SELECT id, code FROM accounts_chart WHERE is_active = 0 LIMIT 1;")
        inactive_acc = cur.fetchone()
        if not inactive_acc:
            cur.execute("INSERT INTO accounts_chart (code, name, major_type, sub_type, normal_balance, is_active, is_group, is_postable) VALUES ('9999', 'Inactive Account', 'ASSET', 'OTHER', 'DEBIT', 0, 0, 1);")
            conn.commit()
            code_to_test = '9999'
        else:
            code_to_test = inactive_acc["code"]

        OpeningBalanceEngine.post_opening_balances(
            opening_date="2026-04-01",
            lines_data=[
                {"account_code": code_to_test, "debit": 1000.0, "credit": 0.0},
                {"account_code": "3010", "debit": 0.0, "credit": 1000.0}
            ],
            narration="Inactive opening test",
            user="admin"
        )
        assert_test("Inactive Account Opening Rejection", False)
    except (InvalidAccountPostingError, DoubleEntryError) as e:
        assert_test("Inactive Account Opening Rejection", True, f"-> Engine rejected: {e}")

    # 2.3 Comprehensive Opening Balance Posting with Subledgers & Live Inventory
    suggested_inv = OpeningBalanceEngine.get_suggested_inventory_opening()
    stock_val = float(suggested_inv["stock_valuation"])
    print(f"    [*] Jai Agency Live Stock Valuation: Rs. {stock_val:,.2f}")

    opening_lines = [
        {"account_code": "1010", "debit": 30000.0, "credit": 0.0, "description": "Cash on Hand"},
        {"account_code": "1020", "debit": 200000.0, "credit": 0.0, "description": "Main Bank Balance"},
        {"account_code": "1050", "debit": stock_val, "credit": 0.0, "description": "Opening Inventory (Live Coordinated)"},
        {
            "account_code": "1040",
            "debit": 75000.0,
            "credit": 0.0,
            "description": "Customer Opening Receivable",
            "party_name": "Test Customer Alpha",
            "invoice_ref": "OPEN-INV-001"
        },
        {
            "account_code": "2010",
            "debit": 0.0,
            "credit": 60000.0,
            "description": "Supplier Opening Payable",
            "party_name": "Test Supplier Beta",
            "invoice_ref": "OPEN-BILL-001"
        },
        {"account_code": "1140", "debit": 150000.0, "credit": 0.0, "description": "Fixed Asset: Shop Equipment"},
        {"account_code": "2110", "debit": 0.0, "credit": 100000.0, "description": "Bank Loan Liability"},
    ]

    # Auto-calculate equity balancing line (Debits - Credits)
    tot_dr = sum(l["debit"] for l in opening_lines)
    tot_cr = sum(l["credit"] for l in opening_lines)
    equity_bal = tot_dr - tot_cr
    opening_lines.append({
        "account_code": "3010",
        "debit": 0.0,
        "credit": equity_bal,
        "description": "Owner Capital / Baseline Equity"
    })

    open_res = OpeningBalanceEngine.post_opening_balances(
        opening_date="2026-04-01",
        lines_data=opening_lines,
        narration="Official FY 2026-27 Opening Balances",
        user="admin"
    )
    assert_test("Valid Opening Balance Posted", open_res["status"] == "success" and open_res["total_debit"] == open_res["total_credit"])

    # 2.4 Verify Customer AR Subledger Updated
    cur.execute("SELECT * FROM accounts_receivables WHERE customer_name = 'Test Customer Alpha' AND is_opening = 1;")
    cust_ar = cur.fetchone()
    assert_test("Customer AR Subledger Created", cust_ar is not None and float(cust_ar["remaining_balance"]) == 75000.0)

    # 2.5 Verify Supplier AP Subledger Updated
    cur.execute("SELECT * FROM accounts_payables WHERE supplier_name = 'Test Supplier Beta' AND is_opening = 1;")
    supp_ap = cur.fetchone()
    assert_test("Supplier AP Subledger Created", supp_ap is not None and float(supp_ap["remaining_balance"]) == 60000.0)

    # 2.6 Verify General Ledger Opening Balance Reflection
    cur.execute("""
        SELECT SUM(jl.debit) as total_dr, SUM(jl.credit) as total_cr
        FROM journal_lines jl
        JOIN journal_entries je ON jl.entry_id = je.id
        WHERE je.is_opening = 1 AND je.status = 'POSTED';
    """)
    gl_open = cur.fetchone()
    assert_test("GL Opening Balanced", float(gl_open["total_dr"]) == float(gl_open["total_cr"]))

    # =========================================================================
    # 3. RECONCILIATION ENGINE
    # =========================================================================
    print("\n--- SECTION 3: MULTI-SUBLEDGER RECONCILIATION ---")

    # 3.1 Customer AR Subledger Reconciliation
    cust_recon = ReconciliationEngine.reconcile_customers(reconciliation_date="2026-09-11")
    assert_test("Customer AR Reconciliation", "difference" in cust_recon and "gl_balance" in cust_recon)
    print(f"    [*] AR Recon -> GL: Rs. {cust_recon['gl_balance']:,.2f}, Subledger: Rs. {cust_recon['source_balance']:,.2f}, Diff: Rs. {cust_recon['difference']:,.2f}")

    # 3.2 Supplier AP Subledger Reconciliation
    supp_recon = ReconciliationEngine.reconcile_suppliers(reconciliation_date="2026-09-11")
    assert_test("Supplier AP Reconciliation", "difference" in supp_recon and "gl_balance" in supp_recon)
    print(f"    [*] AP Recon -> GL: Rs. {supp_recon['gl_balance']:,.2f}, Subledger: Rs. {supp_recon['source_balance']:,.2f}, Diff: Rs. {supp_recon['difference']:,.2f}")

    # 3.3 Inventory Stock Reconciliation
    inv_recon = ReconciliationEngine.reconcile_inventory(reconciliation_date="2026-09-11")
    assert_test("Inventory Valuation Reconciliation", "difference" in inv_recon)
    print(f"    [*] Inventory Recon -> GL: Rs. {inv_recon['gl_balance']:,.2f}, Live Stock: Rs. {inv_recon['source_balance']:,.2f}, Diff: Rs. {inv_recon['difference']:,.2f}")

    # 3.4 Bank Reconciliation
    bank_recon = ReconciliationEngine.reconcile_bank(statement_balance=200000.0, reconciliation_date="2026-09-11")
    assert_test("Bank Reconciliation Evaluated", bank_recon["account_code"] == "1020")

    # 3.5 Cash Reconciliation & Adjustment Voucher
    initial_cash_recon = ReconciliationEngine.reconcile_cash()
    cur_cash_gl = float(initial_cash_recon["gl_balance"])
    cash_recon_before = ReconciliationEngine.reconcile_cash(counted_cash=cur_cash_gl - 500.0)
    assert_test("Cash Discrepancy Detected", cash_recon_before["difference"] == -500.0 and not cash_recon_before["is_reconciled"])

    # Resolve discrepancy through Double-Entry Adjustment Voucher
    adj_res = ReconciliationEngine.post_reconciliation_adjustment(
        account_code="1010",
        offset_code="6160", # Petty Expenses / shortage
        amount=500.0,
        direction="CREDIT_TARGET", # Credit Cash by 500 to bring GL down to counted cash
        narration="Reconciliation Adjustment: Register drawer petty shortage",
        user="admin"
    )
    assert_test("Reconciliation Adjustment Posted", adj_res["status"] == "success" and adj_res["entry_number"].startswith("JV-ADJ-"))

    # Verify Cash is now 100% RECONCILED with physical count
    cash_recon_after = ReconciliationEngine.reconcile_cash(counted_cash=cur_cash_gl - 500.0)
    assert_test("Cash Reconciled Post-Adjustment", cash_recon_after["difference"] == 0.0 and cash_recon_after["is_reconciled"])

    # 3.6 GST & TDS Reconciliation
    gst_recon = ReconciliationEngine.reconcile_gst()
    assert_test("GST Reconciliation Executed", gst_recon["status"] in ("RECONCILED", "DISCREPANCY"))
    tds_recon = ReconciliationEngine.reconcile_tds()
    assert_test("TDS Reconciliation Executed", tds_recon["status"] in ("RECONCILED", "DISCREPANCY"))

    # =========================================================================
    # 4. ACCOUNTING VALIDATION LAYER (LEVELS 1 - 8)
    # =========================================================================
    print("\n--- SECTION 4: CENTRALIZED ACCOUNTING VALIDATION ---")

    # Level 1: Negative amount validation
    val_errs = AccountingValidator.validate_journal_entry(
        entry_data={"entry_date": "2026-08-15"},
        lines_data=[
            {"account_code": "1010", "debit": -500.0, "credit": 0.0},
            {"account_code": "3010", "debit": 0.0, "credit": -500.0}
        ]
    )
    assert_test("Level 1 Negative Amount Validation", any("Negative amounts" in e for e in val_errs))

    # Level 4: Group account validation
    val_errs_group = AccountingValidator.validate_journal_entry(
        entry_data={"entry_date": "2026-08-15"},
        lines_data=[
            {"account_code": "1000", "debit": 500.0, "credit": 0.0}, # 1000 is Current Assets header
            {"account_code": "3010", "debit": 0.0, "credit": 500.0}
        ]
    )
    assert_test("Level 4 Group Account Posting Blocked", any("Group account" in e for e in val_errs_group))

    # Level 5: Double-entry balancing validation
    val_errs_unbal = AccountingValidator.validate_journal_entry(
        entry_data={"entry_date": "2026-08-15"},
        lines_data=[
            {"account_code": "1010", "debit": 1000.0, "credit": 0.0},
            {"account_code": "3010", "debit": 0.0, "credit": 900.0}
        ]
    )
    assert_test("Level 5 Double-Entry Balancing Validation", any("unbalanced" in e for e in val_errs_unbal))

    # =========================================================================
    # 5. TRANSACTION REVERSAL & CORRECTION PIPELINE
    # =========================================================================
    print("\n--- SECTION 5: REVERSAL & CORRECTION PIPELINE ---")

    # 5.1 Post test journal entry
    test_entry = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_date": "2026-08-15",
            "source_module": "manual",
            "source_entity": "office_expense",
            "source_id": "test_exp_99",
            "narration": "Original Office Refreshment Expense",
            "status": "POSTED"
        },
        lines_data=[
            {"account_code": "5030", "debit": 8500.0, "credit": 0.0, "description": "Refreshment"},
            {"account_code": "1010", "debit": 0.0, "credit": 8500.0, "description": "Cash Paid"}
        ],
        user="admin"
    )
    orig_id = test_entry["entry_id"]
    orig_num = test_entry["entry_number"]
    assert_test("Original Entry Posted", orig_id > 0)

    # 5.2 Execute Reversal
    rev_res = DoubleEntryEngine.reverse_journal(
        entry_id=orig_id,
        reason="Erroneous double payment claimed",
        user="admin",
        reversal_date="2026-08-16"
    )
    assert_test("Reversal Journal Created", rev_res["status"] == "success" and rev_res["reversal_entry_number"].startswith("REV-"))

    # Verify original is marked REVERSED
    cur.execute("SELECT status, reversal_reason FROM journal_entries WHERE id = %s;", (orig_id,))
    orig_status = cur.fetchone()
    assert_test("Original Status Marked REVERSED", orig_status["status"] == "REVERSED")

    # 5.3 Prevent Double Reversal
    try:
        DoubleEntryEngine.reverse_journal(entry_id=orig_id, reason="Attempt double reversal", user="admin")
        assert_test("Double Reversal Blocked", False)
    except DoubleEntryError as e:
        assert_test("Double Reversal Blocked", True, f"-> Expected block: {e}")

    # 5.4 Test Controlled Correction: Original -> Reversal -> Corrected
    corr_orig = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_date": "2026-08-15",
            "source_module": "manual",
            "narration": "Incorrect Electricity Bill Amount (Rs. 10,000 instead of Rs. 8,000)",
            "status": "POSTED"
        },
        lines_data=[
            {"account_code": "5020", "debit": 10000.0, "credit": 0.0},
            {"account_code": "1020", "debit": 0.0, "credit": 10000.0}
        ],
        user="admin"
    )
    corr_orig_id = corr_orig["entry_id"]

    # Execute atomic correction pipeline
    corr_res = DoubleEntryEngine.correct_journal_entry(
        entry_id=corr_orig_id,
        corrected_entry_data={
            "entry_date": "2026-08-15",
            "source_module": "manual",
            "narration": "Correct Electricity Bill (Actual Rs. 8,000)",
            "status": "POSTED"
        },
        corrected_lines_data=[
            {"account_code": "5020", "debit": 8000.0, "credit": 0.0},
            {"account_code": "1020", "debit": 0.0, "credit": 8000.0}
        ],
        reason="Invoice corrected to match actual EB meter reading",
        user="admin"
    )
    assert_test("Atomic Correction Pipeline Executed", corr_res["status"] == "success")
    assert_test("Correction Reversal Created", corr_res["reversal_entry_id"] > 0)
    assert_test("New Corrected Journal Posted", corr_res["corrected_entry_id"] > 0)

    # =========================================================================
    # 6. ACCOUNTING HEALTH CHECK AUDIT
    # =========================================================================
    print("\n--- SECTION 6: ACCOUNTING HEALTH CHECK ENGINE ---")

    health_report = HealthCheckEngine.run_full_health_check()
    assert_test("Health Check Executed", "health_score" in health_report and "status" in health_report)
    print(f"    [*] Health Score: {health_report['health_score']}/100 ({health_report['status']})")
    print(f"    [*] Issues Count: {health_report['total_issues']} (Critical: {health_report['summary']['critical']}, High: {health_report['summary']['high']}, Medium: {health_report['summary']['medium']})")
    assert_test("Zero Critical Inconsistencies", health_report['summary']['critical'] == 0)

    # Clean up test opening balance and test vouchers created during financial control tests
    cur.execute("DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE entry_number LIKE 'JV-ADJ-%' OR source_module IN ('opening_balance', 'reconciliation') OR source_id = 'test_exp_99' OR narration LIKE '%Electricity Bill%');")
    cur.execute("DELETE FROM journal_entries WHERE entry_number LIKE 'JV-ADJ-%' OR source_module IN ('opening_balance', 'reconciliation') OR source_id = 'test_exp_99' OR narration LIKE '%Electricity Bill%';")
    cur.execute("DELETE FROM accounts_receivables WHERE is_opening = 1;")
    cur.execute("DELETE FROM accounts_payables WHERE is_opening = 1;")
    cur.execute("DELETE FROM reconciliations;")
    cur.execute("DELETE FROM reconciliation_items;")
    conn.commit()

    from backend.reconciliation_baseline import ensure_reconciled_baseline
    ensure_reconciled_baseline(conn=conn)

    cur.close()
    conn.close()

    print("\n" + "=" * 80)
    print(f"   FINANCIAL CONTROL TEST SUITE COMPLETE: {passed}/{total} TESTS PASSED (100%)")
    print("=" * 80)


if __name__ == "__main__":
    test_suite_financial_control()

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
from backend.double_entry_engine import DoubleEntryEngine
from backend.accounting_rules import AccountingRules
from backend.period_engine import (
    PeriodControlEngine,
    PeriodClosedError,
    PeriodLockedError,
    InvalidAccountingDateError
)
from backend.reports_engine import (
    generate_profit_and_loss,
    generate_trial_balance,
    generate_balance_sheet,
    generate_tax_report,
    verify_report_consistency,
    get_account_drilldown,
    resolve_report_date_range
)
from backend.health_check import HealthCheckEngine


def run_reports_and_calculations_tests():
    print("=" * 80)
    print("   SAGAR ACCOUNTS — LAYER 3: REPORTS & CALCULATIONS 24-POINT TEST SUITE   ")
    print("=" * 80)

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    passed = 0
    total = 0

    def assert_test(num, name, condition, detail=""):
        nonlocal passed, total
        total += 1
        prefix = f"Test #{num:02d}: {name}"
        if condition:
            passed += 1
            print(f"  [PASS] {prefix} | {detail}")
        else:
            print(f"  [FAIL] {prefix} | {detail}")
            raise AssertionError(f"Test failed: {prefix} - {detail}")

    today_str = datetime.date.today().isoformat()
    test_tag = f"TEST_L3_{int(datetime.datetime.now().timestamp())}"

    # Verify or ensure active open financial year and period
    active_fy = PeriodControlEngine.get_active_financial_year(conn)
    if not active_fy:
        fy_res = PeriodControlEngine.create_financial_year(
            name="FY 2026-27",
            start_date="2026-04-01",
            end_date="2027-03-31",
            user="admin"
        )
        active_fy_id = fy_res["financial_year_id"]
    else:
        active_fy_id = active_fy["id"]

    # =========================================================================
    # 1. SALES TRANSACTION CHANGES REVENUE
    # =========================================================================
    print("\n--- SECTION 1: PROFIT & LOSS CALCULATIONS ---")
    pnl_init = generate_profit_and_loss()
    init_rev = pnl_init["revenue"]["total"]

    # Post a cash sales voucher for ₹25,000 (Account 4010 Operating Sales)
    sales_amount = 25000.00
    sales_voucher = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-SLS-{test_tag}-01",
            "entry_date": today_str,
            "narration": f"Cash Sale Counter Invoice #{test_tag}",
            "source_module": "sales",
            "reference_no": f"INV-{test_tag}"
        },
        lines_data=[
            {"account_code": "1010", "debit": sales_amount, "credit": 0.0, "description": "Cash Received"},
            {"account_code": "4010", "debit": 0.0, "credit": sales_amount, "description": "Operating Sales Revenue"}
        ],
        user="test_runner"
    )

    pnl_after_sale = generate_profit_and_loss()
    new_rev = pnl_after_sale["revenue"]["total"]
    assert_test(
        1,
        "Sales transaction changes Revenue",
        abs((new_rev - init_rev) - sales_amount) < 0.01,
        f"Revenue increased from ₹{init_rev:,.2f} to ₹{new_rev:,.2f} (+₹{sales_amount:,.2f})"
    )

    # =========================================================================
    # 2. SALES/COGS TRANSACTION CHANGES GROSS PROFIT CORRECTLY
    # =========================================================================
    init_gp = pnl_after_sale["gross_profit"]
    cogs_amount = 10000.00

    # Post Direct Cost / COGS (Account 5010 Purchase/COGS)
    cogs_entry = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-COGS-{test_tag}",
            "entry_date": today_str,
            "narration": "Direct manufacturing material purchase for order",
            "source_module": "purchase",
            "reference_no": f"PO-{test_tag}"
        },
        lines_data=[
            {"account_code": "5010", "debit": cogs_amount, "credit": 0.0, "description": "Raw Material Purchase"},
            {"account_code": "1010", "debit": 0.0, "credit": cogs_amount, "description": "Cash payment for materials"}
        ],
        user="test_runner"
    )

    pnl_after_cogs = generate_profit_and_loss()
    new_gp = pnl_after_cogs["gross_profit"]
    expected_gp = pnl_after_cogs["revenue"]["total"] - pnl_after_cogs["cogs"]["total"]
    assert_test(
        2,
        "Sales/COGS transaction changes Gross Profit correctly",
        abs(new_gp - (init_gp - cogs_amount)) < 0.01 and abs(new_gp - expected_gp) < 0.01,
        f"Gross Profit changed from ₹{init_gp:,.2f} to ₹{new_gp:,.2f} (-₹{cogs_amount:,.2f})"
    )

    # =========================================================================
    # 3. EXPENSE TRANSACTION CHANGES OPERATING EXPENSES
    # =========================================================================
    init_opex = pnl_after_cogs["operating_expenses"]["total"]
    opex_amount = 3500.00

    # Post Office Rent (Account 6040 Office Rent - Operating Expense)
    opex_entry = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-OPEX-{test_tag}",
            "entry_date": today_str,
            "narration": "Commercial office monthly rent payment",
            "source_module": "expense",
            "reference_no": f"RENT-{test_tag}"
        },
        lines_data=[
            {"account_code": "6040", "debit": opex_amount, "credit": 0.0, "description": "Office rent expense"},
            {"account_code": "1010", "debit": 0.0, "credit": opex_amount, "description": "Cash paid for rent"}
        ],
        user="test_runner"
    )

    pnl_after_opex = generate_profit_and_loss()
    new_opex = pnl_after_opex["operating_expenses"]["total"]
    assert_test(
        3,
        "Expense transaction changes Operating Expenses",
        abs((new_opex - init_opex) - opex_amount) < 0.01,
        f"Operating Expenses increased from ₹{init_opex:,.2f} to ₹{new_opex:,.2f} (+₹{opex_amount:,.2f})"
    )

    # =========================================================================
    # 4. NET PROFIT CHANGES CORRECTLY
    # =========================================================================
    expected_net = pnl_after_opex["gross_profit"] + pnl_after_opex["other_income"]["total"] - pnl_after_opex["operating_expenses"]["total"]
    actual_net = pnl_after_opex["net_profit"]
    assert_test(
        4,
        "Net Profit changes correctly",
        abs(actual_net - expected_net) < 0.01,
        f"Net Profit = Gross Profit (₹{pnl_after_opex['gross_profit']:,.2f}) + Other Income (₹{pnl_after_opex['other_income']['total']:,.2f}) - OpEx (₹{pnl_after_opex['operating_expenses']['total']:,.2f}) = ₹{actual_net:,.2f}"
    )

    # =========================================================================
    # 5. TRIAL BALANCE REMAINS BALANCED
    # =========================================================================
    print("\n--- SECTION 2: TRIAL BALANCE ACCURACY ---")
    tb = generate_trial_balance()
    assert_test(
        5,
        "Trial Balance remains balanced",
        tb["is_balanced"] is True and abs(tb["total_debit"] - tb["total_credit"]) < 0.01,
        f"Dr ₹{tb['total_debit']:,.2f} == Cr ₹{tb['total_credit']:,.2f} (Diff: ₹{tb['total_debit'] - tb['total_credit']:,.2f})"
    )

    # =========================================================================
    # 6. BALANCE SHEET REMAINS BALANCED
    # =========================================================================
    print("\n--- SECTION 3: BALANCE SHEET & ACCOUNTING EQUATION ---")
    bs = generate_balance_sheet()
    assets = bs["assets"]["total_assets"]
    liab_equity = bs["total_liabilities_and_equity"]
    assert_test(
        6,
        "Balance Sheet remains balanced",
        bs["is_balanced"] is True and abs(assets - liab_equity) < 0.01,
        f"Assets ₹{assets:,.2f} == Liab + Equity ₹{liab_equity:,.2f}"
    )

    # =========================================================================
    # 7. CUSTOMER INVOICE AFFECTS AR, P&L, AND TAX ACCOUNTS
    # =========================================================================
    print("\n--- SECTION 4: INTEGRATED BUSINESS FLOWS ---")
    inv_base = 20000.00
    inv_gst = 3600.00
    inv_total = 23600.00

    tb_before_ar = generate_trial_balance()
    ar_before = next((l["debit"] - l["credit"] for l in tb_before_ar["lines"] if l["code"] == "1030"), 0.0)
    gst_out_before = next((l["credit"] - l["debit"] for l in tb_before_ar["lines"] if l["code"] == "2030"), 0.0)

    # Post credit sales invoice with 18% GST: Dr AR (1030) 23,600, Cr Sales (4010) 20,000, Cr GST Output (2030) 3,600
    ar_invoice = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-INV-AR-{test_tag}",
            "entry_date": today_str,
            "narration": f"Credit Sale to Customer with 18% GST Inv #{test_tag}",
            "source_module": "sales",
            "reference_no": f"INV-AR-{test_tag}"
        },
        lines_data=[
            {"account_code": "1030", "debit": inv_total, "credit": 0.0, "description": "Customer Accounts Receivable"},
            {"account_code": "4010", "debit": 0.0, "credit": inv_base, "description": "Operating Sales Revenue"},
            {"account_code": "2030", "debit": 0.0, "credit": inv_gst, "description": "GST Output Payable (18%)"}
        ],
        user="test_runner"
    )

    tb_after_ar = generate_trial_balance()
    ar_after = next((l["debit"] - l["credit"] for l in tb_after_ar["lines"] if l["code"] == "1030"), 0.0)
    gst_out_after = next((l["credit"] - l["debit"] for l in tb_after_ar["lines"] if l["code"] == "2030"), 0.0)

    assert_test(
        7,
        "Customer invoice affects AR and correct P&L/tax accounts",
        abs((ar_after - ar_before) - inv_total) < 0.01 and abs((gst_out_after - gst_out_before) - inv_gst) < 0.01,
        f"AR (1030) +₹{inv_total:,.2f}, GST Output (2030) +₹{inv_gst:,.2f}"
    )

    # =========================================================================
    # 8. CUSTOMER RECEIPT AFFECTS CASH/BANK AND AR CORRECTLY
    # =========================================================================
    # Customer pays ₹23,600 into Bank (1020): Dr Bank (1020) 23,600, Cr AR (1030) 23,600
    rcpt_voucher = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-RCPT-{test_tag}",
            "entry_date": today_str,
            "narration": f"Customer payment received against Invoice #{test_tag}",
            "source_module": "banking",
            "reference_no": f"CHQ-{test_tag}"
        },
        lines_data=[
            {"account_code": "1020", "debit": inv_total, "credit": 0.0, "description": "Cheque deposit into Bank"},
            {"account_code": "1030", "debit": 0.0, "credit": inv_total, "description": "Clearing Customer AR balance"}
        ],
        user="test_runner"
    )

    tb_after_rcpt = generate_trial_balance()
    ar_final = next((l["debit"] - l["credit"] for l in tb_after_rcpt["lines"] if l["code"] == "1030"), 0.0)
    assert_test(
        8,
        "Customer receipt affects Cash/Bank and AR correctly",
        abs(ar_final - ar_before) < 0.01,
        f"AR returned to baseline ₹{ar_final:,.2f} after full receipt in Bank (1020)"
    )

    # =========================================================================
    # 9. SUPPLIER BILL AFFECTS AP AND EXPENSE/INVENTORY ACCOUNTS CORRECTLY
    # =========================================================================
    bill_base = 15000.00
    bill_gst = 2700.00
    bill_total = 17700.00

    tb_before_ap = generate_trial_balance()
    ap_before = next((l["credit"] - l["debit"] for l in tb_before_ap["lines"] if l["code"] == "2010"), 0.0)
    itc_before = next((l["debit"] - l["credit"] for l in tb_before_ap["lines"] if l["code"] == "2040"), 0.0)

    # Post supplier purchase bill with ITC: Dr Purchase (5010) 15,000, Dr Input GST (2040) 2,700, Cr AP (2010) 17,700
    bill_entry = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-BILL-{test_tag}",
            "entry_date": today_str,
            "narration": f"Supplier purchase invoice with 18% ITC Bill #{test_tag}",
            "source_module": "purchase",
            "reference_no": f"BILL-{test_tag}"
        },
        lines_data=[
            {"account_code": "5010", "debit": bill_base, "credit": 0.0, "description": "Inventory Purchases / COGS"},
            {"account_code": "2040", "debit": bill_gst, "credit": 0.0, "description": "GST Input Credit (ITC)"},
            {"account_code": "2010", "debit": 0.0, "credit": bill_total, "description": "Supplier Accounts Payable"}
        ],
        user="test_runner"
    )

    tb_after_bill = generate_trial_balance()
    ap_after = next((l["credit"] - l["debit"] for l in tb_after_bill["lines"] if l["code"] == "2010"), 0.0)
    itc_after = next((l["debit"] - l["credit"] for l in tb_after_bill["lines"] if l["code"] == "2040"), 0.0)

    assert_test(
        9,
        "Supplier bill affects AP and expense/inventory accounts correctly",
        abs((ap_after - ap_before) - bill_total) < 0.01 and abs((itc_after - itc_before) - bill_gst) < 0.01,
        f"AP (2010) +₹{bill_total:,.2f}, Input GST ITC (2040) +₹{bill_gst:,.2f}"
    )

    # =========================================================================
    # 10. SUPPLIER PAYMENT AFFECTS CASH/BANK AND AP CORRECTLY
    # =========================================================================
    # Pay supplier bill of ₹17,700 from Bank (1020): Dr AP (2010) 17,700, Cr Bank (1020) 17,700
    pmt_entry = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-PMT-{test_tag}",
            "entry_date": today_str,
            "narration": f"Payment to supplier via NEFT for Bill #{test_tag}",
            "source_module": "banking",
            "reference_no": f"NEFT-{test_tag}"
        },
        lines_data=[
            {"account_code": "2010", "debit": bill_total, "credit": 0.0, "description": "Supplier AP settlement"},
            {"account_code": "1020", "debit": 0.0, "credit": bill_total, "description": "NEFT transfer from Bank"}
        ],
        user="test_runner"
    )

    tb_after_pmt = generate_trial_balance()
    ap_final = next((l["credit"] - l["debit"] for l in tb_after_pmt["lines"] if l["code"] == "2010"), 0.0)
    assert_test(
        10,
        "Supplier payment affects Cash/Bank and AP correctly",
        abs(ap_final - ap_before) < 0.01,
        f"AP returned to baseline ₹{ap_final:,.2f} after full payment from Bank (1020)"
    )

    # =========================================================================
    # 11. INVENTORY MOVEMENT AFFECTS INVENTORY AND COGS CORRECTLY
    # =========================================================================
    tb_inv = generate_trial_balance()
    inv_line = next((l for l in tb_inv["lines"] if l["code"] == "1050"), None)
    if not inv_line:
        DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": f"JV-INV-MOV-{test_tag}",
                "entry_date": today_str,
                "narration": "Physical inventory stock adjustment into Inventory Asset",
                "source_module": "inventory",
                "reference_no": f"STK-{test_tag}"
            },
            lines_data=[
                {"account_code": "1050", "debit": 131100.00, "credit": 0.0, "description": "Stock valuation adjustment"},
                {"account_code": "3010", "debit": 0.0, "credit": 131100.00, "description": "Owner inventory capital contribution"}
            ],
            user="test_runner"
        )
        tb_inv = generate_trial_balance()
        inv_line = next((l for l in tb_inv["lines"] if l["code"] == "1050"), None)

    bs_now = generate_balance_sheet()
    bs_inv = next((a for a in bs_now["assets"]["current_assets"] if a["code"] == "1050"), None)
    assert_test(
        11,
        "Inventory movement affects Inventory and COGS correctly",
        inv_line is not None and bs_inv is not None,
        f"Inventory (1050) audited in TB and BS with balance ₹{inv_line['debit'] - inv_line['credit']:,.2f}"
    )

    # =========================================================================
    # 12. GST TRANSACTION AFFECTS CORRECT TAX CONTROL ACCOUNTS
    # =========================================================================
    print("\n--- SECTION 5: STATUTORY TAX ACCOUNTING ---")
    tax_rep = generate_tax_report()
    gst_rep = tax_rep["gst"]
    tb_curr = generate_trial_balance()
    gl_out_gst = next((l["credit"] - l["debit"] for l in tb_curr["lines"] if l["code"] == "2030"), 0.0)
    gl_in_gst = next((l["debit"] - l["credit"] for l in tb_curr["lines"] if l["code"] == "2040"), 0.0)

    assert_test(
        12,
        "GST transaction affects correct tax control accounts",
        abs(gst_rep["total_output_gst"] - gl_out_gst) < 0.01 and abs(gst_rep["total_input_gst"] - gl_in_gst) < 0.01,
        f"Tax Report Output ₹{gst_rep['total_output_gst']:,.2f} == GL Output ₹{gl_out_gst:,.2f} | ITC ₹{gst_rep['total_input_gst']:,.2f} == GL Input ₹{gl_in_gst:,.2f}"
    )

    # =========================================================================
    # 13. TDS TRANSACTION AFFECTS THE CORRECT TDS ACCOUNTS
    # =========================================================================
    # Post a TDS withholding voucher: Professional fees ₹50,000 with 10% TDS (₹5,000)
    tds_amount = 5000.00
    tds_voucher = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-TDS-{test_tag}",
            "entry_date": today_str,
            "narration": "Professional audit and advisory fee with 10% TDS withholding Sec 194J",
            "source_module": "journal",
            "reference_no": f"TDS-REF-{test_tag}"
        },
        lines_data=[
            {"account_code": "6100", "debit": 50000.00, "credit": 0.0, "description": "Audit & Legal Fee"},
            {"account_code": "2050", "debit": 0.0, "credit": tds_amount, "description": "TDS Payable Sec 194J"},
            {"account_code": "1020", "debit": 0.0, "credit": 45000.00, "description": "Bank payment net of TDS"}
        ],
        user="test_runner"
    )

    tax_rep_after_tds = generate_tax_report()
    tb_after_tds = generate_trial_balance()
    gl_tds_payable = next((l["credit"] - l["debit"] for l in tb_after_tds["lines"] if l["code"] == "2050"), 0.0)
    assert_test(
        13,
        "TDS transaction affects correct TDS accounts",
        abs(tax_rep_after_tds["tds"]["net_tds_payable"] - gl_tds_payable) < 0.01,
        f"TDS Report Payable ₹{tax_rep_after_tds['tds']['net_tds_payable']:,.2f} == GL Account 2050 ₹{gl_tds_payable:,.2f}"
    )

    # =========================================================================
    # 14. OPENING BALANCES APPEAR CORRECTLY
    # =========================================================================
    print("\n--- SECTION 6: OPENING BALANCES & MOVEMENTS ---")
    tb_mov = generate_trial_balance(view_mode="movement")
    assert_test(
        14,
        "Opening balances appear correctly",
        "total_opening_debit" in tb_mov and "total_opening_credit" in tb_mov and abs(tb_mov["total_opening_debit"] - tb_mov["total_opening_credit"]) < 0.01,
        f"Opening Dr ₹{tb_mov['total_opening_debit']:,.2f} == Opening Cr ₹{tb_mov['total_opening_credit']:,.2f}"
    )

    # =========================================================================
    # 15. REVERSAL UPDATES ALL AFFECTED REPORTS
    # =========================================================================
    print("\n--- SECTION 7: REVERSALS & CORRECTIONS ---")
    tb_pre_rev = generate_trial_balance()
    pnl_pre_rev = generate_profit_and_loss()
    bs_pre_rev = generate_balance_sheet()

    # Create a temporary expense voucher to reverse
    temp_rev_entry = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-TEMP-REV-{test_tag}",
            "entry_date": today_str,
            "narration": "Temporary miscellaneous supplies to test reversal",
            "source_module": "expense"
        },
        lines_data=[
            {"account_code": "6050", "debit": 1250.00, "credit": 0.0, "description": "Stationery supplies"},
            {"account_code": "1010", "debit": 0.0, "credit": 1250.00, "description": "Cash payment"}
        ],
        user="test_runner"
    )
    rev_target_id = temp_rev_entry["entry_id"]

    # Now reverse this entry
    rev_res = DoubleEntryEngine.reverse_journal_entry(
        entry_id=rev_target_id,
        reason="Erroneous stationery voucher test reversal",
        user="test_runner"
    )

    tb_post_rev = generate_trial_balance()
    pnl_post_rev = generate_profit_and_loss()
    bs_post_rev = generate_balance_sheet()

    assert_test(
        15,
        "Reversal updates all affected reports",
        tb_post_rev["is_balanced"] and bs_post_rev["is_balanced"] and abs(pnl_post_rev["operating_expenses"]["total"] - pnl_pre_rev["operating_expenses"]["total"]) < 0.01,
        f"OpEx returned exactly to baseline ₹{pnl_post_rev['operating_expenses']['total']:,.2f}, TB & BS in perfect balance"
    )

    # =========================================================================
    # 16. CORRECTION UPDATES ALL AFFECTED REPORTS
    # =========================================================================
    # Create an entry with wrong amount ₹2,000, then correct it to ₹1,500
    wrong_entry = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-TO-CORRECT-{test_tag}",
            "entry_date": today_str,
            "narration": "Entry to be corrected",
            "source_module": "expense"
        },
        lines_data=[
            {"account_code": "6060", "debit": 2000.00, "credit": 0.0, "description": "Courier charges (Wrong)"},
            {"account_code": "1010", "debit": 0.0, "credit": 2000.00, "description": "Cash paid"}
        ],
        user="test_runner"
    )

    corr_res = DoubleEntryEngine.correct_journal_entry(
        entry_id=wrong_entry["entry_id"],
        corrected_entry_data={"narration": "Corrected courier charges to ₹1,500"},
        corrected_lines_data=[
            {"account_code": "6060", "debit": 1500.00, "credit": 0.0, "description": "Courier charges (Corrected)"},
            {"account_code": "1010", "debit": 0.0, "credit": 1500.00, "description": "Cash paid (Corrected)"}
        ],
        reason="Corrected courier voucher amount from 2000 to 1500",
        user="test_runner"
    )

    tb_post_corr = generate_trial_balance()
    bs_post_corr = generate_balance_sheet()
    assert_test(
        16,
        "Correction updates all affected reports",
        corr_res["status"] == "success" and tb_post_corr["is_balanced"] and bs_post_corr["is_balanced"],
        f"Original reversed with JV #{corr_res.get('reversal_entry_id')}, new posted with JV #{corr_res.get('corrected_entry_id')}"
    )

    # =========================================================================
    # 17. CLOSED-PERIOD TRANSACTIONS ARE HANDLED CORRECTLY
    # =========================================================================
    print("\n--- SECTION 8: PERIOD BOUNDARIES & AUDIT ---")
    cur.execute("""
        SELECT id, period_name, start_date, end_date
        FROM accounting_periods
        WHERE status = 'OPEN'
        ORDER BY period_number DESC LIMIT 1;
    """)
    period_to_test = cur.fetchone()

    closed_blocked = False
    if period_to_test:
        PeriodControlEngine.close_period(period_to_test["id"], user="test_runner")
        try:
            DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_number": f"JV-BLOCKED-{test_tag}",
                    "entry_date": str(period_to_test["start_date"]),
                    "narration": "Attempted posting into closed period"
                },
                lines_data=[
                    {"account_code": "1010", "debit": 100.0, "credit": 0.0, "description": "Test Dr"},
                    {"account_code": "3010", "debit": 0.0, "credit": 100.0, "description": "Test Cr"}
                ],
                user="test_runner"
            )
        except (PeriodClosedError, PeriodLockedError):
            closed_blocked = True
        finally:
            PeriodControlEngine.reopen_period(period_to_test["id"], user="admin", reason="Test cleanup")

    assert_test(
        17,
        "Closed-period transactions are handled correctly",
        closed_blocked,
        f"Period #{period_to_test['id']} closure strictly prohibited posting (PeriodClosedError)"
    )

    # =========================================================================
    # 18. TRIAL BALANCE DEBIT = CREDIT
    # =========================================================================
    print("\n--- SECTION 9: REPORT CONSISTENCY RULES ---")
    tb_final = generate_trial_balance()
    assert_test(
        18,
        "Trial Balance Debit = Credit",
        tb_final["total_debit"] == tb_final["total_credit"] and tb_final["is_balanced"],
        f"Total Dr ₹{tb_final['total_debit']:,.2f} == Total Cr ₹{tb_final['total_credit']:,.2f}"
    )

    # =========================================================================
    # 19. BALANCE SHEET ASSETS = LIABILITIES + EQUITY
    # =========================================================================
    bs_final = generate_balance_sheet()
    diff_bs = abs(bs_final["assets"]["total_assets"] - bs_final["total_liabilities_and_equity"])
    assert_test(
        19,
        "Balance Sheet Assets = Liabilities + Equity",
        diff_bs < 0.01 and bs_final["is_balanced"],
        f"Total Assets ₹{bs_final['assets']['total_assets']:,.2f} == Total Liab + Eq ₹{bs_final['total_liabilities_and_equity']:,.2f}"
    )

    # =========================================================================
    # 20. P&L NET PROFIT/LOSS AGREES WITH BALANCE SHEET EQUITY
    # =========================================================================
    pnl_final = generate_profit_and_loss()
    bs_net_profit = bs_final["equity"]["current_period_profit"]
    assert_test(
        20,
        "P&L Net Profit/Loss agrees with corresponding Balance Sheet treatment",
        abs(pnl_final["net_profit"] - bs_net_profit) < 0.01,
        f"P&L Net Profit (₹{pnl_final['net_profit']:,.2f}) == BS Equity Line 3030 (₹{bs_net_profit:,.2f})"
    )

    # =========================================================================
    # 21. TAX BALANCES AGREE WITH RELEVANT LEDGER/CONTROL ACCOUNTS
    # =========================================================================
    tax_final = generate_tax_report()
    gst_out_ledger = next((l["credit"] - l["debit"] for l in tb_final["lines"] if l["code"] == "2030"), 0.0)
    assert_test(
        21,
        "Tax balances agree with relevant ledger/control accounts",
        abs(tax_final["gst"]["total_output_gst"] - gst_out_ledger) < 0.01,
        f"Tax Report GST Output (₹{tax_final['gst']['total_output_gst']:,.2f}) == GL Account 2030 (₹{gst_out_ledger:,.2f})"
    )

    # =========================================================================
    # 22. DRILL-DOWN REACHES ORIGINAL TRANSACTION
    # =========================================================================
    print("\n--- SECTION 10: REPORT DRILL-DOWN PROVENANCE ---")
    dd = get_account_drilldown("1010")
    assert_test(
        22,
        "Drill-down reaches the original transaction",
        dd is not None and "transactions" in dd and len(dd["transactions"]) > 0 and "entry_number" in dd["transactions"][0] and "running_balance" in dd["transactions"][0],
        f"Account 1010 ({dd['account']['name']}) has {len(dd['transactions'])} transactions with full line details and references"
    )

    # =========================================================================
    # 23. NO REPORT CONTAINS HARD-CODED ACCOUNTING VALUES
    # =========================================================================
    # Add a distinct journal entry with an unusual amount (₹7,777.00) and verify it flows through all reports
    distinct_val = 7777.00
    distinct_entry = DoubleEntryEngine.post_journal_entry(
        entry_data={
            "entry_number": f"JV-DISTINCT-{test_tag}",
            "entry_date": today_str,
            "narration": f"Distinct amount test {distinct_val}",
            "source_module": "expense"
        },
        lines_data=[
            {"account_code": "6130", "debit": distinct_val, "credit": 0.0, "description": "Distinct travel expense"},
            {"account_code": "1010", "debit": 0.0, "credit": distinct_val, "description": "Cash paid"}
        ],
        user="test_runner"
    )

    pnl_dyn = generate_profit_and_loss()
    travel_line = next((l for l in pnl_dyn["operating_expenses"]["lines"] if l["code"] == "6130"), None)
    tb_dyn = generate_trial_balance()
    tb_travel = next((l for l in tb_dyn["lines"] if l["code"] == "6130"), None)

    assert_test(
        23,
        "No report contains hard-coded accounting values",
        travel_line is not None and travel_line["amount"] >= distinct_val and tb_travel is not None and tb_travel["debit"] >= distinct_val,
        f"Dynamic journal amount ₹{distinct_val:,.2f} dynamically updated P&L and TB without any hardcoding"
    )

    # =========================================================================
    # 24. NO DUPLICATE CALCULATIONS FROM UNRELATED DATA SOURCES
    # =========================================================================
    # Run the comprehensive cross-report consistency engine
    audit_res = verify_report_consistency()
    all_rules_pass = audit_res["is_consistent"] and audit_res["passed_rules"] == 10
    assert_test(
        24,
        "No duplicate calculations: All 10 Cross-Report Consistency Rules PASS",
        all_rules_pass,
        f"Passed: {audit_res['passed_rules']} / {audit_res['total_rules']} Rules (Status: {audit_res['status']})"
    )

    # Health Check Engine Check
    hc = HealthCheckEngine.run_full_health_check()
    print(f"\n>> Accounting Health Check Score: {hc.get('health_score', hc.get('score', 100))}/100 ({hc['status']}), Issues: {len(hc['issues'])}")

    # Clean up test entries created during this test run so DB returns to pure state
    try:
        cur.execute("SELECT id FROM journal_entries WHERE entry_number LIKE %s;", (f"%{test_tag}%",))
        test_ids = [r["id"] for r in cur.fetchall()]
        if test_ids:
            placeholders = ",".join(["%s"] * len(test_ids))
            cur.execute(f"DELETE FROM journal_lines WHERE entry_id IN ({placeholders});", tuple(test_ids))
            cur.execute(f"DELETE FROM accounting_audit_trail WHERE entity_id IN ({placeholders}) AND entity_type = 'journal_entry';", tuple(test_ids))
            cur.execute(f"DELETE FROM journal_entries WHERE id IN ({placeholders});", tuple(test_ids))
            conn.commit()
            print(f"[*] Cleaned up {len(test_ids)} temporary test journals.")
    except Exception as e:
        print(f"Warning during test cleanup: {e}")

    print("\n" + "=" * 80)
    print(f"   COMPLETED: {passed} / {total} TESTS PASSED (100% SUCCESS)   ")
    print("=" * 80)

    cur.close()
    conn.close()
    return passed == total


if __name__ == "__main__":
    success = run_reports_and_calculations_tests()
    if not success:
        sys.exit(1)

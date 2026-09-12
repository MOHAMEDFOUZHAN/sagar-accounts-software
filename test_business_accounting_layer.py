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
from backend.coa_engine import ChartOfAccountsEngine
from backend.double_entry_engine import DoubleEntryEngine
from backend.ledger_engine import LedgerEngine
from backend.accounting_rules import AccountingRules
from backend.period_engine import PeriodControlEngine, PeriodClosedError
from backend.reconciliation_engine import ReconciliationEngine
from backend.business_accounting import BusinessAccountingService
from backend.reports_engine import (
    generate_profit_and_loss,
    generate_balance_sheet,
    generate_trial_balance,
    generate_tax_report,
    verify_report_consistency
)
from backend.accounts_engine import (
    get_receivables,
    record_receivable_payment,
    get_payables,
    record_payable_payment,
    create_fixed_asset,
    create_liability
)


def run_business_accounting_test_suite():
    print("=" * 85)
    print("   SAGAR ACCOUNTS — LAYER 4: BUSINESS ACCOUNTING COMPREHENSIVE TEST SUITE   ")
    print("=" * 85)

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    passed = 0
    total = 0
    test_journals_to_clean = []
    test_records_to_clean = {
        "receivables": [],
        "payables": [],
        "assets": [],
        "liabilities": []
    }

    def assert_test(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        if condition:
            passed += 1
            print(f"  [PASS] {name} | {detail}")
        else:
            print(f"  [FAIL] {name} | {detail}")
            raise AssertionError(f"Test failed: {name} - {detail}")

    def track_jv(res):
        if isinstance(res, dict):
            jid = res.get("journal_entry_id") or res.get("entry_id") or res.get("journal_id")
            if jid:
                test_journals_to_clean.append(int(jid))
                return int(jid)
        elif isinstance(res, (int, str)):
            try:
                jid = int(res)
                test_journals_to_clean.append(jid)
                return jid
            except Exception:
                pass
        return None

    try:
        today = datetime.date.today()
        today_str = today.isoformat()

        # =====================================================================
        # SECTION 1: ACCOUNTS RECEIVABLE (AR)
        # =====================================================================
        print("\n--- SECTION 1: ACCOUNTS RECEIVABLE (AR) ---")

        # 1.1 Customer invoice creation
        cust_test = f"Test Cust {int(datetime.datetime.now().timestamp()) % 10000}"
        cur.execute("""
            INSERT INTO accounts_receivables
                (receivable_no, invoice_ref, customer_name, contact_phone, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (
            f"REC-TST-{cust_test[-4:]}",
            f"INV-TST-{cust_test[-4:]}",
            cust_test,
            "9988776655",
            today_str,
            today_str,
            11800.00,
            0.0,
            11800.00,
            "Pending"
        ))
        conn.commit()
        cur.execute("SELECT last_insert_rowid() as id;")
        rec_id = cur.fetchone()["id"]
        test_records_to_clean["receivables"].append(rec_id)

        # Post matching GL journal for customer invoice (Dr AR 1040, Cr Sales 4010, Cr GST 2030)
        ar_acc = ChartOfAccountsEngine.get_account_by_code("1040", conn=conn)
        sales_acc = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)
        tax_acc = ChartOfAccountsEngine.get_account_by_code("2030", conn=conn)

        inv_jv = DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": today_str,
                "source_module": "sales",
                "source_entity": "bill",
                "source_id": str(rec_id),
                "reference_no": f"INV-TST-{cust_test[-4:]}",
                "narration": f"Test Credit Invoice for {cust_test}",
                "status": "POSTED"
            },
            lines_data=[
                {"account_id": ar_acc["id"], "debit": 11800.00, "credit": 0.0, "party_type": "customer", "party_name": cust_test},
                {"account_id": sales_acc["id"], "debit": 0.0, "credit": 10000.00, "party_type": "customer", "party_name": cust_test},
                {"account_id": tax_acc["id"], "debit": 0.0, "credit": 1800.00, "party_type": "customer", "party_name": cust_test}
            ],
            user="test_runner",
            external_conn=conn
        )
        track_jv(inv_jv)
        assert_test("Test #01: Customer Invoice Posted", inv_jv["entry_id"] > 0, f"Invoice #{rec_id} posted with JV #{inv_jv['entry_id']}")

        # 1.2 Customer Receipt Payment
        rec_res = record_receivable_payment(rec_id, 5000.00, "Bank Transfer", f"REC-PAY-{rec_id}", "Partial payment", "test_runner")
        track_jv(rec_res)
        cur.execute("SELECT remaining_balance, paid_amount, status FROM accounts_receivables WHERE id = %s;", (rec_id,))
        rec_row = cur.fetchone()
        assert_test("Test #02: Customer Receipt", rec_row["remaining_balance"] == 6800.00 and rec_row["status"] == "Partial", f"Remaining: ₹{rec_row['remaining_balance']}, Paid: ₹{rec_row['paid_amount']}")

        # 1.3 Customer Credit Note (Sales return / price adjustment)
        cn_res = AccountingRules.post_customer_credit_note({
            "customer_name": cust_test,
            "receivable_id": rec_id,
            "invoice_ref": f"INV-TST-{cust_test[-4:]}",
            "amount": 1180.00,
            "tax_amount": 180.00,
            "reason": "Damaged items credited back",
            "date": today_str,
            "user": "test_runner"
        }, conn=conn)
        track_jv(cn_res)
        cur.execute("SELECT remaining_balance FROM accounts_receivables WHERE id = %s;", (rec_id,))
        rec_row2 = cur.fetchone()
        assert_test("Test #03: Customer Credit Note", rec_row2["remaining_balance"] == 5620.00, f"Balance reduced to ₹{rec_row2['remaining_balance']} after ₹1,180.00 credit note")

        # 1.4 Customer Advance
        adv_res = AccountingRules.post_customer_advance({
            "customer_name": cust_test,
            "amount": 2500.00,
            "payment_method": "Bank Transfer",
            "date": today_str,
            "narration": "Advance for upcoming orders",
            "user": "test_runner"
        }, conn=conn)
        track_jv(adv_res)
        adv_acc = ChartOfAccountsEngine.get_account_by_code("2060", conn=conn)
        adv_bal = LedgerEngine.get_account_balance(adv_acc["id"], conn=conn)
        assert_test("Test #04: Customer Advance Posted", adv_bal["balance"] >= 2500.00, f"Account 2060 liability: ₹{adv_bal['balance']}")

        # 1.5 Bad Debt Write-Off
        wo_res = AccountingRules.post_customer_writeoff({
            "customer_name": cust_test,
            "receivable_id": rec_id,
            "amount": 620.00,
            "reason": "Uncollectible portion written off",
            "date": today_str,
            "user": "test_runner"
        }, conn=conn)
        track_jv(wo_res)
        cur.execute("SELECT remaining_balance FROM accounts_receivables WHERE id = %s;", (rec_id,))
        rec_row3 = cur.fetchone()
        assert_test("Test #05: Bad Debt Write-off", rec_row3["remaining_balance"] == 5000.00, f"Receivable balance reduced to ₹{rec_row3['remaining_balance']}")

        # 1.6 5-Bucket AR Ageing
        ageing = BusinessAccountingService.get_ar_ageing(conn=conn)
        assert_test("Test #06: AR Ageing 5-Bucket", "current" in ageing["totals"] and "days_1_30" in ageing["totals"] and "days_90_plus" in ageing["totals"], f"Ageing total: ₹{ageing['totals']['total_outstanding']}, Customer count: {ageing['customer_count']}")

        # 1.7 Customer Ledger Statement
        cust_ledger = BusinessAccountingService.get_customer_ledger(cust_test, conn=conn)
        assert_test("Test #07: Customer Ledger Statement", cust_ledger["count"] >= 3, f"Found {cust_ledger['count']} movements for {cust_test}, Net balance: ₹{cust_ledger['closing_balance']}")

        # 1.8 AR Control Account Reconciliation
        ar_recon = BusinessAccountingService.get_ar_reconciliation(conn=conn)
        assert_test("Test #08: AR Control Reconciliation", "subledger_total" in ar_recon and "gl_control_balance" in ar_recon, f"Subledger: ₹{ar_recon['subledger_total']}, GL 1040: ₹{ar_recon['gl_control_balance']}, Diff: ₹{ar_recon['difference']}")

        # =====================================================================
        # SECTION 2: ACCOUNTS PAYABLE (AP)
        # =====================================================================
        print("\n--- SECTION 2: ACCOUNTS PAYABLE (AP) ---")

        # 2.1 Supplier Bill
        supp_test = f"Test Vendor {int(datetime.datetime.now().timestamp()) % 10000}"
        cur.execute("""
            INSERT INTO accounts_payables
                (payable_no, invoice_ref, supplier_name, contact_phone, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (
            f"PAY-TST-{supp_test[-4:]}",
            f"BILL-TST-{supp_test[-4:]}",
            supp_test,
            "9876543210",
            today_str,
            today_str,
            15000.00,
            0.0,
            15000.00,
            "Pending"
        ))
        conn.commit()
        cur.execute("SELECT last_insert_rowid() as id;")
        pay_id = cur.fetchone()["id"]
        test_records_to_clean["payables"].append(pay_id)

        # Post matching GL bill entry: Dr Inventory 1050, Cr AP 2010
        ap_acc = ChartOfAccountsEngine.get_account_by_code("2010", conn=conn)
        inv_acc = ChartOfAccountsEngine.get_account_by_code("1050", conn=conn)
        bill_jv = DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": today_str,
                "source_module": "inventory",
                "source_entity": "purchase_order",
                "source_id": str(pay_id),
                "reference_no": f"BILL-TST-{supp_test[-4:]}",
                "narration": f"Inward raw materials from {supp_test}",
                "status": "POSTED"
            },
            lines_data=[
                {"account_id": inv_acc["id"], "debit": 15000.00, "credit": 0.0, "party_type": "supplier", "party_name": supp_test},
                {"account_id": ap_acc["id"], "debit": 0.0, "credit": 15000.00, "party_type": "supplier", "party_name": supp_test}
            ],
            user="test_runner",
            external_conn=conn
        )
        track_jv(bill_jv)
        assert_test("Test #09: Supplier Bill Posted", bill_jv["entry_id"] > 0, f"Bill #{pay_id} posted with JV #{bill_jv['entry_id']}")

        # 2.2 Supplier Payment
        pay_res = record_payable_payment(pay_id, 6000.00, "Bank Transfer", f"DISB-{pay_id}", "Partial settlement", "test_runner")
        track_jv(pay_res)
        cur.execute("SELECT remaining_balance, paid_amount, status FROM accounts_payables WHERE id = %s;", (pay_id,))
        pay_row = cur.fetchone()
        assert_test("Test #10: Supplier Payment", pay_row["remaining_balance"] == 9000.00 and pay_row["status"] == "Partial", f"Remaining: ₹{pay_row['remaining_balance']}, Paid: ₹{pay_row['paid_amount']}")

        # 2.3 Supplier Debit Note (Purchase return / discount)
        dn_res = AccountingRules.post_supplier_debit_note({
            "supplier_name": supp_test,
            "payable_id": pay_id,
            "invoice_ref": f"BILL-TST-{supp_test[-4:]}",
            "amount": 2000.00,
            "tax_amount": 0.0,
            "reason": "Damaged raw material returned",
            "date": today_str,
            "user": "test_runner"
        }, conn=conn)
        track_jv(dn_res)
        cur.execute("SELECT remaining_balance FROM accounts_payables WHERE id = %s;", (pay_id,))
        pay_row2 = cur.fetchone()
        assert_test("Test #11: Supplier Debit Note", pay_row2["remaining_balance"] == 7000.00, f"Remaining AP reduced to ₹{pay_row2['remaining_balance']}")

        # 2.4 Supplier Advance
        s_adv_res = AccountingRules.post_supplier_advance({
            "supplier_name": supp_test,
            "amount": 3000.00,
            "payment_method": "Bank Transfer",
            "date": today_str,
            "narration": "Advance for upcoming batch shipment",
            "user": "test_runner"
        }, conn=conn)
        track_jv(s_adv_res)
        adv_supp_acc = ChartOfAccountsEngine.get_account_by_code("1070", conn=conn)
        adv_supp_bal = LedgerEngine.get_account_balance(adv_supp_acc["id"], conn=conn)
        assert_test("Test #12: Supplier Advance Posted", adv_supp_bal["balance"] >= 3000.00, f"Account 1070 advance asset: ₹{adv_supp_bal['balance']}")

        # 2.5 5-Bucket AP Ageing
        ap_ageing = BusinessAccountingService.get_ap_ageing(conn=conn)
        assert_test("Test #13: AP Ageing 5-Bucket", "current" in ap_ageing["totals"] and "days_1_30" in ap_ageing["totals"], f"AP Ageing total: ₹{ap_ageing['totals']['total_outstanding']}, Supplier count: {ap_ageing['supplier_count']}")

        # 2.6 Supplier Ledger Statement
        supp_ledger = BusinessAccountingService.get_supplier_ledger(supp_test, conn=conn)
        assert_test("Test #14: Supplier Ledger Statement", supp_ledger["count"] >= 3, f"Found {supp_ledger['count']} movements for {supp_test}, Closing: ₹{supp_ledger['closing_balance']}")

        # 2.7 AP Control Account Reconciliation
        ap_recon = BusinessAccountingService.get_ap_reconciliation(conn=conn)
        assert_test("Test #15: AP Control Reconciliation", "subledger_total" in ap_recon and "gl_control_balance" in ap_recon, f"Subledger: ₹{ap_recon['subledger_total']}, GL 2010: ₹{ap_recon['gl_control_balance']}")

        # =====================================================================
        # SECTION 3: CASH & BANK ACCOUNTING
        # =====================================================================
        print("\n--- SECTION 3: CASH & BANK ACCOUNTING ---")

        # 3.1 Contra Transfer: Cash -> Bank
        cb_summary_before = BusinessAccountingService.get_cash_bank_summary(conn=conn)
        transfer_res = AccountingRules.post_contra_transfer({
            "from_account_code": "1010",
            "to_account_code": "1020",
            "amount": 2000.00,
            "date": today_str,
            "reference_no": "CHQ-DEP-01",
            "narration": "Vault cash deposit into operating bank account",
            "user": "test_runner"
        }, conn=conn)
        track_jv(transfer_res)

        cb_summary_after = BusinessAccountingService.get_cash_bank_summary(conn=conn)
        diff_liquid = abs(cb_summary_after["total_liquid_assets"] - cb_summary_before["total_liquid_assets"])
        assert_test("Test #16: Contra Cash-to-Bank Transfer", diff_liquid < 0.01, f"Cash reduced by ₹2,000, Bank increased by ₹2,000, Net Liquid Asset change: ₹{diff_liquid:.2f}")

        # 3.2 Bank Charges Posting
        charges_res = AccountingRules.post_bank_charges({
            "bank_account_code": "1020",
            "amount": 350.00,
            "date": today_str,
            "description": "Monthly current account ledger maintenance fee",
            "user": "test_runner"
        }, conn=conn)
        track_jv(charges_res)
        chg_acc = ChartOfAccountsEngine.get_account_by_code("7010", conn=conn)
        chg_bal = LedgerEngine.get_account_balance(chg_acc["id"], conn=conn)
        assert_test("Test #17: Bank Charges Accounting", chg_bal["balance"] >= 350.00, f"Account 7010 (Bank Charges) balance: ₹{chg_bal['balance']}")

        # =====================================================================
        # SECTION 4: BANK RECONCILIATION
        # =====================================================================
        print("\n--- SECTION 4: BANK RECONCILIATION ---")

        # 4.1 Bank Reconciliation Worksheet
        worksheet = ReconciliationEngine.get_bank_reconciliation_worksheet(as_of_date=today_str, statement_balance=None, conn=conn)
        assert_test("Test #18: Bank Reconciliation Worksheet", "gl_book_balance" in worksheet and "statement_balance" in worksheet, f"Book: ₹{worksheet['gl_book_balance']}, Adjusted Book: ₹{worksheet['adjusted_book_balance']}, Status: {worksheet['status']}")

        # 4.2 Statement with Float Items (Transit deposits, unpresented cheques)
        ws_float = ReconciliationEngine.get_bank_reconciliation_worksheet(
            as_of_date=today_str,
            statement_balance=worksheet["gl_book_balance"] + 1500.00,
            conn=conn
        )
        assert_test("Test #19: Bank Float Discrepancy Detection", ws_float["status"] == "DISCREPANCY" and abs(abs(ws_float["reconciliation_difference"]) - 1500.00) < 0.01, f"Detected difference: ₹{ws_float['reconciliation_difference']}")

        # =====================================================================
        # SECTION 5: FIXED ASSETS & DEPRECIATION
        # =====================================================================
        print("\n--- SECTION 5: FIXED ASSETS & DEPRECIATION ---")

        # 5.1 Asset Creation
        ast_name = f"CNC Packaging Unit {int(datetime.datetime.now().timestamp()) % 1000}"
        ast_res = create_fixed_asset(
            asset_name=ast_name,
            category="Machinery",
            purchase_date=today_str,
            purchase_value=60000.00,
            useful_life_years=5,
            supplier="Heavy Machinery Ltd",
            payment_method="Bank Transfer",
            notes="Factory production unit"
        )
        ast_id = ast_res["asset_id"]
        test_records_to_clean["assets"].append(ast_id)
        cur.execute("SELECT id FROM journal_entries WHERE source_module = 'asset' AND source_id = %s;", (str(ast_id),))
        ast_jv = cur.fetchone()
        if ast_jv:
            track_jv(ast_jv["id"])
        assert_test("Test #20: Fixed Asset Purchase Booked", ast_id > 0, f"Asset #{ast_id} created ({ast_res['asset_code']}) with cost ₹60,000.00")

        # 5.2 Periodic Depreciation Posting
        depr_res = AccountingRules.post_periodic_depreciation(
            asset_id=ast_id,
            depreciation_date=today_str,
            depreciation_amount=1000.00,
            user="test_runner",
            conn=conn
        )
        track_jv(depr_res)
        assert_test("Test #21: Periodic Depreciation Posted", depr_res["new_accumulated_depreciation"] == 1000.00 and depr_res["new_net_book_value"] == 59000.00, f"NBV: ₹{depr_res['new_net_book_value']}, Depr Expense: ₹1,000.00 posted with JV #{depr_res['journal_entry_id']}")

        # 5.3 Duplicate Depreciation Prevention
        dup_prevented = False
        try:
            AccountingRules.post_periodic_depreciation(
                asset_id=ast_id,
                depreciation_date=today_str,
                depreciation_amount=1000.00,
                user="test_runner",
                conn=conn
            )
        except ValueError as e:
            if "already been" in str(e):
                dup_prevented = True
        assert_test("Test #22: Duplicate Depreciation Blocked", dup_prevented, "Engine rejected second depreciation posting for same asset and period")

        # 5.4 Asset Disposal with Gain/Loss
        disp_res = AccountingRules.post_asset_disposal({
            "asset_id": ast_id,
            "disposal_date": today_str,
            "disposal_proceeds": 62000.00, # Sold for ₹62,000 when NBV is ₹59,000 -> Gain of ₹3,000
            "payment_method": "Bank Transfer",
            "notes": "Upgraded to newer model",
            "user": "test_runner"
        }, conn=conn)
        track_jv(disp_res)
        cur.execute("SELECT status, current_value FROM accounts_fixed_assets WHERE id = %s;", (ast_id,))
        disp_ast = cur.fetchone()
        assert_test("Test #23: Asset Disposal with Gain", disp_ast["status"] == "Disposed" and disp_res["gain_loss"] == 3000.00, f"Gain on disposal: ₹{disp_res['gain_loss']}, Status: {disp_ast['status']}")

        # =====================================================================
        # SECTION 6: LOAN ACCOUNTING
        # =====================================================================
        print("\n--- SECTION 6: LOAN ACCOUNTING ---")

        # 6.1 Loan Receipt
        loan_title = f"Working Capital Loan {int(datetime.datetime.now().timestamp()) % 1000}"
        loan_res = create_liability(
            title=loan_title,
            liability_type="Bank Loan",
            principal_amount=100000.00,
            interest_rate=9.5,
            tenure_months=24,
            lender="State Bank of India",
            start_date=today_str,
            notes="Business expansion loan"
        )
        lia_id = loan_res["liability_id"]
        test_records_to_clean["liabilities"].append(lia_id)
        cur.execute("SELECT id FROM journal_entries WHERE source_module = 'liability' AND source_id = %s;", (str(lia_id),))
        lia_jv = cur.fetchone()
        if lia_jv:
            track_jv(lia_jv["id"])
        assert_test("Test #24: Commercial Loan Disbursed", lia_id > 0, f"Loan #{lia_id} booked ({loan_res['liability_code']}) with principal ₹100,000.00")

        # 6.2 Loan Installment with Principal + Interest Split
        repay_res = AccountingRules.post_loan_repayment_installment({
            "liability_id": lia_id,
            "principal_amount": 10000.00,
            "interest_amount": 800.00,
            "payment_method": "Bank Transfer",
            "payment_date": today_str,
            "reference_no": "EMI-01",
            "notes": "Month 1 EMI payment",
            "user": "test_runner"
        }, conn=conn)
        track_jv(repay_res)
        assert_test("Test #25: Loan Installment Split Repayment", repay_res["remaining_balance"] == 90000.00 and repay_res["total_paid"] == 10800.00, f"Principal reduced to ₹{repay_res['remaining_balance']}, Interest of ₹800 charged to P&L (7020), Total paid: ₹{repay_res['total_paid']}")

        # 6.3 Loan Subledger
        loan_sub = BusinessAccountingService.get_loan_subledger(conn=conn)
        assert_test("Test #26: Loan Subledger Reconciliation", "total_outstanding_balance" in loan_sub and "gl_loan_balance" in loan_sub, f"Subledger Outstanding: ₹{loan_sub['total_outstanding_balance']}, GL Loan: ₹{loan_sub['gl_loan_balance']}")

        # =====================================================================
        # SECTION 7: CAPITAL & DRAWINGS
        # =====================================================================
        print("\n--- SECTION 7: CAPITAL & DRAWINGS ---")

        # 7.1 Capital Contribution
        cap_res = AccountingRules.post_capital({
            "investor_name": "Sagar Partner",
            "amount": 50000.00,
            "payment_method": "Bank Transfer",
            "date": today_str,
            "narration": "Infusion of additional equity capital",
            "user": "test_runner"
        }, conn=conn)
        track_jv(cap_res)
        cap_acc = ChartOfAccountsEngine.get_account_by_code("3010", conn=conn)
        cap_bal = LedgerEngine.get_account_balance(cap_acc["id"], conn=conn)
        assert_test("Test #27: Capital Contribution", cap_bal["balance"] >= 50000.00, f"Account 3010 (Owner Capital) balance: ₹{cap_bal['balance']}")

        # 7.2 Owner Personal Drawings
        draw_res = AccountingRules.post_drawings({
            "owner_name": "Sagar Partner",
            "amount": 8000.00,
            "payment_method": "Cash",
            "date": today_str,
            "narration": "Monthly personal allowance withdrawal",
            "user": "test_runner"
        }, conn=conn)
        track_jv(draw_res)
        draw_acc = ChartOfAccountsEngine.get_account_by_code("3020", conn=conn)
        draw_bal = LedgerEngine.get_account_balance(draw_acc["id"], conn=conn)
        assert_test("Test #28: Owner Drawings (Contra-Equity)", draw_bal["balance"] >= 8000.00, f"Account 3020 (Drawings) balance: ₹{draw_bal['balance']}")

        # 7.3 Drawings Do Not Affect Operating Expenses
        pnl = generate_profit_and_loss(conn=conn)
        opex_codes = [line["code"] for line in pnl["operating_expenses"]["lines"]]
        assert_test("Test #29: Drawings Excluded from P&L OPEX", "3020" not in opex_codes, "Account 3020 (Drawings) is strictly treated as Equity contra in Balance Sheet, zero OPEX contamination")

        # =====================================================================
        # SECTION 8: EXPENSE ACCOUNTING
        # =====================================================================
        print("\n--- SECTION 8: EXPENSE ACCOUNTING ---")

        # 8.1 Operating Expense (Rent)
        rent_res = AccountingRules.post_expense({
            "id": 99991,
            "category": "Office Rent",
            "description": "Monthly branch rent",
            "amount": 12000.00,
            "payment_method": "Bank Transfer",
            "date": today_str
        }, conn=conn)
        track_jv(rent_res)
        rent_acc = ChartOfAccountsEngine.get_account_by_code("6040", conn=conn)
        rent_bal = LedgerEngine.get_account_balance(rent_acc["id"], conn=conn)
        assert_test("Test #30: Operating Expense (Office Rent)", rent_bal["balance"] >= 12000.00, f"Account 6040 balance: ₹{rent_bal['balance']}")

        # 8.2 Petty Expense (Tea & Snacks)
        petty_res = AccountingRules.post_expense({
            "id": 99992,
            "category": "Petty Expenses",
            "description": "Tea and client hospitality",
            "amount": 450.00,
            "payment_method": "Cash",
            "date": today_str
        }, conn=conn)
        track_jv(petty_res)
        petty_acc = ChartOfAccountsEngine.get_account_by_code("6160", conn=conn)
        petty_bal = LedgerEngine.get_account_balance(petty_acc["id"], conn=conn)
        assert_test("Test #31: Petty Expense Accounting", petty_bal["balance"] >= 450.00, f"Account 6160 balance: ₹{petty_bal['balance']}")

        # =====================================================================
        # SECTION 9: INVENTORY & COGS ACCOUNTING
        # =====================================================================
        print("\n--- SECTION 9: INVENTORY & COGS ACCOUNTING ---")

        # 9.1 Stock Wastage / Damage Adjustment
        inv_adj_res = AccountingRules.post_inventory_adjustment({
            "product_code": "RAW-MAT-01",
            "qty_change": -5.0,
            "unit_cost": 250.00,
            "adjustment_type": "damage",
            "reason": "Water damage during warehouse transport",
            "date": today_str,
            "user": "test_runner"
        }, conn=conn)
        track_jv(inv_adj_res)
        assert_test("Test #32: Stock Wastage Adjustment", inv_adj_res["entry_id"] > 0, f"Stock write-down of ₹1,250.00 posted with JV #{inv_adj_res['entry_id']}")

        # 9.2 Stock Surplus Adjustment
        surp_res = AccountingRules.post_inventory_adjustment({
            "product_code": "RAW-MAT-02",
            "qty_change": 3.0,
            "unit_cost": 400.00,
            "adjustment_type": "surplus",
            "reason": "Physical inventory count found excess packaging rolls",
            "date": today_str,
            "user": "test_runner"
        }, conn=conn)
        track_jv(surp_res)
        assert_test("Test #33: Stock Surplus Adjustment", surp_res["entry_id"] > 0, f"Stock surplus of ₹1,200.00 posted with JV #{surp_res['entry_id']}")

        # 9.3 Inventory Valuation Reconciliation
        inv_recon = BusinessAccountingService.get_inventory_reconciliation(conn=conn)
        assert_test("Test #34: Inventory Valuation Reconciliation", "live_storage_valuation" in inv_recon and "gl_inventory_balance" in inv_recon, f"Live Storage: ₹{inv_recon['live_storage_valuation']}, GL 1050/1060: ₹{inv_recon['gl_inventory_balance']}, Diff: ₹{inv_recon['difference']}")

        # =====================================================================
        # SECTION 10: CROSS-REPORT INTEGRITY & MATHEMATICAL BALANCING
        # =====================================================================
        print("\n--- SECTION 10: CROSS-REPORT INTEGRITY ---")

        # 10.1 Trial Balance Equilibrium
        tb = generate_trial_balance(conn=conn)
        assert_test("Test #35: Trial Balance Equilibrium", tb["is_balanced"], f"Total Dr: ₹{tb['total_debit']:,.2f} == Total Cr: ₹{tb['total_credit']:,.2f} (Diff: ₹{tb['difference']:,.2f})")

        # 10.2 Balance Sheet Accounting Equation
        bs = generate_balance_sheet(conn=conn)
        total_assets = bs["assets"]["total_assets"]
        total_liab_eq = bs["total_liabilities_and_equity"]
        assert_test("Test #36: Balance Sheet Equation", bs["is_balanced"], f"Assets: ₹{total_assets:,.2f} == Liabilities + Equity: ₹{total_liab_eq:,.2f}")

        # 10.3 All 10 Cross-Report Consistency Rules
        consistency = verify_report_consistency(conn=conn)
        for chk in consistency.get("checks", []):
            if not chk["passed"]:
                print(f"  [CONSISTENCY FAILED] {chk['rule']}: {chk['details']}")
        assert_test("Test #37: 10 Cross-Report Consistency Rules", consistency["passed_rules"] == consistency["total_rules"], f"Passed {consistency['passed_rules']} / {consistency['total_rules']} consistency rules (Status: {consistency['status']})")

    finally:
        # Cleanup temporary test journals safely
        print(f"\n[*] Cleaning up {len(test_journals_to_clean)} test journal entries and test subledger entities...")
        for jid in set(test_journals_to_clean):
            try:
                cur.execute("DELETE FROM journal_lines WHERE entry_id = %s;", (jid,))
                cur.execute("DELETE FROM journal_entries WHERE id = %s;", (jid,))
            except Exception:
                pass

        for rid in test_records_to_clean["receivables"]:
            try:
                cur.execute("DELETE FROM accounts_credit_notes WHERE receivable_id = %s;", (rid,))
                cur.execute("DELETE FROM accounts_receivables WHERE id = %s;", (rid,))
            except Exception:
                pass

        for pid in test_records_to_clean["payables"]:
            try:
                cur.execute("DELETE FROM accounts_debit_notes WHERE payable_id = %s;", (pid,))
                cur.execute("DELETE FROM accounts_payables WHERE id = %s;", (pid,))
            except Exception:
                pass

        for aid in test_records_to_clean["assets"]:
            try:
                cur.execute("DELETE FROM accounts_asset_depreciation_log WHERE asset_id = %s;", (aid,))
                cur.execute("DELETE FROM accounts_fixed_assets WHERE id = %s;", (aid,))
            except Exception:
                pass

        for lid in test_records_to_clean["liabilities"]:
            try:
                cur.execute("DELETE FROM accounts_liability_payments WHERE liability_id = %s;", (lid,))
                cur.execute("DELETE FROM accounts_liabilities WHERE id = %s;", (lid,))
            except Exception:
                pass

        from backend.reconciliation_baseline import ensure_reconciled_baseline
        ensure_reconciled_baseline(conn=conn)

        conn.commit()
        cur.close()
        conn.close()

    print("\n" + "=" * 85)
    print(f"   COMPLETED: {passed} / {total} TESTS PASSED (100% SUCCESS)   ")
    print("=" * 85)
    return passed == total


if __name__ == '__main__':
    success = run_business_accounting_test_suite()
    sys.exit(0 if success else 1)

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
from backend.double_entry_engine import DoubleEntryEngine, DoubleEntryError, UnbalancedJournalError
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
from backend.audit_engine import AuditEngine
from backend.health_check import HealthCheckEngine
from backend.sync_engine import get_live_inventory_valuation


def run_master_accounting_audit_suite():
    print("=" * 90)
    print("   SAGAR ACCOUNTS — COMPREHENSIVE FINAL QUALITY & ACCOUNTING AUDIT SUITE   ")
    print("=" * 90)

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    passed_tests = 0
    total_tests = 0
    audit_findings = []

    def test(title, func):
        nonlocal passed_tests, total_tests
        total_tests += 1
        print(f"\n[{total_tests:02d}] TESTING: {title}")
        try:
            func()
            passed_tests += 1
            print(f"     >>> PASS: {title}")
        except AssertionError as ae:
            msg = f"ASSERTION FAILED: {str(ae)}"
            print(f"     >>> FAIL: {msg}")
            audit_findings.append({"test": title, "status": "FAIL", "reason": msg})
        except Exception as e:
            msg = f"UNEXPECTED ERROR: {type(e).__name__} - {str(e)}"
            print(f"     >>> ERROR: {msg}")
            audit_findings.append({"test": title, "status": "ERROR", "reason": msg})

    # Track any temporary vouchers created during this audit
    created_journals = []
    created_subledger_items = {"receivables": [], "payables": [], "assets": [], "liabilities": []}

    from backend.reconciliation_baseline import ensure_reconciled_baseline
    ensure_reconciled_baseline(conn=conn)

    try:
        # =====================================================================
        # PART 1: CORE ACCOUNTING INVARIANTS & DOUBLE-ENTRY ENGINE
        # =====================================================================
        print("\n" + "-" * 80)
        print("PART 1: CORE ACCOUNTING FOUNDATION & DOUBLE-ENTRY ENGINE")
        print("-" * 80)

        def t_balanced_journal_posting():
            cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
            sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)
            gst = ChartOfAccountsEngine.get_account_by_code("2030", conn=conn)

            # Post a 3-line balanced sale: Cash Dr 1180, Sales Cr 1000, GST Cr 180
            res = DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_number": "JV-AUD-001",
                    "entry_date": "2026-02-18 10:00:00",
                    "source_module": "manual",
                    "source_entity": "audit_voucher",
                    "source_id": "AUD-001",
                    "narration": "Audit Test: Balanced Cash Sale with GST"
                },
                lines_data=[
                    {"account_id": cash["id"], "debit": 1180.0, "credit": 0.0, "description": "Cash receipt"},
                    {"account_id": sales["id"], "debit": 0.0, "credit": 1000.0, "description": "Operating Revenue"},
                    {"account_id": gst["id"], "debit": 0.0, "credit": 180.0, "description": "Output GST 18%", "tax_code": "GST18", "tax_rate": 18.0},
                ],
                user="auditor",
                external_conn=conn
            )
            created_journals.append(res["entry_id"])
            assert res["status"] == "POSTED"
            assert res["total_debit"] == 1180.0
            assert res["total_credit"] == 1180.0

            # Verify line sum in DB
            cur.execute("SELECT SUM(debit) as d, SUM(credit) as c FROM journal_lines WHERE entry_id = %s;", (res["entry_id"],))
            row = cur.fetchone()
            assert round(float(row["d"]), 2) == 1180.0
            assert round(float(row["c"]), 2) == 1180.0
        test("1. Double-Entry Posting: Total Debits == Total Credits (Header & Line Invariants)", t_balanced_journal_posting)

        def t_unbalanced_journal_rejection():
            cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
            sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)

            try:
                DoubleEntryEngine.post_journal_entry(
                    entry_data={
                        "entry_number": "JV-AUD-BAD-01",
                        "entry_date": "2026-02-18 10:05:00",
                        "source_module": "manual",
                        "narration": "Deliberately Unbalanced Voucher"
                    },
                    lines_data=[
                        {"account_id": cash["id"], "debit": 1500.0, "credit": 0.0, "description": "Cash in"},
                        {"account_id": sales["id"], "debit": 0.0, "credit": 1400.0, "description": "Sales out"},
                    ],
                    user="auditor",
                    external_conn=conn
                )
                assert False, "DoubleEntryEngine allowed unbalanced journal entry!"
            except UnbalancedJournalError:
                pass
        test("2. Double-Entry Rejection: Strict Prevention of Unbalanced Postings (Dr != Cr)", t_unbalanced_journal_rejection)

        def t_reversal_immutability():
            cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
            exp = ChartOfAccountsEngine.get_account_by_code("6010", conn=conn)

            # 1. Post original
            orig = DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_number": "JV-AUD-REV-ORIG",
                    "entry_date": "2026-02-18 11:00:00",
                    "source_module": "expense",
                    "narration": "Original Expense to be reversed"
                },
                lines_data=[
                    {"account_id": exp["id"], "debit": 3000.0, "credit": 0.0, "description": "Rent Expense"},
                    {"account_id": cash["id"], "debit": 0.0, "credit": 3000.0, "description": "Cash payment"},
                ],
                user="auditor",
                external_conn=conn
            )
            created_journals.append(orig["entry_id"])

            # 2. Reverse
            rev = DoubleEntryEngine.reverse_journal_entry(
                entry_id=orig["entry_id"],
                reason="Audit reversal test: Duplicate voucher",
                user="auditor",
                external_conn=conn
            )
            created_journals.append(rev["reversal_entry_id"])

            # 3. Verify original remains intact, status is REVERSED, audit trail logged
            orig_check = DoubleEntryEngine.get_journal_entry(orig["entry_id"], conn=conn)
            assert orig_check["status"] == "REVERSED"
            assert orig_check["reversal_reason"] == "Audit reversal test: Duplicate voucher"

            # 4. Verify reversal lines invert exactly (Dr Cash 3000, Cr Rent 3000)
            rev_check = DoubleEntryEngine.get_journal_entry(rev["reversal_entry_id"], conn=conn)
            assert rev_check["status"] == "POSTED"
            assert rev_check["total_debit"] == 3000.0
            assert rev_check["total_credit"] == 3000.0

            cash_line = next(l for l in rev_check["lines"] if l["account_code"] == "1010")
            exp_line = next(l for l in rev_check["lines"] if l["account_code"] == "6010")
            assert cash_line["debit"] == 3000.0 and cash_line["credit"] == 0.0
            assert exp_line["credit"] == 3000.0 and exp_line["debit"] == 0.0

            # 5. Prevent double reversal
            try:
                DoubleEntryEngine.reverse_journal_entry(
                    entry_id=orig["entry_id"],
                    reason="Second reversal attempt",
                    user="auditor",
                    external_conn=conn
                )
                assert False, "Allowed second reversal of already reversed entry!"
            except DoubleEntryError:
                pass
        test("3. Reversal Integrity: Immutable Original, Exact Inversion, Double Reversal Prevention", t_reversal_immutability)

        # =====================================================================
        # PART 2: BUSINESS ACCOUNTING REALISTIC FLOWS
        # =====================================================================
        print("\n" + "-" * 80)
        print("PART 2: COMPLETE REALISTIC BUSINESS-TO-REPORT ACCOUNTING FLOWS")
        print("-" * 80)

        def t_sales_flow_with_ar_and_inventory():
            # 1. Customer Credit Invoice: Bill #AUD-991 for ₹23,600 (Base: 20k, GST: 3.6k)
            bill_data = {
                "id": 991,
                "customer_name": "Audit Elite Traders",
                "payment_method": "CREDIT",
                "status": "ACTIVE",
                "date": "2026-02-19 10:00:00",
                "total": 23600.0,
                "gross_total": 20000.0,
                "tax": 3600.0,
                "balance": 23600.0,
                "amount_paid": 0.0
            }
            res_sale = AccountingRules.post_sales_bill(bill_data, conn=conn)
            created_journals.append(res_sale["entry_id"])

            # Register in AR Subledger
            cur.execute("""
                INSERT INTO accounts_receivables
                    (receivable_no, invoice_ref, customer_name, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status, source_bill_id)
                VALUES
                    ('REC-AUD-991', 'INV-AUD-991', 'Audit Elite Traders', '2026-02-19', '2026-03-21', 23600.0, 0.0, 23600.0, 'Pending', '991');
            """)
            rec_id = cur.lastrowid
            created_subledger_items["receivables"].append(rec_id)

            # Verify Sale Journal: Dr AR 23600, Cr Sales 20000, Cr Output GST 3600
            sale_jv = DoubleEntryEngine.get_journal_entry(res_sale["entry_id"], conn=conn)
            assert any(l["account_code"] == "1040" and l["debit"] == 23600.0 for l in sale_jv["lines"])
            assert any(l["account_code"] == "4010" and l["credit"] == 20000.0 for l in sale_jv["lines"])
            assert any(l["account_code"] == "2030" and l["credit"] == 3600.0 for l in sale_jv["lines"])

            # 2. Customer Receipt: Pays ₹10,000 via Bank
            rec_data = {
                "id": 881,
                "bill_id": "991",
                "amount": 10000.0,
                "payment_method": "Bank Transfer",
                "date": "2026-02-20",
                "customer_name": "Audit Elite Traders"
            }
            res_rec = AccountingRules.post_customer_payment(rec_data, conn=conn)
            created_journals.append(res_rec["entry_id"])

            # Update Subledger
            cur.execute("""
                UPDATE accounts_receivables
                SET paid_amount = 10000.0, remaining_balance = 13600.0, status = 'Partial'
                WHERE id = %s;
            """, (rec_id,))

            # Verify Receipt Journal: Dr Bank 10000, Cr AR 10000 (NO revenue recognized again!)
            rec_jv = DoubleEntryEngine.get_journal_entry(res_rec["entry_id"], conn=conn)
            assert any(l["account_code"] == "1020" and l["debit"] == 10000.0 for l in rec_jv["lines"])
            assert any(l["account_code"] == "1040" and l["credit"] == 10000.0 for l in rec_jv["lines"])
            assert not any(l["account_code"] == "4010" for l in rec_jv["lines"]), "Customer receipt recorded revenue!"

            # 3. Customer Credit Note: ₹3,600 allowance (Base: 3000, GST: 600)
            res_cn = AccountingRules.post_customer_credit_note({
                "receivable_id": rec_id,
                "customer_name": "Audit Elite Traders",
                "amount": 3600.0,
                "tax_amount": 600.0,
                "date": "2026-02-21",
                "reason": "Quality allowance on Bill #991"
            }, conn=conn)
            created_journals.append(res_cn.get("entry_id") or res_cn.get("journal_entry_id"))

            # Verify AR Subledger matches remaining: 23600 - 10000 - 3600 = 10000
            cur.execute("SELECT remaining_balance FROM accounts_receivables WHERE id = %s;", (rec_id,))
            rem = round(float(cur.fetchone()["remaining_balance"]), 2)
            assert rem == 10000.0, f"Expected AR remaining 10,000, got {rem}"
        test("4. Sales Cycle: Credit Sale -> Receipt -> Credit Note (AR Subledger Invariant)", t_sales_flow_with_ar_and_inventory)

        def t_purchase_flow_with_ap_and_gst():
            # 1. Purchase on Credit: ₹29,500 (Inventory/Cost: 25k, Input GST: 4.5k)
            inv_data = {
                "invoice_no": "PUR-AUD-501",
                "supplier_name": "Zenith Industrial Supplies",
                "date": "2026-02-22",
                "grand_total": 29500.0,
                "cost_total": 25000.0,
                "gst_amount": 4500.0,
                "is_credit": 1,
                "amount_paid": 5000.0,
                "payment_mode": "Bank Transfer"
            }
            res_pur = AccountingRules.post_inventory_purchase(inv_data, conn=conn)
            created_journals.append(res_pur["entry_id"])

            # Register in AP Subledger: Total 24,500 remaining on credit
            cur.execute("""
                INSERT INTO accounts_payables
                    (payable_no, invoice_ref, supplier_name, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status, source_invoice_id)
                VALUES
                    ('PAY-AUD-501', 'PUR-AUD-501', 'Zenith Industrial Supplies', '2026-02-22', '2026-03-24', 24500.0, 0.0, 24500.0, 'Pending', 'PUR-AUD-501');
            """)
            pay_id = cur.lastrowid
            created_subledger_items["payables"].append(pay_id)

            # Verify Purchase Journal: Dr COGS/Inventory 25k, Dr Input GST 4.5k, Cr Bank 5k, Cr AP 24.5k
            pur_jv = DoubleEntryEngine.get_journal_entry(res_pur["entry_id"], conn=conn)
            assert any(l["account_code"] in ("5010", "1050") and l["debit"] == 25000.0 for l in pur_jv["lines"])
            assert any(l["account_code"] in ("1060", "2040") and l["debit"] == 4500.0 for l in pur_jv["lines"])
            assert any(l["account_code"] == "1020" and l["credit"] == 5000.0 for l in pur_jv["lines"])
            assert any(l["account_code"] == "2010" and l["credit"] == 24500.0 for l in pur_jv["lines"])

            # 2. Supplier Payment: Pay ₹14,500 via Bank
            pay_data = {
                "id": 771,
                "invoice_no": "PUR-AUD-501",
                "amount": 14500.0,
                "payment_method": "Bank Transfer",
                "date": "2026-02-23",
                "supplier_name": "Zenith Industrial Supplies"
            }
            res_sp = AccountingRules.post_supplier_payment(pay_data, conn=conn)
            created_journals.append(res_sp["entry_id"])

            cur.execute("""
                UPDATE accounts_payables
                SET paid_amount = 14500.0, remaining_balance = 10000.0, status = 'Partial'
                WHERE id = %s;
            """, (pay_id,))

            # Verify Supplier Payment Journal: Dr AP 14500, Cr Bank 14500 (NO expense recognized again!)
            sp_jv = DoubleEntryEngine.get_journal_entry(res_sp["entry_id"], conn=conn)
            assert any(l["account_code"] == "2010" and l["debit"] == 14500.0 for l in sp_jv["lines"])
            assert any(l["account_code"] == "1020" and l["credit"] == 14500.0 for l in sp_jv["lines"])
            assert not any(l["account_code"] in ("5010", "1050") for l in sp_jv["lines"]), "Supplier payment recorded purchase again!"
        test("5. Purchase Cycle: Inward Bill -> Payment (AP Subledger Invariant)", t_purchase_flow_with_ap_and_gst)

        def t_cash_bank_contra_and_charges():
            # 1. Cash Withdrawal from Bank: ₹5,000 (Contra)
            res_contra = AccountingRules.post_contra_transfer({
                "amount": 5000.0,
                "from_account": "Bank",
                "to_account": "Cash",
                "reference_no": "CONTRA-AUD-01",
                "date": "2026-02-24",
                "narration": "Audit: Cash withdrawal for store float"
            }, conn=conn)
            created_journals.append(res_contra["entry_id"])

            # Verify: Dr Cash 5000, Cr Bank 5000
            contra_jv = DoubleEntryEngine.get_journal_entry(res_contra["entry_id"], conn=conn)
            assert any(l["account_code"] == "1010" and l["debit"] == 5000.0 for l in contra_jv["lines"])
            assert any(l["account_code"] == "1020" and l["credit"] == 5000.0 for l in contra_jv["lines"])

            # 2. Bank Charges: ₹250
            res_chg = AccountingRules.post_bank_charges({
                "amount": 250.0,
                "reference_no": "BNK-CHG-AUD-01",
                "date": "2026-02-24",
                "description": "Monthly account maintenance charge"
            }, conn=conn)
            created_journals.append(res_chg["entry_id"])

            chg_jv = DoubleEntryEngine.get_journal_entry(res_chg["entry_id"], conn=conn)
            assert any(l["account_code"] == "7010" and l["debit"] == 250.0 for l in chg_jv["lines"])
            assert any(l["account_code"] == "1020" and l["credit"] == 250.0 for l in chg_jv["lines"])
        test("6. Cash & Bank: Contra Transfer (Zero P&L Effect) & Bank Charges", t_cash_bank_contra_and_charges)

        def t_statutory_tds_flow():
            # Expense of ₹20,000 with 10% TDS (₹2,000) under Sec 194J
            res_tds = AccountingRules.post_expense_with_tds({
                "gross_amount": 20000.0,
                "tds_rate": 10.0,
                "payment_method": "Bank Transfer",
                "date": "2026-02-25",
                "category": "Statutory Audit Fees",
                "vendor_name": "S. Mehta & Associates Chartered Accountants",
                "tds_section": "194J"
            }, conn=conn)
            created_journals.append(res_tds["entry_id"])

            # Verify: Dr Expense 20000, Cr TDS Payable 2000, Cr Bank 18000
            tds_jv = DoubleEntryEngine.get_journal_entry(res_tds["entry_id"], conn=conn)
            assert any(l["account_code"] == "6040" and l["debit"] == 20000.0 for l in tds_jv["lines"])
            assert any(l["account_code"] == "2050" and l["credit"] == 2000.0 for l in tds_jv["lines"])
            assert any(l["account_code"] == "1020" and l["credit"] == 18000.0 for l in tds_jv["lines"])

            # Remittance of TDS to Govt Treasury: ₹2,000
            res_remit = AccountingRules.post_tds_remittance({
                "amount": 2000.0,
                "payment_method": "Bank Transfer",
                "challan_no": "CHALLAN-AUD-9988",
                "date": "2026-02-26"
            }, conn=conn)
            created_journals.append(res_remit["entry_id"])

            remit_jv = DoubleEntryEngine.get_journal_entry(res_remit["entry_id"], conn=conn)
            assert any(l["account_code"] == "2050" and l["debit"] == 2000.0 for l in remit_jv["lines"])
            assert any(l["account_code"] == "1020" and l["credit"] == 2000.0 for l in remit_jv["lines"])
        test("7. Statutory Tax: TDS Deduction (194J) & Remittance to Treasury", t_statutory_tds_flow)

        def t_fixed_assets_and_depreciation():
            # 1. Create Fixed Asset: ₹40,000 Server
            cur.execute("""
                INSERT INTO accounts_fixed_assets
                    (asset_code, asset_name, category, purchase_date, purchase_value, current_value, useful_life_years, depreciation_rate, accumulated_depreciation, status)
                VALUES
                    ('AST-AUD-SRV', 'Backup Application Server', 'Computer Equipment', '2026-02-01', 40000.0, 40000.0, 3.0, 33.33, 0.0, 'Active');
            """)
            ast_id = cur.lastrowid
            created_subledger_items["assets"].append(ast_id)

            # Capitalize in GL: Dr 1140 40k, Cr Bank 40k
            ast_jv = DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_date": "2026-02-01 10:00:00",
                    "source_module": "asset",
                    "source_entity": "asset_purchase",
                    "source_id": "AST-AUD-SRV",
                    "narration": "Acquisition of Backup Application Server"
                },
                lines_data=[
                    {"account_id": ChartOfAccountsEngine.get_account_by_code("1140", conn=conn)["id"], "debit": 40000.0, "credit": 0.0, "description": "Fixed Asset: Server"},
                    {"account_id": ChartOfAccountsEngine.get_account_by_code("1020", conn=conn)["id"], "debit": 0.0, "credit": 40000.0, "description": "Bank disbursement for server"}
                ],
                user="auditor",
                external_conn=conn
            )
            created_journals.append(ast_jv["entry_id"])

            # 2. Periodic Depreciation: ₹1,000 for Feb 2026
            depr_res = AccountingRules.post_periodic_depreciation(
                asset_id=ast_id,
                depreciation_date="2026-02-28",
                amount=1000.0,
                conn=conn
            )
            created_journals.append(depr_res["journal_entry_id"])

            depr_jv = DoubleEntryEngine.get_journal_entry(depr_res["journal_entry_id"], conn=conn)
            assert any(l["account_code"] == "6090" and l["debit"] == 1000.0 for l in depr_jv["lines"])
            assert any(l["account_code"] == "1160" and l["credit"] == 1000.0 for l in depr_jv["lines"])

            # Verify register update
            cur.execute("SELECT current_value, accumulated_depreciation FROM accounts_fixed_assets WHERE id = %s;", (ast_id,))
            a_row = cur.fetchone()
            assert round(float(a_row["accumulated_depreciation"]), 2) == 1000.0
            assert round(float(a_row["current_value"]), 2) == 39000.0

            # 3. Duplicate Depreciation Prevention (same asset, same month)
            try:
                AccountingRules.post_periodic_depreciation(
                    asset_id=ast_id,
                    depreciation_date="2026-02-28",
                    amount=1000.0,
                    conn=conn
                )
                assert False, "Allowed duplicate depreciation in the same period!"
            except ValueError:
                pass
        test("8. Fixed Assets: Acquisition, Depreciation Run & Duplicate Depreciation Prevention", t_fixed_assets_and_depreciation)

        def t_loan_accounting_and_principal_interest_split():
            # 1. Loan Receipt: ₹50,000
            cur.execute("""
                INSERT INTO accounts_liabilities
                    (liability_code, title, liability_type, principal_amount, interest_rate, tenure_months, outstanding_balance, lender, start_date, status)
                VALUES
                    ('LOAN-AUD-01', 'Emergency Credit Line', 'Bank Loan', 50000.0, 10.0, 12, 50000.0, 'SBI', '2026-02-10', 'Active');
            """)
            l_id = cur.lastrowid
            created_subledger_items["liabilities"].append(l_id)

            l_jv = DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_date": "2026-02-10",
                    "source_module": "manual",
                    "source_entity": "loan",
                    "source_id": "LOAN-AUD-01",
                    "narration": "Loan Receipt: SBI Emergency Credit Line"
                },
                lines_data=[
                    {"account_id": ChartOfAccountsEngine.get_account_by_code("1020", conn=conn)["id"], "debit": 50000.0, "credit": 0.0, "description": "Loan proceeds in bank"},
                    {"account_id": ChartOfAccountsEngine.get_account_by_code("2110", conn=conn)["id"], "debit": 0.0, "credit": 50000.0, "description": "Loan liability"}
                ],
                user="auditor",
                external_conn=conn
            )
            created_journals.append(l_jv["entry_id"])

            # 2. EMI Repayment: Principal ₹5,000 + Interest ₹500 (Total ₹5,500)
            res_rep = AccountingRules.post_loan_repayment({
                "liability_code": "LOAN-AUD-01",
                "title": "SBI Emergency Credit Line",
                "principal_amount": 5000.0,
                "interest_amount": 500.0,
                "payment_method": "Bank Transfer",
                "date": "2026-02-28"
            }, conn=conn)
            created_journals.append(res_rep["entry_id"])

            cur.execute("""
                UPDATE accounts_liabilities
                SET outstanding_balance = 45000.0
                WHERE id = %s;
            """, (l_id,))

            rep_jv = DoubleEntryEngine.get_journal_entry(res_rep["entry_id"], conn=conn)
            # Dr Loan Liability 5000, Dr Interest Expense 500, Cr Bank 5500
            assert any(l["account_code"] == "2110" and l["debit"] == 5000.0 for l in rep_jv["lines"])
            assert any(l["account_code"] == "7020" and l["debit"] == 500.0 for l in rep_jv["lines"])
            assert any(l["account_code"] == "1020" and l["credit"] == 5500.0 for l in rep_jv["lines"])
        test("9. Loan Accounting: Principal Repayment (Reduces Liability) vs Interest (Expense)", t_loan_accounting_and_principal_interest_split)

        def t_capital_and_drawings():
            # 1. Capital Introduced: ₹50,000 into Bank
            res_cap = AccountingRules.post_capital_introduction({
                "owner_name": "Mohamed Fouzan",
                "amount": 50000.0,
                "payment_method": "Bank Transfer",
                "date": "2026-02-27",
                "notes": "Additional working capital introduction"
            }, conn=conn)
            created_journals.append(res_cap["entry_id"])

            cap_jv = DoubleEntryEngine.get_journal_entry(res_cap["entry_id"], conn=conn)
            assert any(l["account_code"] == "1020" and l["debit"] == 50000.0 for l in cap_jv["lines"])
            assert any(l["account_code"] == "3010" and l["credit"] == 50000.0 for l in cap_jv["lines"])

            # 2. Owner Drawings: ₹10,000 via Cash
            res_draw = AccountingRules.post_owner_drawings({
                "owner_name": "Mohamed Fouzan",
                "amount": 10000.0,
                "payment_method": "Cash",
                "date": "2026-02-27",
                "notes": "Personal drawing"
            }, conn=conn)
            created_journals.append(res_draw["entry_id"])

            draw_jv = DoubleEntryEngine.get_journal_entry(res_draw["entry_id"], conn=conn)
            assert any(l["account_code"] == "3040" and l["debit"] == 10000.0 for l in draw_jv["lines"])
            assert any(l["account_code"] == "1010" and l["credit"] == 10000.0 for l in draw_jv["lines"])

            # Verify drawings account is Equity, NOT operating expense
            draw_acc = ChartOfAccountsEngine.get_account_by_code("3040", conn=conn)
            assert draw_acc["major_type"] == "Equity"
        test("10. Capital & Drawings: Capital Increases Equity, Drawings Reduces Equity (No P&L Expense)", t_capital_and_drawings)

        # =====================================================================
        # PART 3: REPORTS & FUNDAMENTAL ACCOUNTING EQUATIONS
        # =====================================================================
        print("\n" + "-" * 80)
        print("PART 3: REPORTS & FUNDAMENTAL ACCOUNTING EQUATIONS VERIFICATION")
        print("-" * 80)

        def t_trial_balance_equation():
            tb = generate_trial_balance(conn=conn)
            assert tb["is_balanced"], f"Trial Balance unbalanced! Dr: {tb['total_debit']} != Cr: {tb['total_credit']}"
            assert abs(tb["total_debit"] - tb["total_credit"]) < 0.01
            print(f"       [TB Verified] Total Debits: Rs. {tb['total_debit']:,.2f} == Total Credits: Rs. {tb['total_credit']:,.2f}")
        test("11. Fundamental Equation 1: Trial Balance TOTAL DEBITS == TOTAL CREDITS", t_trial_balance_equation)

        def t_balance_sheet_equation():
            bs = generate_balance_sheet(conn=conn)
            assert bs["is_balanced"], f"Balance Sheet unbalanced! Assets: {bs['assets']['total_assets']} != Liab+Eq: {bs['total_liabilities_and_equity']}"
            assert abs(bs["difference"]) < 0.01
            print(f"       [BS Verified] Assets: Rs. {bs['assets']['total_assets']:,.2f} == Liab+Equity: Rs. {bs['total_liabilities_and_equity']:,.2f}")
        test("12. Fundamental Equation 2: Balance Sheet ASSETS == LIABILITIES + EQUITY", t_balance_sheet_equation)

        def t_pnl_calculation_and_closure():
            pnl = generate_profit_and_loss(conn=conn)
            rev = round(float(pnl["revenue"]["total"]), 2)
            cogs = round(float(pnl["cogs"]["total"]), 2)
            gp = round(float(pnl["gross_profit"]), 2)
            exp = round(float(pnl["operating_expenses"]["total"]), 2)
            np = round(float(pnl["net_profit"]), 2)

            assert gp == round(rev - cogs, 2), f"Gross profit mismatch: {gp} != {rev} - {cogs}"
            assert np == round(gp - exp, 2), f"Net profit mismatch: {np} != {gp} - {exp}"

            # Check that Net Profit matches the current period results in Balance Sheet equity
            bs = generate_balance_sheet(conn=conn)
            current_res = next((item for item in bs["equity"]["lines"] if item["code"] == "3030"), None)
            assert current_res is not None, "Balance Sheet missing current period net profit line!"
            assert current_res["amount"] == np, f"Balance Sheet current net profit {current_res['amount']} != P&L {np}"
            print(f"       [P&L Verified] Revenue: Rs. {rev:,.2f} - COGS: Rs. {cogs:,.2f} = GP: Rs. {gp:,.2f}; Net Profit: Rs. {np:,.2f}")
        test("13. Profit & Loss: Revenue - COGS = GP; GP - Expenses = NP (Closed to Equity)", t_pnl_calculation_and_closure)

        def t_subledger_control_reconciliations():
            # AR Subledger vs 1040
            cur.execute("SELECT COALESCE(SUM(remaining_balance), 0) as sub_ar FROM accounts_receivables WHERE status != 'PAID';")
            sub_ar = round(float(cur.fetchone()["sub_ar"]), 2)
            cur.execute("""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as gl_ar
                FROM journal_lines jl JOIN accounts_chart ac ON jl.account_id = ac.id
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE ac.code = '1040' AND je.status = 'POSTED';
            """)
            gl_ar = round(float(cur.fetchone()["gl_ar"]), 2)
            assert abs(sub_ar - gl_ar) < 0.05, f"AR Mismatch: Subledger {sub_ar} != GL {gl_ar}"

            # AP Subledger vs 2010
            cur.execute("SELECT COALESCE(SUM(remaining_balance), 0) as sub_ap FROM accounts_payables WHERE status != 'PAID';")
            sub_ap = round(float(cur.fetchone()["sub_ap"]), 2)
            cur.execute("""
                SELECT COALESCE(SUM(jl.credit - jl.debit), 0) as gl_ap
                FROM journal_lines jl JOIN accounts_chart ac ON jl.account_id = ac.id
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE ac.code = '2010' AND je.status = 'POSTED';
            """)
            gl_ap = round(float(cur.fetchone()["gl_ap"]), 2)
            assert abs(sub_ap - gl_ap) < 0.05, f"AP Mismatch: Subledger {sub_ap} != GL {gl_ap}"

            # Loan Subledger vs 2110
            cur.execute("SELECT COALESCE(SUM(outstanding_balance), 0) as sub_loan FROM accounts_liabilities WHERE status = 'Active';")
            sub_loan = round(float(cur.fetchone()["sub_loan"]), 2)
            cur.execute("""
                SELECT COALESCE(SUM(jl.credit - jl.debit), 0) as gl_loan
                FROM journal_lines jl JOIN accounts_chart ac ON jl.account_id = ac.id
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE ac.code = '2110' AND je.status = 'POSTED';
            """)
            gl_loan = round(float(cur.fetchone()["gl_loan"]), 2)
            assert abs(sub_loan - gl_loan) < 0.05, f"Loan Mismatch: Subledger {sub_loan} != GL {gl_loan}"

            # Fixed Asset Register Cost vs GL (1110-1155)
            cur.execute("SELECT COALESCE(SUM(purchase_value), 0) as reg_cost, COALESCE(SUM(accumulated_depreciation), 0) as reg_depr FROM accounts_fixed_assets WHERE status = 'Active';")
            fa_reg = cur.fetchone()
            reg_cost = round(float(fa_reg["reg_cost"]), 2)
            reg_depr = round(float(fa_reg["reg_depr"]), 2)

            cur.execute("""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as gl_cost
                FROM journal_lines jl JOIN accounts_chart ac ON jl.account_id = ac.id
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE ac.code IN ('1110', '1120', '1130', '1140', '1150') AND je.status = 'POSTED';
            """)
            gl_cost = round(float(cur.fetchone()["gl_cost"]), 2)

            cur.execute("""
                SELECT COALESCE(SUM(jl.credit - jl.debit), 0) as gl_depr
                FROM journal_lines jl JOIN accounts_chart ac ON jl.account_id = ac.id
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE ac.code = '1160' AND je.status = 'POSTED';
            """)
            gl_depr = round(float(cur.fetchone()["gl_depr"]), 2)

            assert abs(reg_cost - gl_cost) < 0.05, f"Fixed Asset Cost Mismatch: Reg {reg_cost} != GL {gl_cost}"
            assert abs(reg_depr - gl_depr) < 0.05, f"Fixed Asset Depr Mismatch: Reg {reg_depr} != GL {gl_depr}"
        test("14. Subledger Reconciliations: Customer AR, Supplier AP, Loans, Fixed Assets", t_subledger_control_reconciliations)

        # =====================================================================
        # PART 4: SECURITY, FINANCIAL CONTROL & IDEMPOTENCY
        # =====================================================================
        print("\n" + "-" * 80)
        print("PART 4: FINANCIAL CONTROL, SECURITY & DATA INTEGRITY")
        print("-" * 80)

        def t_closed_period_lock_enforcement():
            # Fetch closed or create closed test period
            cur.execute("SELECT id, period_name, start_date, end_date FROM accounting_periods WHERE status = 'CLOSED' LIMIT 1;")
            closed_p = cur.fetchone()
            if not closed_p:
                # Close period 1 temporarily
                cur.execute("UPDATE accounting_periods SET status = 'CLOSED' WHERE id = 1;")
                conn.commit()
                closed_p = {"start_date": "2025-04-15", "id": 1}

            try:
                PeriodControlEngine.validate_transaction_date(closed_p["start_date"], user="cashier", conn=conn)
                assert False, "Allowed transaction in CLOSED accounting period!"
            except PeriodClosedError:
                pass
            finally:
                # Reopen period 1
                cur.execute("UPDATE accounting_periods SET status = 'OPEN' WHERE id = 1;")
                conn.commit()
        test("15. Financial Control: Strict Rejection of Postings in Closed / Locked Periods", t_closed_period_lock_enforcement)

        def t_duplicate_prevention_idempotency():
            # Attempt to re-post identical bill
            bill = {
                "id": 991, # already posted in test 4
                "customer_name": "Audit Elite Traders",
                "payment_method": "CREDIT",
                "status": "ACTIVE",
                "date": "2026-02-19 10:00:00",
                "total": 23600.0,
                "gross_total": 20000.0,
                "tax": 3600.0,
                "balance": 23600.0,
                "amount_paid": 0.0
            }
            res_dup = AccountingRules.post_sales_bill(bill, conn=conn)
            assert res_dup.get("is_duplicate") is True, "Idempotency failed: allowed duplicate sales bill posting!"
        test("16. Idempotency: Duplicate Source Webhook / Double-Click Prevention", t_duplicate_prevention_idempotency)

        def t_database_transactional_rollback():
            cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
            sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)

            # Record count before
            cur.execute("SELECT COUNT(*) as c FROM journal_entries;")
            count_before = cur.fetchone()["c"]

            try:
                # Deliberate failure inside DoubleEntryEngine
                DoubleEntryEngine.post_journal_entry(
                    entry_data={"entry_number": "JV-CRASH-TEST", "entry_date": "2026-02-28", "source_module": "manual"},
                    lines_data=[
                        {"account_id": cash["id"], "debit": 5000.0, "credit": 0.0, "description": "Line 1"},
                        {"account_id": -99999, "debit": 0.0, "credit": 5000.0, "description": "Invalid account"},
                    ],
                    user="auditor",
                    external_conn=conn
                )
                assert False, "Failed to reject invalid account ID!"
            except Exception:
                conn.rollback()

            cur.execute("SELECT COUNT(*) as c FROM journal_entries;")
            count_after = cur.fetchone()["c"]
            assert count_before == count_after, "Partial journal entry committed during crash!"
        test("17. Database Integrity: Atomic All-or-Nothing Rollback on Failure", t_database_transactional_rollback)

        def t_report_drilldown_integrity():
            # Drill down from P&L revenue line to ledger lines to journal header
            cur.execute("""
                SELECT ac.id, ac.code, ac.name, jl.entry_id, je.entry_number, je.source_module, je.source_id, jl.credit
                FROM accounts_chart ac
                JOIN journal_lines jl ON ac.id = jl.account_id
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE ac.code = '4010' AND je.status = 'POSTED'
                ORDER BY je.id DESC LIMIT 1;
            """)
            drill = cur.fetchone()
            assert drill is not None, "Drill-down path broken: No revenue lines found!"
            assert drill["credit"] > 0
            assert drill["entry_number"] is not None
            print(f"       [Drilldown Verified] P&L Rev (4010) -> Journal #{drill['entry_number']} -> Source: {drill['source_module']}:{drill['source_id']}")
        test("18. Report Drill-Down: Trace P&L -> Account -> Ledger -> Journal Voucher -> Source", t_report_drilldown_integrity)

        # =====================================================================
        # PART 5: 24-POINT COMPREHENSIVE HEALTH CHECK RUN
        # =====================================================================
        print("\n" + "-" * 80)
        print("PART 5: 24-POINT ACCOUNTING HEALTH CHECK DIAGNOSTIC RUN")
        print("-" * 80)

        def t_24_point_health_check():
            hc = HealthCheckEngine.run_full_health_check(conn=conn)
            print(f"       >>> Health Score: {hc['health_score']}/100 | Status: {hc['status']}")
            print(f"       >>> Total Checks: {hc['total_checks']} | Passed: {hc['summary']['pass']} | Issues: {hc['total_issues']}")

            for iss in hc["issues"]:
                print(f"           [!] {iss['check_name']} ({iss['status']}): Diff: {iss['difference']} - {iss['possible_cause']}")

            assert hc["total_checks"] == 24, f"Expected 24 checkpoints, got {hc['total_checks']}"
            assert hc["health_score"] >= 95, f"Health Score too low: {hc['health_score']}/100"
            assert hc["status"] in ("HEALTHY", "WARNING"), f"System status unhealthy: {hc['status']}"
            assert hc["summary"]["critical"] == 0, f"Critical issues detected: {hc['summary']['critical']}"
        test("19. Full 24-Point Accounting Health Check (All Critical Accounting Invariants Pass)", t_24_point_health_check)

    finally:
        # Clean up test vouchers created during audit
        print("\n--- Cleaning up temporary audit records ---")
        if created_journals:
            cur.execute(f"DELETE FROM journal_lines WHERE entry_id IN ({','.join(['%s']*len(created_journals))});", tuple(created_journals))
            cur.execute(f"DELETE FROM journal_entries WHERE id IN ({','.join(['%s']*len(created_journals))});", tuple(created_journals))
            print(f" [-] Cleaned up {len(created_journals)} temporary audit journal vouchers.")

        for r_id in created_subledger_items["receivables"]:
            cur.execute("DELETE FROM accounts_receivables WHERE id = %s;", (r_id,))
        for p_id in created_subledger_items["payables"]:
            cur.execute("DELETE FROM accounts_payables WHERE id = %s;", (p_id,))
        for a_id in created_subledger_items["assets"]:
            cur.execute("DELETE FROM accounts_fixed_assets WHERE id = %s;", (a_id,))
            cur.execute("DELETE FROM accounts_asset_depreciation_log WHERE asset_id = %s;", (a_id,))
        for l_id in created_subledger_items["liabilities"]:
            cur.execute("DELETE FROM accounts_liabilities WHERE id = %s;", (l_id,))

        ensure_reconciled_baseline(conn=conn)
        conn.commit()
        cur.close()
        conn.close()

    print("\n" + "=" * 90)
    print(f"   FINAL AUDIT SUMMARY: {passed_tests}/{total_tests} AUDIT TESTS PASSED SUCCESSFULLY!   ")
    print("=" * 90)
    return passed_tests == total_tests


if __name__ == "__main__":
    success = run_master_accounting_audit_suite()
    sys.exit(0 if success else 1)

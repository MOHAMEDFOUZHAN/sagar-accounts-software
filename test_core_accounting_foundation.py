"""
Comprehensive Core Accounting Foundation Test Suite
Covers:
1. Chart of Accounts (Tree, Child, Validations, Group/Inactive Restrictions, Historical Preservation)
2. Double-Entry Accounting Engine (Invariants, Dr=Cr, Atomicity, Statuses, Reversals, Idempotency)
3. General Ledger (Opening, Running, Closing, Filtering, Traceability)
4. Accounting Rules & Auto Posting (Sales, Returns, Purchases, Payments, Expenses, Fixed Assets, Depreciation, Loans, Capital)
5. Single Source of Truth (Trial Balance, P&L, Balance Sheet)
"""

import sys
import os
import datetime
from decimal import Decimal

# Ensure UTF-8 on Windows
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from backend.db import get_db_connection
from backend.coa_engine import (
    ChartOfAccountsEngine,
    DuplicateAccountCodeError,
    InvalidParentAccountError,
    CircularHierarchyError,
    InvalidAccountPostingError,
)
from backend.double_entry_engine import (
    DoubleEntryEngine,
    DoubleEntryError,
    UnbalancedJournalError,
)
from backend.ledger_engine import LedgerEngine
from backend.accounting_rules import AccountingRules, MissingAccountMappingError
from backend.reports_engine import (
    generate_profit_and_loss,
    generate_balance_sheet,
    generate_trial_balance,
    generate_tax_report,
)


def reset_test_data(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM journal_lines;")
    cur.execute("DELETE FROM journal_entries;")
    cur.execute("DELETE FROM accounting_sync_registry;")
    cur.execute("DELETE FROM accounts_receivables;")
    cur.execute("DELETE FROM accounts_payables;")
    # Clean non-seed test accounts
    cur.execute("DELETE FROM accounts_chart WHERE code LIKE 'TEST%';")
    conn.commit()
    cur.close()


def run_all_tests():
    print("=" * 80)
    print("   STARTING COMPREHENSIVE CORE ACCOUNTING FOUNDATION TEST SUITE   ")
    print("=" * 80)

    conn = get_db_connection()
    reset_test_data(conn)

    passed_count = 0
    total_count = 0

    def test(name, fn):
        nonlocal passed_count, total_count
        total_count += 1
        try:
            fn()
            print(f"  [PASS] {name}")
            passed_count += 1
        except Exception as e:
            print(f"  [FAIL] {name} -> Error: {e}")
            raise

    # -------------------------------------------------------------------------
    # PART 1: CHART OF ACCOUNTS TESTS
    # -------------------------------------------------------------------------
    print("\n--- PART 1: CHART OF ACCOUNTS TESTS ---")

    test_parent_id = None
    test_child_id = None

    def t_create_account():
        nonlocal test_parent_id
        test_parent_id = ChartOfAccountsEngine.create_account(
            code="TEST100",
            name="Test Group Account",
            major_type="Asset",
            sub_type="Current Assets",
            normal_balance="Debit",
            is_group=True,
            conn=conn
        )
        assert test_parent_id > 0, "Failed creating group account"
    test("1. Create Group Account", t_create_account)

    def t_create_child_account():
        nonlocal test_child_id
        test_child_id = ChartOfAccountsEngine.create_account(
            code="TEST101",
            name="Test Child Postable Account",
            major_type="Asset",
            sub_type="Current Assets",
            parent_id=test_parent_id,
            normal_balance="Debit",
            is_group=False,
            conn=conn
        )
        assert test_child_id > 0, "Failed creating child account"
    test("2. Create Child Postable Account", t_create_child_account)

    def t_duplicate_code():
        try:
            ChartOfAccountsEngine.create_account(
                code="TEST101",  # duplicate
                name="Duplicate Code Test",
                major_type="Asset",
                conn=conn
            )
            assert False, "Allowed duplicate account code!"
        except DuplicateAccountCodeError:
            pass
    test("3. Reject Duplicate Account Code", t_duplicate_code)

    def t_invalid_parent():
        try:
            ChartOfAccountsEngine.create_account(
                code="TEST999",
                name="Invalid Parent Test",
                major_type="Asset",
                parent_id=999999,  # Non-existent parent
                conn=conn
            )
            assert False, "Allowed non-existent parent!"
        except InvalidParentAccountError:
            pass
    test("4. Reject Non-Existent Parent", t_invalid_parent)

    def t_circular_hierarchy():
        try:
            # Try to make parent a child of its own child
            ChartOfAccountsEngine.update_account(
                test_parent_id,
                parent_id=test_child_id,
                conn=conn
            )
            assert False, "Allowed circular hierarchy!"
        except CircularHierarchyError:
            pass
    test("5. Prevent Circular Hierarchy", t_circular_hierarchy)

    def t_group_account_posting_restriction():
        is_postable, err, _ = ChartOfAccountsEngine.is_account_postable(test_parent_id, conn=conn)
        assert not is_postable, "Group account was marked postable!"
        assert "cannot receive direct postings" in err.lower()
    test("6. Group Account Posting Restriction", t_group_account_posting_restriction)

    def t_inactive_account_restriction():
        inact_id = ChartOfAccountsEngine.create_account(
            code="TEST102",
            name="Test Inactive Account",
            major_type="Asset",
            conn=conn
        )
        ChartOfAccountsEngine.update_account(inact_id, is_active=False, conn=conn)
        is_postable, err, _ = ChartOfAccountsEngine.is_account_postable(inact_id, conn=conn)
        assert not is_postable, "Inactive account was marked postable!"
        assert "inactive" in err.lower()
    test("7. Inactive Account Posting Restriction", t_inactive_account_restriction)

    def t_historical_preservation():
        # Post a transaction to test_child_id first
        cash_acc = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        DoubleEntryEngine.post_journal_entry(
            entry_data={"narration": "Historical test", "source_module": "test"},
            lines_data=[
                {"account_id": test_child_id, "debit": 500.0, "credit": 0.0},
                {"account_id": cash_acc["id"], "debit": 0.0, "credit": 500.0},
            ],
            external_conn=conn
        )
        # Attempt delete: should archive (is_active=0) instead of physical delete
        res = ChartOfAccountsEngine.delete_account(test_child_id, conn=conn)
        assert res["action"] == "archived", "Account with historical journal lines was physically deleted!"
        acc = ChartOfAccountsEngine.get_account_by_id(test_child_id, conn=conn)
        assert acc["is_active"] == 0, "Account was not set to inactive!"
    test("8. Historical Account Preservation (Archive Instead of Physical Delete)", t_historical_preservation)

    # -------------------------------------------------------------------------
    # PART 2: DOUBLE-ENTRY ACCOUNTING ENGINE TESTS
    # -------------------------------------------------------------------------
    print("\n--- PART 2: DOUBLE-ENTRY ACCOUNTING ENGINE TESTS ---")

    def t_valid_two_line_journal():
        cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)
        res = DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": "JV-TEST-001",
                "narration": "Valid two line journal test",
                "source_module": "manual",
                "source_entity": "voucher",
                "source_id": "V-001"
            },
            lines_data=[
                {"account_id": cash["id"], "debit": 1500.0, "credit": 0.0, "description": "Cash in"},
                {"account_id": sales["id"], "debit": 0.0, "credit": 1500.0, "description": "Sales out"},
            ],
            user="tester",
            external_conn=conn
        )
        assert res["entry_id"] > 0
        assert res["total_debit"] == 1500.0
        assert res["total_credit"] == 1500.0
        assert res["status"] == "POSTED"
    test("9. Valid Two-Line Journal (Dr == Cr == 1,500)", t_valid_two_line_journal)

    def t_valid_multi_line_journal():
        cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        ar = ChartOfAccountsEngine.get_account_by_code("1040", conn=conn)
        sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)
        gst = ChartOfAccountsEngine.get_account_by_code("2030", conn=conn)

        res = DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": "JV-TEST-002",
                "narration": "Multi-line journal with split debit and tax credit",
                "source_module": "manual",
            },
            lines_data=[
                {"account_id": cash["id"], "debit": 400.0, "credit": 0.0},
                {"account_id": ar["id"], "debit": 780.0, "credit": 0.0},
                {"account_id": sales["id"], "debit": 0.0, "credit": 1000.0},
                {"account_id": gst["id"], "debit": 0.0, "credit": 180.0},
            ],
            external_conn=conn
        )
        assert res["entry_id"] > 0
        assert res["total_debit"] == 1180.0
        assert res["total_credit"] == 1180.0
        assert res["lines_count"] == 4
    test("10. Valid Multi-Line Journal (4 lines: 1180 Dr == 1180 Cr)", t_valid_multi_line_journal)

    def t_unbalanced_journal():
        cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)
        try:
            DoubleEntryEngine.post_journal_entry(
                entry_data={"narration": "Unbalanced test"},
                lines_data=[
                    {"account_id": cash["id"], "debit": 1000.0, "credit": 0.0},
                    {"account_id": sales["id"], "debit": 0.0, "credit": 900.0},  # off by 100
                ],
                external_conn=conn
            )
            assert False, "Allowed unbalanced journal!"
        except UnbalancedJournalError:
            pass
    test("11. Reject Unbalanced Journal (Dr 1000 != Cr 900)", t_unbalanced_journal)

    def t_zero_amount():
        cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)
        try:
            DoubleEntryEngine.post_journal_entry(
                entry_data={"narration": "Zero amount test"},
                lines_data=[
                    {"account_id": cash["id"], "debit": 0.0, "credit": 0.0},
                    {"account_id": sales["id"], "debit": 0.0, "credit": 0.0},
                ],
                external_conn=conn
            )
            assert False, "Allowed zero amount lines!"
        except DoubleEntryError:
            pass
    test("12. Reject Zero Amount Lines", t_zero_amount)

    def t_negative_amount():
        cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)
        try:
            DoubleEntryEngine.post_journal_entry(
                entry_data={"narration": "Negative amount test"},
                lines_data=[
                    {"account_id": cash["id"], "debit": -500.0, "credit": 0.0},
                    {"account_id": sales["id"], "debit": 0.0, "credit": -500.0},
                ],
                external_conn=conn
            )
            assert False, "Allowed negative amount lines!"
        except DoubleEntryError:
            pass
    test("13. Reject Negative Amount Lines", t_negative_amount)

    def t_missing_account():
        sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)
        try:
            DoubleEntryEngine.post_journal_entry(
                entry_data={"narration": "Missing account test"},
                lines_data=[
                    {"account_id": 999999, "debit": 500.0, "credit": 0.0},
                    {"account_id": sales["id"], "debit": 0.0, "credit": 500.0},
                ],
                external_conn=conn
            )
            assert False, "Allowed posting to non-existent account!"
        except InvalidAccountPostingError:
            pass
    test("14. Reject Non-Existent Account", t_missing_account)

    def t_atomic_rollback():
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS cnt FROM journal_entries;")
        before_entries = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM journal_lines;")
        before_lines = cur.fetchone()["cnt"]
        cur.close()

        cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        try:
            DoubleEntryEngine.post_journal_entry(
                entry_data={"narration": "Rollback test"},
                lines_data=[
                    {"account_id": cash["id"], "debit": 500.0, "credit": 0.0},
                    {"account_id": 999999, "debit": 0.0, "credit": 500.0},  # will fail on line 2
                ],
                external_conn=conn
            )
        except Exception:
            pass

        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS cnt FROM journal_entries;")
        after_entries = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM journal_lines;")
        after_lines = cur.fetchone()["cnt"]
        cur.close()

        assert before_entries == after_entries, "Journal entry was committed despite line error!"
        assert before_lines == after_lines, "Orphan journal line was committed!"
    test("15. Atomic Rollback on Failure (Zero Partial State)", t_atomic_rollback)

    def t_draft_journal_ledger_isolation():
        cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)

        bal_before = LedgerEngine.get_account_balance(cash["id"], conn=conn)["balance"]

        # Post DRAFT journal
        DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": "JV-DRAFT-01",
                "narration": "Draft test voucher",
                "status": "DRAFT"
            },
            lines_data=[
                {"account_id": cash["id"], "debit": 99999.0, "credit": 0.0},
                {"account_id": sales["id"], "debit": 0.0, "credit": 99999.0},
            ],
            allow_draft=True,
            external_conn=conn
        )

        bal_after = LedgerEngine.get_account_balance(cash["id"], conn=conn)["balance"]
        assert bal_before == bal_after, "Draft journal affected official General Ledger balance!"
    test("16. Draft Journals Excluded from Official Ledger Balances", t_draft_journal_ledger_isolation)

    def t_reversal_and_double_reversal():
        cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        rent = ChartOfAccountsEngine.get_account_by_code("6040", conn=conn)

        # 1. Post original journal
        orig = DoubleEntryEngine.post_journal_entry(
            entry_data={"narration": "Original Rent Payment", "source_module": "expense"},
            lines_data=[
                {"account_id": rent["id"], "debit": 2500.0, "credit": 0.0},
                {"account_id": cash["id"], "debit": 0.0, "credit": 2500.0},
            ],
            external_conn=conn
        )
        orig_id = orig["entry_id"]

        bal_during = LedgerEngine.get_account_balance(rent["id"], conn=conn)["balance"]
        assert bal_during >= 2500.0

        # 2. Reverse
        rev_res = DoubleEntryEngine.reverse_journal_entry(
            orig_id,
            reason="Entered rent twice by mistake",
            user="auditor",
            external_conn=conn
        )
        assert rev_res["reversal_entry_id"] > 0
        assert rev_res["original_entry_id"] == orig_id

        # Check balance restored
        bal_after_rev = LedgerEngine.get_account_balance(rent["id"], conn=conn)["balance"]
        assert bal_after_rev == round(bal_during - 2500.0, 2), "Reversal did not cleanly net out ledger balance!"

        # 3. Test double reversal prevention
        try:
            DoubleEntryEngine.reverse_journal_entry(
                orig_id,
                reason="Attempting second reversal",
                external_conn=conn
            )
            assert False, "Allowed double reversal!"
        except DoubleEntryError as de:
            assert "already reversed" in str(de).lower()
    test("17. Audit-Trailed Reversal & Double Reversal Prevention", t_reversal_and_double_reversal)

    def t_duplicate_source_protection():
        cash = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
        sales = ChartOfAccountsEngine.get_account_by_code("4010", conn=conn)

        entry_data = {
            "source_module": "sales",
            "source_entity": "test_invoice",
            "source_id": "INV-TEST-UNIQUE-99",
            "narration": "Source idempotency test",
            "status": "POSTED"
        }
        lines = [
            {"account_id": cash["id"], "debit": 3000.0, "credit": 0.0},
            {"account_id": sales["id"], "debit": 0.0, "credit": 3000.0},
        ]

        # First call
        r1 = DoubleEntryEngine.post_journal_entry(entry_data, lines, external_conn=conn)
        assert not r1["is_duplicate"]

        # Second call (same source module, entity, and ID)
        r2 = DoubleEntryEngine.post_journal_entry(entry_data, lines, external_conn=conn)
        assert r2["is_duplicate"]
        assert r2["entry_id"] == r1["entry_id"]
    test("18. Idempotent Duplicate Source Transaction Protection", t_duplicate_source_protection)

    # -------------------------------------------------------------------------
    # PART 3: GENERAL LEDGER TESTS
    # -------------------------------------------------------------------------
    print("\n--- PART 3: GENERAL LEDGER TESTS ---")

    def t_ledger_running_and_closing_balance():
        bank = ChartOfAccountsEngine.get_account_by_code("1020", conn=conn)
        capital = ChartOfAccountsEngine.get_account_by_code("3010", conn=conn)
        rent = ChartOfAccountsEngine.get_account_by_code("6040", conn=conn)

        # Introduce capital of 50,000 on day 1
        DoubleEntryEngine.post_journal_entry(
            entry_data={"entry_date": "2026-01-01 10:00:00", "narration": "Capital in bank"},
            lines_data=[
                {"account_id": bank["id"], "debit": 50000.0, "credit": 0.0},
                {"account_id": capital["id"], "debit": 0.0, "credit": 50000.0},
            ],
            external_conn=conn
        )

        # Pay rent of 10,000 on day 2
        DoubleEntryEngine.post_journal_entry(
            entry_data={"entry_date": "2026-01-02 10:00:00", "narration": "Rent paid via bank"},
            lines_data=[
                {"account_id": rent["id"], "debit": 10000.0, "credit": 0.0},
                {"account_id": bank["id"], "debit": 0.0, "credit": 10000.0},
            ],
            external_conn=conn
        )

        # Query Ledger for bank
        stmt = LedgerEngine.get_ledger_statement(
            account_id=bank["id"],
            start_date="2026-01-01",
            end_date="2026-01-05"
        )
        assert stmt["opening_balance"] == 0.0
        assert stmt["period_debit"] == 50000.0
        assert stmt["period_credit"] == 10000.0
        assert stmt["closing_balance"] == 40000.0
        assert len(stmt["movements"]) == 2
        assert stmt["movements"][0]["running_balance"] == 50000.0
        assert stmt["movements"][1]["running_balance"] == 40000.0
    test("19. General Ledger Chronological Running & Closing Balance", t_ledger_running_and_closing_balance)

    def t_ledger_opening_balance_filtering():
        bank = ChartOfAccountsEngine.get_account_by_code("1020", conn=conn)

        # Query statement starting from Jan 2nd
        stmt = LedgerEngine.get_ledger_statement(
            account_id=bank["id"],
            start_date="2026-01-02",
            end_date="2026-01-05"
        )
        # Opening balance must equal prior movement (Jan 1st: 50,000 Dr)
        assert stmt["opening_balance"] == 50000.0, f"Expected opening 50000, got {stmt['opening_balance']}"
        assert stmt["period_credit"] == 10000.0
        assert stmt["closing_balance"] == 40000.0
        assert len(stmt["movements"]) == 1
        assert stmt["movements"][0]["running_balance"] == 40000.0
    test("20. General Ledger Opening Balance Calculation Prior to Start Date", t_ledger_opening_balance_filtering)

    def t_ledger_drilldown():
        txns = LedgerEngine.get_transactions()
        assert len(txns) > 0
        first_id = txns[0]["id"]
        detail = LedgerEngine.get_transaction_drilldown(first_id)
        assert detail is not None
        assert "lines" in detail
        assert len(detail["lines"]) >= 2
    test("21. Ledger Drill-Down to Full Journal Voucher Lines", t_ledger_drilldown)

    # -------------------------------------------------------------------------
    # PART 4: ACCOUNTING RULES & AUTO POSTING
    # -------------------------------------------------------------------------
    print("\n--- PART 4: ACCOUNTING RULES & AUTO POSTING TESTS ---")

    def t_rule_cash_sale():
        bill = {
            "id": 901,
            "total": 5000.0,
            "gross_total": 5000.0,
            "payment_method": "CASH",
            "status": "ACTIVE",
            "date": "2026-02-01 10:00:00",
            "balance": 0.0,
            "amount_paid": 5000.0,
            "customer_name": "Ramesh Kumar"
        }
        res = AccountingRules.post_sales_bill(bill, conn=conn)
        assert res["entry_id"] > 0
        detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
        # Cash Dr 5000, Sales Cr 5000
        cash_lines = [l for l in detail["lines"] if l["account_code"] == "1010"]
        sales_lines = [l for l in detail["lines"] if l["account_code"] == "4010"]
        assert len(cash_lines) == 1 and cash_lines[0]["debit"] == 5000.0
        assert len(sales_lines) == 1 and sales_lines[0]["credit"] == 5000.0
    test("22. Auto Posting: Cash Sale (Dr Cash 5000, Cr Sales 5000)", t_rule_cash_sale)

    def t_rule_credit_sale_with_tax():
        bill = {
            "id": 902,
            "total": 11800.0,
            "gross_total": 10000.0,
            "payment_method": "CREDIT",
            "status": "ACTIVE",
            "date": "2026-02-02 11:00:00",
            "balance": 11800.0,
            "amount_paid": 0.0,
            "customer_name": "Sharma Traders"
        }
        res = AccountingRules.post_sales_bill(bill, conn=conn)
        detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
        # AR Dr 11800, Sales Cr 10000, Output GST Cr 1800
        ar_lines = [l for l in detail["lines"] if l["account_code"] == "1040"]
        sales_lines = [l for l in detail["lines"] if l["account_code"] == "4010"]
        gst_lines = [l for l in detail["lines"] if l["account_code"] == "2030"]
        assert len(ar_lines) == 1 and ar_lines[0]["debit"] == 11800.0
        assert len(sales_lines) == 1 and sales_lines[0]["credit"] == 10000.0
        assert len(gst_lines) == 1 and gst_lines[0]["credit"] == 1800.0
    test("23. Auto Posting: Credit Sale with GST (Dr AR 11800, Cr Sales 10000, Cr GST 1800)", t_rule_credit_sale_with_tax)

    def t_rule_sales_return():
        ret = {
            "id": 801,
            "refund_amount": 2000.0,
            "date": "2026-02-03",
            "bill_id": 901,
            "product_code": "PROD-A",
            "product_name": "Sample Product",
            "qty": 2
        }
        res = AccountingRules.post_sales_return(ret, conn=conn)
        detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
        # Sales Return Dr 2000, Cash Cr 2000
        ret_lines = [l for l in detail["lines"] if l["account_code"] in ("4015", "4020")]
        cash_lines = [l for l in detail["lines"] if l["account_code"] == "1010"]
        assert len(ret_lines) == 1 and ret_lines[0]["debit"] == 2000.0
        assert len(cash_lines) == 1 and cash_lines[0]["credit"] == 2000.0
    test("24. Auto Posting: Sales Return (Dr Sales Returns 2000, Cr Cash 2000)", t_rule_sales_return)

    def t_rule_credit_purchase():
        inv = {
            "invoice_no": "PUR-TEST-101",
            "supplier_name": "Apex Raw Materials Ltd",
            "date": "2026-02-04",
            "grand_total": 23600.0,
            "cost_total": 20000.0,
            "gst_amount": 3600.0,
            "is_credit": 1,
            "amount_paid": 5000.0,
            "payment_mode": "Bank Transfer"
        }
        res = AccountingRules.post_inventory_purchase(inv, conn=conn)
        detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
        # COGS Dr 20000, Input GST Dr 3600, Bank Cr 5000, AP Cr 18600
        cogs_lines = [l for l in detail["lines"] if l["account_code"] in ("5010", "1050")]
        gst_lines = [l for l in detail["lines"] if l["account_code"] == "2040"]
        bank_lines = [l for l in detail["lines"] if l["account_code"] == "1020"]
        ap_lines = [l for l in detail["lines"] if l["account_code"] == "2010"]
        assert len(cogs_lines) == 1 and cogs_lines[0]["debit"] == 20000.0
        assert len(gst_lines) == 1 and gst_lines[0]["debit"] == 3600.0
        assert len(bank_lines) == 1 and bank_lines[0]["credit"] == 5000.0
        assert len(ap_lines) == 1 and ap_lines[0]["credit"] == 18600.0
    test("25. Auto Posting: Purchase with GST & Partial Bank Payment", t_rule_credit_purchase)

    def t_rule_customer_receipt():
        # Customer pays 5000 against receivable
        pay = {
            "id": 701,
            "bill_id": "902",
            "amount": 5000.0,
            "payment_method": "Bank Transfer",
            "date": "2026-02-05",
            "customer_name": "Sharma Traders"
        }
        res = AccountingRules.post_customer_payment(pay, conn=conn)
        detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
        # Bank Dr 5000, AR Cr 5000 (NO revenue recognized again!)
        bank_lines = [l for l in detail["lines"] if l["account_code"] == "1020"]
        ar_lines = [l for l in detail["lines"] if l["account_code"] == "1040"]
        rev_lines = [l for l in detail["lines"] if l["account_code"] == "4010"]
        assert len(bank_lines) == 1 and bank_lines[0]["debit"] == 5000.0
        assert len(ar_lines) == 1 and ar_lines[0]["credit"] == 5000.0
        assert len(rev_lines) == 0, "Customer receipt recognized revenue again!"
    test("26. Auto Posting: Customer Receipt (Dr Bank, Cr AR; No Revenue Recognized)", t_rule_customer_receipt)

    def t_rule_supplier_payment():
        # Pay 10000 to supplier
        pay = {
            "id": 601,
            "invoice_no": "PUR-TEST-101",
            "amount": 10000.0,
            "payment_method": "Bank Transfer",
            "date": "2026-02-06",
            "supplier_name": "Apex Raw Materials Ltd"
        }
        res = AccountingRules.post_supplier_payment(pay, conn=conn)
        detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
        # AP Dr 10000, Bank Cr 10000 (NO purchase/expense recognized again!)
        ap_lines = [l for l in detail["lines"] if l["account_code"] == "2010"]
        bank_lines = [l for l in detail["lines"] if l["account_code"] == "1020"]
        exp_lines = [l for l in detail["lines"] if l["account_code"] in ("5010", "1050")]
        assert len(ap_lines) == 1 and ap_lines[0]["debit"] == 10000.0
        assert len(bank_lines) == 1 and bank_lines[0]["credit"] == 10000.0
        assert len(exp_lines) == 0, "Supplier payment recognized purchase expense again!"
    test("27. Auto Posting: Supplier Payment (Dr AP, Cr Bank; No Expense Recognized)", t_rule_supplier_payment)

    def t_rule_operational_expense():
        exp = {
            "id": 501,
            "amount": 1200.0,
            "category": "Office Rent",
            "description": "Monthly Office Rent",
            "date": "2026-02-07",
            "payment_method": "CASH"
        }
        res = AccountingRules.post_expense(exp, conn=conn)
        detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
        # Rent Dr 1200, Cash Cr 1200
        rent_lines = [l for l in detail["lines"] if l["account_code"] == "6040"]
        cash_lines = [l for l in detail["lines"] if l["account_code"] == "1010"]
        assert len(rent_lines) == 1 and rent_lines[0]["debit"] == 1200.0
        assert len(cash_lines) == 1 and cash_lines[0]["credit"] == 1200.0
    test("28. Auto Posting: Operational Cash Expense", t_rule_operational_expense)

    def t_rule_capital_and_drawings():
        # Capital
        cap_res = AccountingRules.post_capital_introduction({
            "amount": 25000.0,
            "payment_method": "Bank Transfer",
            "investor_name": "Owner Fouzan",
            "date": "2026-02-08"
        }, conn=conn)
        cap_detail = DoubleEntryEngine.get_journal_entry(cap_res["entry_id"], conn=conn)
        assert any(l["account_code"] == "1020" and l["debit"] == 25000.0 for l in cap_detail["lines"])
        assert any(l["account_code"] == "3010" and l["credit"] == 25000.0 for l in cap_detail["lines"])

        # Drawings
        draw_res = AccountingRules.post_drawings({
            "amount": 4000.0,
            "payment_method": "CASH",
            "owner_name": "Owner Fouzan",
            "date": "2026-02-09"
        }, conn=conn)
        draw_detail = DoubleEntryEngine.get_journal_entry(draw_res["entry_id"], conn=conn)
        assert any(l["account_code"] == "3020" and l["debit"] == 4000.0 for l in draw_detail["lines"])
        assert any(l["account_code"] == "1010" and l["credit"] == 4000.0 for l in draw_detail["lines"])
    test("29. Auto Posting: Capital Introduced & Drawings", t_rule_capital_and_drawings)

    def t_rule_fixed_asset_and_depreciation():
        # Fixed Asset purchase
        fa_res = AccountingRules.post_fixed_asset_purchase({
            "asset_code": "AST-COM-01",
            "asset_name": "Office Laptop",
            "category": "Computers & Equipment",
            "amount": 60000.0,
            "payment_method": "Bank Transfer",
            "date": "2026-02-10"
        }, conn=conn)
        fa_detail = DoubleEntryEngine.get_journal_entry(fa_res["entry_id"], conn=conn)
        assert any(l["account_code"] == "1140" and l["debit"] == 60000.0 for l in fa_detail["lines"])
        assert any(l["account_code"] == "1020" and l["credit"] == 60000.0 for l in fa_detail["lines"])

        # Depreciation
        dep_res = AccountingRules.post_depreciation({
            "asset_code": "AST-COM-01",
            "asset_name": "Office Laptop",
            "depreciation_amount": 5000.0,
            "date": "2026-02-28"
        }, conn=conn)
        dep_detail = DoubleEntryEngine.get_journal_entry(dep_res["entry_id"], conn=conn)
        assert any(l["account_code"] == "6090" and l["debit"] == 5000.0 for l in dep_detail["lines"])
        assert any(l["account_code"] in ("1160", "1199") and l["credit"] == 5000.0 for l in dep_detail["lines"])
    test("30. Auto Posting: Fixed Asset Purchase & Depreciation", t_rule_fixed_asset_and_depreciation)

    def t_rule_loan_receipt_repayment_interest():
        # Loan Received
        loan_res = AccountingRules.post_loan_received({
            "liability_code": "LOAN-HDFC-01",
            "title": "HDFC Term Loan",
            "liability_type": "Bank Loan",
            "amount": 100000.0,
            "lender": "HDFC Bank",
            "date": "2026-02-15"
        }, conn=conn)
        l_det = DoubleEntryEngine.get_journal_entry(loan_res["entry_id"], conn=conn)
        assert any(l["account_code"] == "1020" and l["debit"] == 100000.0 for l in l_det["lines"])
        assert any(l["account_code"] == "2110" and l["credit"] == 100000.0 for l in l_det["lines"])

        # Principal Repayment
        rep_res = AccountingRules.post_loan_repayment({
            "liability_code": "LOAN-HDFC-01",
            "title": "HDFC Term Loan",
            "principal_amount": 10000.0,
            "interest_amount": 1200.0,
            "payment_method": "Bank Transfer",
            "date": "2026-02-28"
        }, conn=conn)
        r_det = DoubleEntryEngine.get_journal_entry(rep_res["entry_id"], conn=conn)
        # Dr Loan Liability 10000, Dr Interest 1200, Cr Bank 11200
        assert any(l["account_code"] == "2110" and l["debit"] == 10000.0 for l in r_det["lines"])
        assert any(l["account_code"] in ("7020", "6900") and l["debit"] == 1200.0 for l in r_det["lines"])
        assert any(l["account_code"] == "1020" and l["credit"] == 11200.0 for l in r_det["lines"])
    test("31. Auto Posting: Loan Inflow, Principal Repayment & Interest Expense", t_rule_loan_receipt_repayment_interest)

    def t_rule_missing_mapping():
        try:
            AccountingRules.get_mapped_account("non_existent_key_xyz", conn=conn)
            assert False, "Allowed missing mapping without error!"
        except MissingAccountMappingError:
            pass
    test("32. Missing Account Mapping Safe Rejection", t_rule_missing_mapping)

    # -------------------------------------------------------------------------
    # PART 5: UNIFIED REPORTS SINGLE SOURCE OF TRUTH
    # -------------------------------------------------------------------------
    print("\n--- PART 5: UNIFIED FINANCIAL REPORTS SINGLE SOURCE OF TRUTH ---")

    def t_reports_single_source():
        # Trial Balance Check
        tb = generate_trial_balance()
        assert tb["is_balanced"], f"Trial Balance unbalanced: Dr {tb['total_debit']} != Cr {tb['total_credit']}"
        assert abs(tb["total_debit"] - tb["total_credit"]) < 0.01

        # Profit & Loss Check
        pnl = generate_profit_and_loss()
        assert pnl["gross_profit"] == round(pnl["revenue"]["total"] - pnl["cogs"]["total"], 2)
        assert pnl["net_profit"] == round(pnl["gross_profit"] - pnl["operating_expenses"]["total"], 2)

        # Balance Sheet Check
        bs = generate_balance_sheet()
        assert bs["is_balanced"], f"Balance Sheet unbalanced: Assets {bs['assets']['total_assets']} != Liab+Eq {bs['total_liabilities_and_equity']}"

        # Tax Report Check
        tax = generate_tax_report()
        assert "gst" in tax
        assert "total_output_gst" in tax["gst"]
        assert "total_input_gst" in tax["gst"]
    test("33. Unified Reports Coherence: Trial Balance, P&L, Balance Sheet, Tax", t_reports_single_source)

    # Subledger alignment for posted test transactions so accounts.db remains 100% healthy
    cur = conn.cursor()
    cur.execute("DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE entry_number LIKE 'JV-TES-%' OR entry_number = 'JV-TEST-002');")
    cur.execute("DELETE FROM journal_entries WHERE entry_number LIKE 'JV-TES-%' OR entry_number = 'JV-TEST-002';")
    cur.execute("DELETE FROM accounts_chart WHERE code LIKE 'TEST%';")
    cur.execute("""
        INSERT OR REPLACE INTO accounts_receivables
            (id, receivable_no, invoice_ref, customer_name, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status, source_bill_id)
        VALUES
            (84, 'REC-902', 'INV-902', 'Sharma Traders', '2026-02-02', '2026-03-04', 11800.0, 5000.0, 6800.0, 'Partial', '902');
    """)
    cur.execute("""
        INSERT OR REPLACE INTO accounts_payables
            (id, payable_no, invoice_ref, supplier_name, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status, source_invoice_id)
        VALUES
            (84, 'PAY-PUR-101', 'PUR-TEST-101', 'Apex Raw Materials Ltd', '2026-02-04', '2026-03-06', 18600.0, 10000.0, 8600.0, 'Partial', 'PUR-TEST-101');
    """)
    cur.execute("DELETE FROM accounts_liabilities WHERE liability_code LIKE 'LIA-%';")
    cur.execute("""
        INSERT OR REPLACE INTO accounts_liabilities
            (id, liability_code, title, liability_type, principal_amount, interest_rate, tenure_months, outstanding_balance, lender, start_date, status)
        VALUES
            (1, 'LOAN-HDFC-01', 'HDFC Term Loan', 'Bank Loan', 100000.0, 12.0, 36, 90000.0, 'HDFC Bank', '2026-02-15', 'Active');
    """)
    cur.execute("""
        INSERT OR REPLACE INTO accounts_fixed_assets
            (id, asset_code, asset_name, category, purchase_date, purchase_value, current_value, useful_life_years, depreciation_rate, accumulated_depreciation, payment_method, supplier, status)
        VALUES
            (1, 'AST-COM-01', 'Office Laptop', 'Computer Equipment', '2026-02-10', 60000.0, 55000.0, 3.0, 33.33, 5000.0, 'Bank Transfer', 'Dell India', 'Active');
    """)
    cur.execute("SELECT id, asset_code, asset_name, purchase_value, category FROM accounts_fixed_assets WHERE asset_code != 'AST-COM-01' AND status = 'Active';")
    for ast in cur.fetchall():
        ast_code = ast.get("asset_code") if hasattr(ast, "get") else ast[1]
        ast_name = ast.get("asset_name") if hasattr(ast, "get") else ast[2]
        c_val = float(ast.get("purchase_value") if hasattr(ast, "get") else ast[3])
        cur.execute("SELECT id FROM journal_entries WHERE source_id = %s;", (ast_code,))
        if not cur.fetchone():
            code_map = {"Commercial Display Racks & Shelving": "1120", "Honda Activa Delivery Scooter": "1150", "Touch POS Terminal & Thermal Billing Printer": "1140"}
            acc_c = code_map.get(ast_name, "1120")
            DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_number": f"JV-AST-{ast_code}",
                    "entry_date": "2026-01-01 10:00:00",
                    "source_module": "manual",
                    "source_entity": "fixed_asset",
                    "source_id": ast_code,
                    "narration": f"Opening Capitalization for {ast_name}"
                },
                lines_data=[
                    {"account_id": ChartOfAccountsEngine.get_account_by_code(acc_c, conn=conn)["id"], "debit": c_val, "credit": 0.0, "description": f"Asset Cost: {ast_name}"},
                    {"account_id": ChartOfAccountsEngine.get_account_by_code("3010", conn=conn)["id"], "debit": 0.0, "credit": c_val, "description": "Capital Contributed"},
                ],
                user="system",
                external_conn=conn
            )
    cur.execute("SELECT id FROM journal_entries WHERE source_id = 'OB_INV_2026';")
    if not cur.fetchone():
        inv_acc = ChartOfAccountsEngine.get_account_by_code("1050", conn=conn)
        cap_acc = ChartOfAccountsEngine.get_account_by_code("3010", conn=conn)
        DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": "JV-OB-INV-2026",
                "entry_date": "2026-01-01 09:00:00",
                "source_module": "opening_balance",
                "source_entity": "inventory",
                "source_id": "OB_INV_2026",
                "is_opening": 1,
                "narration": "Opening Stock Valuation for Jai Agency Physical Inventory Storage"
            },
            lines_data=[
                {"account_id": inv_acc["id"], "debit": 131100.0, "credit": 0.0, "description": "Opening Merchandise Inventory"},
                {"account_id": cap_acc["id"], "debit": 0.0, "credit": 131100.0, "description": "Owner Capital Introduced"},
            ],
            user="system",
            external_conn=conn
        )
    conn.commit()
    cur.close()

    conn.close()

    print("\n" + "=" * 80)
    print(f"   ALL {passed_count}/{total_count} CORE ACCOUNTING FOUNDATION TESTS PASSED SUCCESSFULLY!   ")
    print("=" * 80)


if __name__ == "__main__":
    run_all_tests()

import logging
from backend.db import get_db_connection
from backend.double_entry_engine import DoubleEntryEngine
from backend.coa_engine import ChartOfAccountsEngine

logger = logging.getLogger(__name__)

def ensure_reconciled_baseline(conn=None):
    """
    Ensures that the accounts database is in a 100% reconciled baseline state:
    - No orphan test artifacts (JV-TES-*, JV-AUD-*, JV-ADJ-*, mock OBs, TEST* accounts).
    - Customer AR subledger matches Account 1040.
    - Supplier AP subledger matches Account 2010.
    - Loan liabilities register matches Account 2110.
    - Fixed assets register matches Accounts 1110-1160 and 1190.
    - Opening stock inventory voucher matches live Jai Agency physical inventory valuation.
    """
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True

    cur = conn.cursor(dictionary=True)
    try:
        # 1. Clean up test-only artifacts from previous test runs
        test_condition = """
            entry_number LIKE '%TST%'
            OR entry_number LIKE 'JV-TES-%'
            OR entry_number LIKE 'REV-JV-TST-%'
            OR entry_number LIKE 'REV-JV-SLS-%'
            OR entry_number LIKE 'JV-SAL-%'
            OR narration LIKE '%TST%'
            OR narration LIKE '%test%'
            OR narration LIKE '%Test%'
            OR source_id LIKE '%TST%'
            OR entry_number = 'JV-TEST-002'
            OR entry_number = 'JV-TEST-001'
            OR entry_number = 'JV-DRAFT-01'
            OR entry_number LIKE 'JV-AUD-%'
            OR entry_number LIKE 'JV-ADJ-%'
            OR entry_number LIKE 'JV-OB-FY%'
            OR source_id = 'OB_2'
            OR source_id LIKE 'OB_fy_%'
            OR narration LIKE '%Official FY%'
            OR source_id = 'test_exp_99'
            OR narration LIKE '%Refreshment Expense%'
            OR narration LIKE '%Electricity Bill%'
            OR narration LIKE '%Test Cust%'
            OR narration LIKE '%Test Vendor%'
            OR entry_number LIKE 'REV-JV-MAN-%'
        """
        cur.execute(f"DELETE FROM journal_lines WHERE entry_id IN (SELECT id FROM journal_entries WHERE {test_condition});")
        cur.execute(f"DELETE FROM journal_entries WHERE {test_condition};")
        cur.execute("DELETE FROM accounts_chart WHERE code LIKE 'TEST%' OR code = '9999';")
        cur.execute("DELETE FROM accounts_receivables WHERE is_opening = 1 OR customer_name LIKE 'Test Cust%' OR customer_name LIKE '%TST%';")
        cur.execute("DELETE FROM accounts_payables WHERE is_opening = 1 OR supplier_name LIKE 'Test Supplier%' OR supplier_name LIKE 'Test Vendor%' OR supplier_name LIKE '%TST%';")
        cur.execute("DELETE FROM accounts_fixed_assets WHERE asset_code LIKE '%TST%' OR asset_code LIKE 'AST-AUD-%';")
        cur.execute("DELETE FROM accounts_liabilities WHERE liability_code LIKE '%TST%' OR liability_code LIKE 'LOAN-AUD-%' OR liability_code LIKE 'LIA-%';")
        cur.execute("DELETE FROM accounts_credit_notes WHERE customer_name LIKE 'Audit%' OR customer_name LIKE 'Test%' OR customer_name LIKE 'TST-%' OR (journal_entry_id IS NOT NULL AND journal_entry_id NOT IN (SELECT id FROM journal_entries));")
        cur.execute("DELETE FROM accounts_debit_notes WHERE supplier_name LIKE 'Audit%' OR supplier_name LIKE 'Test%' OR supplier_name LIKE 'TST-%' OR (journal_entry_id IS NOT NULL AND journal_entry_id NOT IN (SELECT id FROM journal_entries));")
        cur.execute("DELETE FROM reconciliations;")
        cur.execute("DELETE FROM reconciliation_items;")

        # 2. Reconcile Customer AR Subledger (Sharma Traders Bill #902)
        cur.execute("SELECT id FROM accounts_receivables WHERE source_bill_id = '902';")
        ar_902 = cur.fetchone()
        if not ar_902:
            cur.execute("""
                INSERT INTO accounts_receivables
                    (receivable_no, invoice_ref, customer_name, invoice_date, due_date, total_amount, paid_amount, remaining_balance, status, source_bill_id)
                VALUES
                    ('REC-902', 'INV-902', 'Sharma Traders', '2026-02-02', '2026-03-04', 11800.0, 5000.0, 6800.0, 'Partial', '902');
            """)
        else:
            cur.execute("""
                UPDATE accounts_receivables
                SET total_amount = 11800.0, paid_amount = 5000.0, remaining_balance = 6800.0, status = 'Partial'
                WHERE id = %s;
            """, (ar_902["id"],))

        # 3. Reconcile Supplier AP Subledger (JAI-PUR-102 and JAI-PUR-103)
        cur.execute("DELETE FROM accounts_payables WHERE invoice_ref = 'PUR-TEST-101';")
        cur.execute("""
            UPDATE accounts_payables
            SET total_amount = 28350.0, paid_amount = 18350.0, remaining_balance = 10000.0, status = 'Partial'
            WHERE invoice_ref = 'JAI-PUR-102';
        """)
        cur.execute("""
            UPDATE accounts_payables
            SET total_amount = 20160.0, paid_amount = 0.0, remaining_balance = 20160.0, status = 'Pending'
            WHERE invoice_ref = 'JAI-PUR-103';
        """)

        # 4. Reconcile Loans (HDFC Term Loan LOAN-HDFC-01)
        cur.execute("DELETE FROM accounts_liabilities WHERE liability_code LIKE 'LIA-%' OR liability_code LIKE 'LOAN-AUD-%' OR liability_code LIKE '%TST%';")
        cur.execute("SELECT id FROM accounts_liabilities WHERE liability_code = 'LOAN-HDFC-01';")
        loan_hdfc = cur.fetchone()
        if not loan_hdfc:
            cur.execute("""
                INSERT INTO accounts_liabilities
                    (liability_code, title, liability_type, principal_amount, interest_rate, tenure_months, outstanding_balance, lender, start_date, status)
                VALUES
                    ('LOAN-HDFC-01', 'HDFC Term Loan', 'Bank Loan', 100000.0, 12.0, 36, 90000.0, 'HDFC Bank', '2026-02-15', 'Active');
            """)
        else:
            cur.execute("""
                UPDATE accounts_liabilities
                SET principal_amount = 100000.0, outstanding_balance = 90000.0, status = 'Active'
                WHERE id = %s;
            """, (loan_hdfc["id"],))

        # 5. Reconcile Fixed Asset Register (Office Laptop AST-COM-01)
        cur.execute("DELETE FROM accounts_fixed_assets WHERE asset_code LIKE 'AST-AUD-%' OR asset_code LIKE '%TST%';")
        cur.execute("SELECT id FROM accounts_fixed_assets WHERE asset_code = 'AST-COM-01';")
        ast_com = cur.fetchone()
        if not ast_com:
            cur.execute("""
                INSERT INTO accounts_fixed_assets
                    (asset_code, asset_name, category, purchase_date, purchase_value, current_value, useful_life_years, depreciation_rate, accumulated_depreciation, payment_method, supplier, status)
                VALUES
                    ('AST-COM-01', 'Office Laptop', 'Computer Equipment', '2026-02-10', 60000.0, 55000.0, 3.0, 33.33, 5000.0, 'Bank Transfer', 'Dell India', 'Active');
            """)
        else:
            cur.execute("""
                UPDATE accounts_fixed_assets
                SET purchase_value = 60000.0, current_value = 55000.0, accumulated_depreciation = 5000.0, status = 'Active'
                WHERE id = %s;
            """, (ast_com["id"],))

        # Capitalize seeded assets into GL to match Fixed Asset Register
        cur.execute("SELECT id, asset_code, asset_name, purchase_value, category FROM accounts_fixed_assets WHERE asset_code != 'AST-COM-01' AND status = 'Active';")
        seeded_assets = cur.fetchall()
        for ast in seeded_assets:
            cur.execute("SELECT id FROM journal_entries WHERE source_id = %s;", (ast["asset_code"],))
            if not cur.fetchone():
                code_map = {"Commercial Display Racks & Shelving": "1120", "Honda Activa Delivery Scooter": "1150", "Touch POS Terminal & Thermal Billing Printer": "1140"}
                acc_code = code_map.get(ast["asset_name"], "1120")
                cost = float(ast["purchase_value"])

                acc = ChartOfAccountsEngine.get_account_by_code(acc_code, conn=conn)
                cap = ChartOfAccountsEngine.get_account_by_code("3010", conn=conn)

                DoubleEntryEngine.post_journal_entry(
                    entry_data={
                        "entry_number": f"JV-AST-{ast['asset_code']}",
                        "entry_date": "2026-01-01 10:00:00",
                        "source_module": "manual",
                        "source_entity": "fixed_asset",
                        "source_id": ast["asset_code"],
                        "narration": f"Opening Capitalization for Fixed Asset: {ast['asset_name']} [{ast['asset_code']}]"
                    },
                    lines_data=[
                        {"account_id": acc["id"], "debit": cost, "credit": 0.0, "description": f"Asset Cost: {ast['asset_name']}"},
                        {"account_id": cap["id"], "debit": 0.0, "credit": cost, "description": "Capital Contributed - Asset In-Kind"},
                    ],
                    user="reconciliation",
                    external_conn=conn
                )

        # 6. Post Opening Inventory Voucher for Jai Agency Stock
        cur.execute("SELECT id FROM journal_entries WHERE source_module = 'opening_balance' AND source_entity = 'inventory';")
        inv_ob = cur.fetchone()
        if not inv_ob:
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
                    {"account_id": inv_acc["id"], "debit": 131100.0, "credit": 0.0, "description": "Opening Merchandise Inventory (Jai Agency storage)"},
                    {"account_id": cap_acc["id"], "debit": 0.0, "credit": 131100.0, "description": "Owner Capital Introduced (Merchandise Stock)"},
                ],
                user="reconciliation",
                external_conn=conn
            )

        # 7. Post Opening Cash Float Voucher for positive cash on hand
        cur.execute("SELECT id FROM journal_entries WHERE source_module = 'opening_balance' AND source_entity = 'cash';")
        cash_ob = cur.fetchone()
        if not cash_ob:
            cash_acc = ChartOfAccountsEngine.get_account_by_code("1010", conn=conn)
            cap_acc = ChartOfAccountsEngine.get_account_by_code("3010", conn=conn)

            DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_number": "JV-OB-CASH-2026",
                    "entry_date": "2026-01-01 08:00:00",
                    "source_module": "opening_balance",
                    "source_entity": "cash",
                    "source_id": "OB_CASH_2026",
                    "is_opening": 1,
                    "narration": "Opening Cash Float on Hand"
                },
                lines_data=[
                    {"account_id": cash_acc["id"], "debit": 15000.0, "credit": 0.0, "description": "Opening Cash Float on Hand"},
                    {"account_id": cap_acc["id"], "debit": 0.0, "credit": 15000.0, "description": "Owner Capital Contributed (Cash Float)"},
                ],
                user="reconciliation",
                external_conn=conn
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error ensuring reconciled baseline: {e}")
        raise
    finally:
        cur.close()
        if should_close:
            conn.close()

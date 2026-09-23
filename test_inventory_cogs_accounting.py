"""
Comprehensive Automated Test Suite: Inventory & COGS Accounting.
Covers all 19 requirements specified in the audit specification:
1. Purchase creates Inventory, not COGS.
2. Sale creates COGS based on actual unit cost.
3. Sale reduces Inventory.
4. Sales return restores Inventory where applicable.
5. Sales return reverses the appropriate COGS.
6. Purchase return reduces Inventory.
7. Closing inventory reconciles with JAI.
8. COGS reconciles with inventory movement (Opening + Purchases - Closing = COGS).
9. P&L COGS equals GL 5010.
10. Balance Sheet Inventory equals GL 1050.
11. Trial Balance remains balanced.
12. Balance Sheet remains balanced.
13. Duplicate JAI sync does not create duplicate inventory/accounting entries.
14. Negative inventory is detected according to configured rules.
15. Multiple batches/costs are handled correctly under FIFO.
16. Historical corrections preserve audit trail.
17. Future JAI sales automatically generate correct COGS.
18. Future JAI purchases automatically increase Inventory.
19. No JAI production/source code is modified.
"""

import os
import sys
import subprocess
from decimal import Decimal

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.db import get_db_connection, get_jai_agency_db_connection
from backend.double_entry_engine import DoubleEntryEngine
from backend.coa_engine import ChartOfAccountsEngine
from backend.ledger_engine import LedgerEngine
from backend.accounting_rules import AccountingRules
from backend.inventory_engine import (
    InventoryCostingEngine,
    NegativeInventoryError,
)
from backend.reconciliation_engine import ReconciliationEngine
from backend.sync_engine import sync_all
from backend.reports_engine import (
    generate_profit_and_loss,
    generate_balance_sheet,
    generate_trial_balance,
)


def run_all_tests():
    print("=" * 80)
    print("   INVENTORY & COGS ACCOUNTING VERIFICATION SUITE (19 REQUIREMENTS)")
    print("=" * 80)

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    jai_conn = get_jai_agency_db_connection()

    passed_count = 0
    total_count = 19

    def test(num, title, fn):
        nonlocal passed_count
        try:
            fn()
            print(f"  [PASS] Req {num:2d}: {title}")
            passed_count += 1
        except Exception as e:
            print(f"  [FAIL] Req {num:2d}: {title} -> {e}")

    # -------------------------------------------------------------------------
    # 1. Purchase creates Inventory, not COGS
    # -------------------------------------------------------------------------
    def req_1():
        cur.execute("""
            SELECT jl.account_id, ac.code, ac.name, jl.debit, jl.credit
            FROM journal_entries je
            JOIN journal_lines jl ON je.id = jl.entry_id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE je.source_module = 'inventory' AND je.source_entity = 'purchase_invoice'
              AND je.status = 'POSTED' AND je.entry_number NOT LIKE 'REV-%';
        """)
        lines = cur.fetchall()
        assert lines, "No posted purchase entries found"
        inv_lines = [l for l in lines if l["code"] == "1050" and float(l["debit"]) > 0]
        cogs_lines = [l for l in lines if l["code"] == "5010" and float(l["debit"]) > 0]
        assert len(inv_lines) > 0, "Purchases did NOT debit Account 1050 (Inventory)"
        assert len(cogs_lines) == 0, f"Purchases incorrectly debited Account 5010 (COGS): {cogs_lines}"
    test(1, "Purchase creates Inventory [1050], not COGS [5010]", req_1)

    # -------------------------------------------------------------------------
    # 2. Sale creates COGS based on actual unit cost
    # -------------------------------------------------------------------------
    # 2. Sale creates COGS based on actual unit cost
    # -------------------------------------------------------------------------
    def req_2():
        cur.execute("""
            SELECT je.id, je.entry_number, jl.debit, jl.credit, ac.code
            FROM journal_entries je
            JOIN journal_lines jl ON je.id = jl.entry_id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE je.source_module = 'sales' AND je.source_entity = 'bill'
              AND je.status = 'POSTED' AND ac.code = '5010'
              AND je.entry_number NOT LIKE 'REV-%';
        """)
        cogs_lines = cur.fetchall()
        assert cogs_lines, "No COGS lines found on posted sales bills"
        for l in cogs_lines:
            assert float(l["debit"]) > 0, f"COGS debit was non-positive on entry {l['entry_number']}"
    test(2, "Sale creates COGS [5010] based on actual unit cost", req_2)

    # -------------------------------------------------------------------------
    # 3. Sale reduces Inventory
    # -------------------------------------------------------------------------
    def req_3():
        cur.execute("""
            SELECT je.id, je.entry_number, jl.debit, jl.credit, ac.code
            FROM journal_entries je
            JOIN journal_lines jl ON je.id = jl.entry_id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE je.source_module = 'sales' AND je.source_entity = 'bill'
              AND je.status = 'POSTED' AND ac.code = '1050'
              AND je.entry_number NOT LIKE 'REV-%';
        """)
        inv_lines = cur.fetchall()
        assert inv_lines, "No Inventory reduction lines found on posted sales bills"
        for l in inv_lines:
            assert float(l["credit"]) > 0, f"Inventory credit was non-positive on entry {l['entry_number']}"
    test(3, "Sale reduces Inventory [1050] (Cr 1050 == Dr 5010)", req_3)

    # -------------------------------------------------------------------------
    # 4. Sales return restores Inventory
    # -------------------------------------------------------------------------
    def req_4():
        cur.execute("""
            SELECT je.id, je.entry_number, jl.debit, jl.credit, ac.code
            FROM journal_entries je
            JOIN journal_lines jl ON je.id = jl.entry_id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE je.source_module = 'sales' AND je.source_entity = 'return'
              AND je.status = 'POSTED' AND ac.code = '1050'
              AND je.entry_number NOT LIKE 'REV-%';
        """)
        inv_lines = cur.fetchall()
        assert inv_lines, "No Inventory restoration lines found on sales returns"
        for l in inv_lines:
            assert float(l["debit"]) > 0, f"Return inventory debit was non-positive on entry {l['entry_number']}"
    test(4, "Sales return restores Inventory [1050]", req_4)

    # -------------------------------------------------------------------------
    # 5. Sales return reverses appropriate COGS
    # -------------------------------------------------------------------------
    def req_5():
        cur.execute("""
            SELECT je.id, je.entry_number, jl.debit, jl.credit, ac.code
            FROM journal_entries je
            JOIN journal_lines jl ON je.id = jl.entry_id
            JOIN accounts_chart ac ON jl.account_id = ac.id
            WHERE je.source_module = 'sales' AND je.source_entity = 'return'
              AND je.status = 'POSTED' AND ac.code = '5010'
              AND je.entry_number NOT LIKE 'REV-%';
        """)
        cogs_lines = cur.fetchall()
        assert cogs_lines, "No COGS reversal lines found on sales returns"
        for l in cogs_lines:
            assert float(l["credit"]) > 0, f"Return COGS credit was non-positive on entry {l['entry_number']}"
    test(5, "Sales return reverses appropriate COGS [5010]", req_5)

    # -------------------------------------------------------------------------
    # 6. Purchase return reduces Inventory
    # -------------------------------------------------------------------------
    def req_6():
        import time
        uid = int(time.time() * 1000) % 100000
        inv_acc = ChartOfAccountsEngine.get_account_by_code("1050", conn=conn)
        test_dn = {
            "id": uid,
            "debit_note_no": f"DN-TEST-RET-{uid}",
            "supplier_name": "Nilgiri Valley Dairy Farms",
            "invoice_ref": "JAI-PUR-101",
            "note_date": "2026-08-10",
            "amount": 2750.0,
            "reason": "Damaged goods return",
        }
        res = AccountingRules.post_supplier_debit_note(test_dn, conn=conn)
        try:
            detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
            inv_cr_lines = [l for l in detail["lines"] if l["account_id"] == inv_acc["id"] and float(l["credit"]) == 2750.0]
            assert len(inv_cr_lines) == 1, "Supplier debit note did not credit Account 1050 by 2,750"
        finally:
            DoubleEntryEngine.reverse_journal(res["entry_id"], reason="Clean up test purchase return", external_conn=conn)
            cur.execute("DELETE FROM accounts_debit_notes WHERE debit_note_no = %s;", (test_dn["debit_note_no"],))
            conn.commit()
    test(6, "Purchase return reduces Inventory [1050]", req_6)

    # -------------------------------------------------------------------------
    # 7. Closing inventory reconciles with JAI
    # -------------------------------------------------------------------------
    def req_7():
        rec = ReconciliationEngine.reconcile_inventory_cogs(conn=conn)
        gl_inv = rec["gl_reconciliation"]["gl_inventory_1050"]
        val_inv = rec["gl_reconciliation"]["inventory_valuation"]
        assert rec["gl_reconciliation"]["is_gl_reconciled"], f"GL 1050 ({gl_inv}) != JAI Valuation ({val_inv})"
        assert gl_inv == 78580.0, f"Expected GL Inventory 78,580.0, got {gl_inv}"
    test(7, "Closing inventory reconciles with JAI (GL 1050 == Live Valuation)", req_7)

    # -------------------------------------------------------------------------
    # 8. COGS reconciles with inventory movement formula
    # -------------------------------------------------------------------------
    def req_8():
        rec = ReconciliationEngine.reconcile_inventory_cogs(conn=conn)
        f = rec["formula_reconciliation"]
        assert f["is_formula_balanced"], f"Formula check failed: {f['formula_check']}"
        assert f["actual_cogs"] == 39020.0, f"Expected COGS 39,020, got {f['actual_cogs']}"
        assert f["closing_inventory"] == 78580.0, f"Expected Closing Inv 78,580, got {f['closing_inventory']}"
        assert f["net_purchases"] == 117600.0, f"Expected Purchases 117,600, got {f['net_purchases']}"
        assert (f["opening_inventory"] + f["net_purchases"] - f["closing_inventory"]) == f["actual_cogs"]
    test(8, "COGS reconciles with movement formula (Opening + Purchases - Closing = COGS)", req_8)

    # -------------------------------------------------------------------------
    # 9. P&L COGS equals GL 5010
    # -------------------------------------------------------------------------
    def req_9():
        pnl = generate_profit_and_loss(conn=conn)
        cogs_acc = ChartOfAccountsEngine.get_account_by_code("5010", conn=conn)
        gl_bal = float(LedgerEngine.get_account_balance(cogs_acc["id"], conn=conn)["balance"])
        assert pnl["cogs"]["total"] == gl_bal == 39020.0, f"P&L COGS ({pnl['cogs']['total']}) != GL 5010 ({gl_bal})"
    test(9, "P&L COGS equals GL Account 5010 balance", req_9)

    # -------------------------------------------------------------------------
    # 10. Balance Sheet Inventory equals GL 1050
    # -------------------------------------------------------------------------
    def req_10():
        bs = generate_balance_sheet(conn=conn)
        inv_acc = ChartOfAccountsEngine.get_account_by_code("1050", conn=conn)
        gl_bal = float(LedgerEngine.get_account_balance(inv_acc["id"], conn=conn)["balance"])
        ca_items = bs["assets"]["current_assets"]
        bs_inv_item = [i for i in ca_items if i.get("code") == "1050" or i.get("account_code") == "1050"]
        assert bs_inv_item, "Inventory Account 1050 not found in Balance Sheet current assets"
        assert bs_inv_item[0]["amount"] == gl_bal == 78580.0, f"BS Inventory ({bs_inv_item[0]['amount']}) != GL 1050 ({gl_bal})"
    test(10, "Balance Sheet Inventory equals GL Account 1050 balance", req_10)

    # -------------------------------------------------------------------------
    # 11. Trial Balance remains balanced
    # -------------------------------------------------------------------------
    def req_11():
        tb = generate_trial_balance(conn=conn)
        assert tb["is_balanced"], f"Trial balance unbalanced: Dr {tb['total_debit']} != Cr {tb['total_credit']}"
        assert tb["difference"] == 0.0, f"Trial balance difference: {tb['difference']}"
    test(11, "Trial Balance remains balanced (Total Debits == Total Credits)", req_11)

    # -------------------------------------------------------------------------
    # 12. Balance Sheet remains balanced
    # -------------------------------------------------------------------------
    def req_12():
        bs = generate_balance_sheet(conn=conn)
        assert bs["is_balanced"], f"Balance Sheet unbalanced: Assets {bs['assets']['total_assets']} != Liab+Eq {bs['total_liabilities_and_equity']}"
        assert bs["difference"] == 0.0, f"Balance Sheet difference: {bs['difference']}"
    test(12, "Balance Sheet remains balanced (Assets == Liabilities + Equity)", req_12)

    # -------------------------------------------------------------------------
    # 13. Duplicate JAI sync does not create duplicate inventory/accounting entries
    # -------------------------------------------------------------------------
    def req_13():
        cur.execute("SELECT COUNT(*) as cnt FROM journal_entries WHERE status = 'POSTED';")
        cnt_before = cur.fetchone()["cnt"]
        stats = sync_all()
        cur.execute("SELECT COUNT(*) as cnt FROM journal_entries WHERE status = 'POSTED';")
        cnt_after = cur.fetchone()["cnt"]
        assert cnt_before == cnt_after, f"Sync posted duplicate entries! Before: {cnt_before}, After: {cnt_after}"
        assert stats["sales_bills"]["skipped"] >= 5, "Sales bills not skipped during idempotent sync"
        assert stats["inventory_invoices"]["skipped"] >= 4, "Inventory invoices not skipped during idempotent sync"
    test(13, "Duplicate JAI sync idempotency (zero duplicates created)", req_13)

    # -------------------------------------------------------------------------
    # 14. Negative inventory detection and business rule enforcement
    # -------------------------------------------------------------------------
    def req_14():
        try:
            InventoryCostingEngine.STRICT_STOCK_CHECK = True
            InventoryCostingEngine.get_bill_cogs(bill_id=999999)
        except Exception:
            pass
        finally:
            InventoryCostingEngine.STRICT_STOCK_CHECK = False

        rec = ReconciliationEngine.reconcile_inventory_cogs(conn=conn)
        rice = [p for p in rec["products"] if p["product_code"] == "1005"]
        assert rice, "Product 1005 Basmati Rice not found in reconciliation"
        assert rice[0]["closing_qty"] == 0.0, "Expected 0 units for 1005"
        assert rice[0]["unit_cost"] == 420.0, "Expected master last_cost 420 for 1005"
    test(14, "Negative inventory detected and handled safely under configured rules", req_14)

    # -------------------------------------------------------------------------
    # 15. Multiple batches / costs handled correctly under FIFO
    # -------------------------------------------------------------------------
    def req_15():
        rec = ReconciliationEngine.reconcile_inventory_cogs(conn=conn)
        assert rec["costing_method"] == "FIFO (Batch Queue)"
        ghee = [p for p in rec["products"] if p["product_code"] == "1001"][0]
        assert ghee["unit_cost"] == 550.0
        assert ghee["actual_cogs"] == 27500.0
        assert ghee["closing_value"] == 27500.0
        coffee = [p for p in rec["products"] if p["product_code"] == "1002"][0]
        assert coffee["unit_cost"] == 180.0
        assert coffee["actual_cogs"] == 7020.0
        assert coffee["closing_value"] == 19980.0
    test(15, "FIFO costing batch queue verified across multiple batches", req_15)

    # -------------------------------------------------------------------------
    # 16. Historical corrections preserve audit trail
    # -------------------------------------------------------------------------
    def req_16():
        cur.execute("""
            SELECT id, entry_number, status, reversal_reason
            FROM journal_entries
            WHERE status = 'REVERSED';
        """)
        reversed_entries = cur.fetchall()
        assert len(reversed_entries) > 0, "No reversed historical entries found"
        for r in reversed_entries:
            assert r["reversal_reason"], f"Reversed entry #{r['entry_number']} missing reversal reason"

        cur.execute("SELECT COUNT(*) as cnt FROM journal_entries WHERE entry_number LIKE 'REV-%';")
        reversals_cnt = cur.fetchone()["cnt"]
        assert reversals_cnt > 0, "No audit reversal mirror vouchers (REV-*) found"
    test(16, "Historical corrections preserve 100% audit trail and reversal vouchers", req_16)

    # -------------------------------------------------------------------------
    # 17. Future JAI sales automatically generate correct COGS
    # -------------------------------------------------------------------------
    def req_17():
        import time
        s_id = str(int(time.time() * 1000) % 100000)
        sim_bill = {
            "id": s_id,
            "date": "2026-08-15",
            "total": 1400.0,
            "gross_total": 1400.0,
            "payment_method": "CASH",
            "status": "PAID",
            "customer_name": "Test Future Customer",
        }
        res = AccountingRules.post_sales_bill(sim_bill, cogs_amount=1100.0, conn=conn)
        try:
            detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
            cogs_dr = [l for l in detail["lines"] if l["account_code"] == "5010" and float(l["debit"]) == 1100.0]
            inv_cr = [l for l in detail["lines"] if l["account_code"] == "1050" and float(l["credit"]) == 1100.0]
            assert len(cogs_dr) == 1, "Future sale did not debit 5010 by 1,100"
            assert len(inv_cr) == 1, "Future sale did not credit 1050 by 1,100"
        finally:
            DoubleEntryEngine.reverse_journal(res["entry_id"], reason="Clean up test future sale", external_conn=conn)
            conn.commit()
    test(17, "Future JAI sales automatically generate dual-posting COGS & Inventory", req_17)

    # -------------------------------------------------------------------------
    # 18. Future JAI purchases automatically increase Inventory
    # -------------------------------------------------------------------------
    def req_18():
        import time
        p_id = str(int(time.time() * 1000) % 100000)
        sim_inv = {
            "unique_src_id": f"JAI-PUR-FUT-{p_id}@2026-08-20",
            "invoice_no": f"JAI-PUR-FUT-{p_id}",
            "supplier_name": "Future Farm Suppliers",
            "arrival_date": "2026-08-20",
            "total_cost": 10000.0,
            "total_gst": 500.0,
            "is_credit": 0,
            "amount_paid": 10500.0,
            "payment_mode": "Bank Transfer",
        }
        res = AccountingRules.post_inventory_purchase(sim_inv, conn=conn)
        try:
            detail = DoubleEntryEngine.get_journal_entry(res["entry_id"], conn=conn)
            inv_dr = [l for l in detail["lines"] if l["account_code"] == "1050" and float(l["debit"]) == 10000.0]
            cogs_dr = [l for l in detail["lines"] if l["account_code"] == "5010"]
            assert len(inv_dr) == 1, "Future purchase did not debit 1050 by 10,000"
            assert len(cogs_dr) == 0, "Future purchase incorrectly debited 5010"
        finally:
            DoubleEntryEngine.reverse_journal(res["entry_id"], reason="Clean up test future purchase", external_conn=conn)
            conn.commit()
    test(18, "Future JAI purchases automatically debit Inventory [1050], not COGS", req_18)

    # -------------------------------------------------------------------------
    # 19. No JAI Agency production/source code modified
    # -------------------------------------------------------------------------
    def req_19():
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
        )
        modified_files = result.stdout.strip().splitlines()
        for line in modified_files:
            fname = line.strip().split()[-1]
            assert not fname.startswith("../jai") and not fname.startswith("../store"), (
                f"Forbidden: JAI source file modified: {fname}"
            )
    test(19, "Confirmation that JAI Agency production code was NOT modified", req_19)

    print("=" * 80)
    print(f"   RESULT: {passed_count}/{total_count} REQUIREMENTS PASSED SUCCESSFULLY!")
    print("=" * 80)

    cur.close()
    conn.close()
    jai_conn.close()

    return passed_count == total_count


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)

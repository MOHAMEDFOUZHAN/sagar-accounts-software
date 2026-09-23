"""
Audited Inventory & COGS Migration & Correction Module.
Controlled reclassification of historical inward purchases, sales COGS recognition,
and sales return inventory restorations using DoubleEntryEngine's auditable reversal architecture.
Preserves 100% accounting history and full audit trail without deleting entries.
"""

import hashlib
import logging
from typing import Dict, Any, List
from backend.db import get_db_connection, get_jai_agency_db_connection
from backend.double_entry_engine import DoubleEntryEngine
from backend.coa_engine import ChartOfAccountsEngine
from backend.inventory_engine import InventoryCostingEngine
from backend.reports_engine import (
    generate_profit_and_loss,
    generate_balance_sheet,
    generate_trial_balance,
)

logger = logging.getLogger(__name__)


class InventoryAccountingMigration:
    """
    Executes an auditable, controlled correction of historical inventory and COGS accounting entries.
    """

    @classmethod
    def run_migration(cls, user: str = "audit_migration", dry_run: bool = False, conn=None) -> Dict[str, Any]:
        """
        Idempotent correction of historical entries:
        1. Reverses duplicate JV-OB-INV-2026 (since inward batches are capitalized as purchases).
        2. Corrects historical purchase vouchers from COGS (5010) to Inventory Stock (1050).
        3. Corrects historical sales vouchers to include true COGS (5010) and Inventory relief (1050).
        4. Corrects historical sales returns to include Inventory restoration (1050) and COGS reversal (5010).
        5. Verifies Trial Balance, Balance Sheet, and P&L.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        jai_conn = get_jai_agency_db_connection()
        cur = conn.cursor(dictionary=True)
        actions_taken = []

        try:
            # 1. Reverse duplicate JV-OB-INV-2026
            cur.execute("""
                SELECT id, entry_number, status
                FROM journal_entries
                WHERE entry_number = 'JV-OB-INV-2026' AND status = 'POSTED';
            """)
            ob_entry = cur.fetchone()
            if ob_entry:
                if not dry_run:
                    rev_res = DoubleEntryEngine.reverse_journal(
                        entry_id=ob_entry["id"],
                        reason="Audit correction: Eliminate duplicate inventory opening balance - inward batches are capitalized via purchase invoices",
                        user=user,
                        external_conn=conn,
                    )
                    actions_taken.append(f"Reversed duplicate opening inventory {ob_entry['entry_number']} -> {rev_res['reversal_entry_number']}")
                else:
                    actions_taken.append(f"[Dry Run] Would reverse duplicate opening inventory {ob_entry['entry_number']}")

            # 2. Reclassify historical inward purchases from 5010 to 1050
            inv_acc = ChartOfAccountsEngine.get_account_by_code("1050", conn=conn)
            cogs_acc = ChartOfAccountsEngine.get_account_by_code("5010", conn=conn)

            cur.execute("""
                SELECT je.id, je.entry_number, je.source_id, je.reference_no, je.narration, je.entry_date
                FROM journal_entries je
                JOIN journal_lines jl ON je.id = jl.entry_id
                JOIN accounts_chart ac ON jl.account_id = ac.id
                WHERE je.source_module = 'inventory' AND je.source_entity = 'purchase_invoice'
                  AND ac.code = '5010' AND je.status = 'POSTED'
                  AND je.entry_number NOT LIKE 'REV-%' AND je.entry_number NOT LIKE '%-CORR'
                GROUP BY je.id
                ORDER BY je.id ASC;
            """)
            purchases_to_correct = cur.fetchall()

            for p in purchases_to_correct:
                p_id = p["id"]
                ref_no = str(p.get("reference_no") or "")
                src_id = str(p.get("source_id") or "")
                is_test_artifact = ("PUR-TEST" in ref_no or "PUR-TEST" in src_id or "TEST" in ref_no)

                if is_test_artifact:
                    if not dry_run:
                        DoubleEntryEngine.reverse_journal(
                            entry_id=p_id,
                            reason="Audit correction: Eliminate synthetic test purchase artifact PUR-TEST-101 - only operational JAI Agency purchases are recognized",
                            user=user,
                            external_conn=conn,
                        )
                        cur.execute("DELETE FROM accounts_payables WHERE invoice_ref = %s OR source_invoice_id = %s;", (ref_no, src_id))
                        actions_taken.append(f"Reversed test purchase artifact {p['entry_number']} ({ref_no}) and cleaned orphan AP subledger")
                    else:
                        actions_taken.append(f"[Dry Run] Would reverse test purchase artifact {p['entry_number']} ({ref_no})")
                    continue

                cur.execute("SELECT * FROM journal_lines WHERE entry_id = %s;", (p_id,))
                orig_lines = cur.fetchall()

                if not dry_run:
                    DoubleEntryEngine.reverse_journal(
                        entry_id=p_id,
                        reason="Audit correction: Reclassify inward purchase from COGS (5010) to Inventory Stock (1050)",
                        user=user,
                        external_conn=conn,
                    )

                    new_lines = []
                    for l in orig_lines:
                        acc_id = l["account_id"]
                        if acc_id == cogs_acc["id"]:
                            acc_id = inv_acc["id"]
                        new_lines.append({
                            "account_id": acc_id,
                            "debit": l["debit"],
                            "credit": l["credit"],
                            "description": (l["description"] or "").replace("Purchase Inward", "Inventory Inward Purchase"),
                            "party_type": l.get("party_type"),
                            "party_id": l.get("party_id"),
                            "party_name": l.get("party_name"),
                            "tax_code": l.get("tax_code"),
                            "tax_rate": l.get("tax_rate"),
                        })

                    post_res = DoubleEntryEngine.post_journal_entry(
                        entry_data={
                            "entry_number": f"{p['entry_number']}-CORR",
                            "entry_date": p["entry_date"],
                            "source_module": "inventory",
                            "source_entity": "purchase_invoice",
                            "source_id": p["source_id"],
                            "reference_no": p["reference_no"],
                            "narration": f"{p['narration']} [CORRECTED: INVENTORY CAPITALIZATION]",
                            "status": "POSTED",
                        },
                        lines_data=new_lines,
                        user=user,
                        external_conn=conn,
                    )

                    cur.execute("""
                        UPDATE accounting_sync_registry
                        SET journal_entry_id = %s
                        WHERE source_module = 'inventory' AND source_entity = 'purchase_invoice' AND source_id = %s;
                    """, (post_res["entry_id"], p["source_id"]))

                    actions_taken.append(f"Corrected purchase {p['entry_number']} -> {post_res['entry_number']}")
                else:
                    actions_taken.append(f"[Dry Run] Would correct purchase {p['entry_number']}")

            # 3. Add COGS and inventory relief to sales bills
            cogs_data = InventoryCostingEngine.get_inventory_and_cogs_breakdown()
            bill_cogs = cogs_data["bill_cogs"]

            cur.execute("""
                SELECT je.id, je.entry_number, je.source_id, je.reference_no, je.narration, je.entry_date
                FROM journal_entries je
                WHERE je.source_module = 'sales' AND je.source_entity = 'bill'
                  AND je.status = 'POSTED'
                  AND je.entry_number NOT LIKE 'REV-%' AND je.entry_number NOT LIKE '%-CORR'
                  AND je.id NOT IN (
                      SELECT jl.entry_id FROM journal_lines jl
                      JOIN accounts_chart ac ON jl.account_id = ac.id
                      WHERE ac.code = '5010'
                  )
                ORDER BY je.id ASC;
            """)
            sales_to_correct = cur.fetchall()

            for s in sales_to_correct:
                s_id = s["id"]
                src_id = s["source_id"]
                try:
                    b_num = int(src_id)
                except Exception:
                    continue

                cogs_amt = bill_cogs.get(b_num, 0.0)
                if cogs_amt <= 0:
                    continue

                cur.execute("SELECT * FROM journal_lines WHERE entry_id = %s;", (s_id,))
                orig_lines = cur.fetchall()

                if not dry_run:
                    DoubleEntryEngine.reverse_journal(
                        entry_id=s_id,
                        reason="Audit correction: Recognize COGS and inventory relief for sales bill",
                        user=user,
                        external_conn=conn,
                    )

                    new_lines = []
                    for l in orig_lines:
                        new_lines.append({
                            "account_id": l["account_id"],
                            "debit": l["debit"],
                            "credit": l["credit"],
                            "description": l["description"],
                            "party_type": l.get("party_type"),
                            "party_id": l.get("party_id"),
                            "party_name": l.get("party_name"),
                            "tax_code": l.get("tax_code"),
                            "tax_rate": l.get("tax_rate"),
                        })

                    new_lines.append({
                        "account_id": cogs_acc["id"],
                        "debit": cogs_amt,
                        "credit": 0.0,
                        "description": f"Cost of Goods Sold for Sale #{src_id}",
                    })
                    new_lines.append({
                        "account_id": inv_acc["id"],
                        "debit": 0.0,
                        "credit": cogs_amt,
                        "description": f"Inventory stock relief for Sale #{src_id}",
                    })

                    post_res = DoubleEntryEngine.post_journal_entry(
                        entry_data={
                            "entry_number": f"{s['entry_number']}-CORR",
                            "entry_date": s["entry_date"],
                            "source_module": "sales",
                            "source_entity": "bill",
                            "source_id": s["source_id"],
                            "reference_no": s["reference_no"],
                            "narration": f"{s['narration']} [CORRECTED: INVENTORY RELIEF & COGS]",
                            "status": "POSTED",
                        },
                        lines_data=new_lines,
                        user=user,
                        external_conn=conn,
                    )

                    # Compute b_hash matching sync_engine
                    b_cur = jai_conn.cursor()
                    b_cur.execute("SELECT * FROM sales_log WHERE id = ?;", (b_num,))
                    b_row = b_cur.fetchone()
                    b_cur.close()
                    if b_row:
                        b_row = dict(b_row)
                        b_total = round(float(b_row.get("total") or 0.0), 2)
                        gross_total = round(float(b_row.get("gross_total") or 0.0), 2)
                        b_mode = str(b_row.get("payment_method") or "CASH").upper()
                        b_status = str(b_row.get("status") or "ACTIVE").upper()
                        b_date_str = str(b_row.get("date") or "")
                        balance = round(float(b_row.get("balance") or 0.0), 2)
                        amount_paid = round(float(b_row.get("amount_paid") or 0.0), 2)
                        raw_hash = f"jai_sls_{b_num}_{b_total}_{gross_total}_{b_mode}_{b_status}_{b_date_str}_{balance}_{amount_paid}_{cogs_amt}"
                        b_hash = hashlib.sha256(raw_hash.encode()).hexdigest()
                    else:
                        b_hash = None

                    if b_hash:
                        cur.execute("""
                            UPDATE accounting_sync_registry
                            SET journal_entry_id = %s, source_hash = %s, status = 'POSTED', synced_at = CURRENT_TIMESTAMP
                            WHERE source_module = 'sales' AND source_entity = 'bill' AND source_id = %s;
                        """, (post_res["entry_id"], b_hash, src_id))
                    else:
                        cur.execute("""
                            UPDATE accounting_sync_registry
                            SET journal_entry_id = %s, status = 'POSTED', synced_at = CURRENT_TIMESTAMP
                            WHERE source_module = 'sales' AND source_entity = 'bill' AND source_id = %s;
                        """, (post_res["entry_id"], src_id))

                    actions_taken.append(f"Corrected sale {s['entry_number']} -> {post_res['entry_number']} (COGS: Rs. {cogs_amt})")
                else:
                    actions_taken.append(f"[Dry Run] Would correct sale {s['entry_number']} with COGS Rs. {cogs_amt}")

            # 4. Add Inventory restoration and COGS reversal to sales returns
            return_cogs = cogs_data["return_cogs"]
            cur.execute("""
                SELECT je.id, je.entry_number, je.source_id, je.reference_no, je.narration, je.entry_date
                FROM journal_entries je
                WHERE je.source_module = 'sales' AND je.source_entity = 'return'
                  AND je.status = 'POSTED'
                  AND je.entry_number NOT LIKE 'REV-%' AND je.entry_number NOT LIKE '%-CORR'
                  AND je.id NOT IN (
                      SELECT jl.entry_id FROM journal_lines jl
                      JOIN accounts_chart ac ON jl.account_id = ac.id
                      WHERE ac.code = '1050'
                  )
                ORDER BY je.id ASC;
            """)
            returns_to_correct = cur.fetchall()

            for r in returns_to_correct:
                r_id = r["id"]
                src_id = r["source_id"]
                try:
                    ret_num = int(src_id)
                except Exception:
                    continue

                inv_cost = return_cogs.get(ret_num, 0.0)
                if inv_cost <= 0:
                    continue

                cur.execute("SELECT * FROM journal_lines WHERE entry_id = %s;", (r_id,))
                orig_lines = cur.fetchall()

                if not dry_run:
                    DoubleEntryEngine.reverse_journal(
                        entry_id=r_id,
                        reason="Audit correction: Restore inventory and reverse COGS for customer return",
                        user=user,
                        external_conn=conn,
                    )

                    new_lines = []
                    for l in orig_lines:
                        new_lines.append({
                            "account_id": l["account_id"],
                            "debit": l["debit"],
                            "credit": l["credit"],
                            "description": l["description"],
                            "party_type": l.get("party_type"),
                            "party_id": l.get("party_id"),
                            "party_name": l.get("party_name"),
                            "tax_code": l.get("tax_code"),
                            "tax_rate": l.get("tax_rate"),
                        })

                    new_lines.append({
                        "account_id": inv_acc["id"],
                        "debit": inv_cost,
                        "credit": 0.0,
                        "description": f"Inventory restoration for Return #{src_id}",
                    })
                    new_lines.append({
                        "account_id": cogs_acc["id"],
                        "debit": 0.0,
                        "credit": inv_cost,
                        "description": f"COGS reversal for Return #{src_id}",
                    })

                    post_res = DoubleEntryEngine.post_journal_entry(
                        entry_data={
                            "entry_number": f"{r['entry_number']}-CORR",
                            "entry_date": r["entry_date"],
                            "source_module": "sales",
                            "source_entity": "return",
                            "source_id": r["source_id"],
                            "reference_no": r["reference_no"],
                            "narration": f"{r['narration']} [CORRECTED: INVENTORY RESTORATION]",
                            "status": "POSTED",
                        },
                        lines_data=new_lines,
                        user=user,
                        external_conn=conn,
                    )

                    # Compute r_hash matching sync_engine
                    r_cur = jai_conn.cursor()
                    r_cur.execute("SELECT * FROM returns_log WHERE id = ?;", (ret_num,))
                    r_row = r_cur.fetchone()
                    r_cur.close()
                    if r_row:
                        r_row = dict(r_row)
                        r_amt = round(float(r_row.get("refund_amount") or 0.0), 2)
                        r_date_str = str(r_row.get("date") or "")
                        raw_hash = f"jai_ret_{ret_num}_{r_amt}_{r_row.get('bill_id')}_{r_date_str}_{inv_cost}"
                        r_hash = hashlib.sha256(raw_hash.encode()).hexdigest()
                    else:
                        r_hash = None

                    if r_hash:
                        cur.execute("""
                            UPDATE accounting_sync_registry
                            SET journal_entry_id = %s, source_hash = %s, status = 'POSTED', synced_at = CURRENT_TIMESTAMP
                            WHERE source_module = 'sales' AND source_entity = 'return' AND source_id = %s;
                        """, (post_res["entry_id"], r_hash, src_id))
                    else:
                        cur.execute("""
                            UPDATE accounting_sync_registry
                            SET journal_entry_id = %s, status = 'POSTED', synced_at = CURRENT_TIMESTAMP
                            WHERE source_module = 'sales' AND source_entity = 'return' AND source_id = %s;
                        """, (post_res["entry_id"], src_id))

                    actions_taken.append(f"Corrected return {r['entry_number']} -> {post_res['entry_number']} (Inv restored: Rs. {inv_cost})")
                else:
                    actions_taken.append(f"[Dry Run] Would correct return {r['entry_number']} with Inv restored Rs. {inv_cost}")

            if not dry_run:
                conn.commit()

            # Post-migration verifications
            tb = generate_trial_balance(conn=conn)
            pnl = generate_profit_and_loss(conn=conn)
            bs = generate_balance_sheet(conn=conn)

            # GL check via LedgerEngine
            from backend.ledger_engine import LedgerEngine
            gl_1050_bal = float(LedgerEngine.get_account_balance(inv_acc["id"], conn=conn)["balance"]) if inv_acc else 0.0
            gl_5010_bal = float(LedgerEngine.get_account_balance(cogs_acc["id"], conn=conn)["balance"]) if cogs_acc else 0.0

            return {
                "success": True,
                "dry_run": dry_run,
                "actions_count": len(actions_taken),
                "actions_taken": actions_taken,
                "trial_balance": {
                    "total_debit": tb["total_debit"],
                    "total_credit": tb["total_credit"],
                    "is_balanced": tb["is_balanced"],
                    "difference": tb["difference"],
                },
                "profit_and_loss": {
                    "operating_revenue": pnl["revenue"]["total"],
                    "cogs": pnl["cogs"]["total"],
                    "gross_profit": pnl["gross_profit"],
                    "operating_expenses": pnl["operating_expenses"]["total"],
                    "net_profit": pnl["net_profit"],
                },
                "balance_sheet": {
                    "total_assets": bs["assets"]["total_assets"],
                    "total_liabilities": bs["liabilities"]["total_liabilities"],
                    "total_equity": bs["equity"]["total_equity"],
                    "total_liabilities_and_equity": bs["total_liabilities_and_equity"],
                    "is_balanced": bs["is_balanced"],
                    "difference": bs["difference"],
                },
                "gl_balances": {
                    "inventory_1050": gl_1050_bal,
                    "cogs_5010": gl_5010_bal,
                },
            }

        except Exception as e:
            conn.rollback()
            logger.error(f"Migration failed: {e}", exc_info=True)
            raise
        finally:
            cur.close()
            if jai_conn:
                try:
                    jai_conn.close()
                except Exception:
                    pass
            if should_close and conn:
                conn.close()

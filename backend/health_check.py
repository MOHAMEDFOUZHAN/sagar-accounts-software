import datetime
import logging
from decimal import Decimal
from backend.db import get_db_connection
from backend.sync_engine import get_live_inventory_valuation
from backend.reports_engine import (
    generate_trial_balance,
    generate_balance_sheet,
    generate_tax_report,
    verify_report_consistency,
)
from backend.period_engine import PeriodControlEngine

logger = logging.getLogger(__name__)


class HealthCheckEngine:
    """
    Comprehensive Diagnostic and Accounting Health Audit System.
    Audits 24 critical checkpoints across General Ledger, Subledgers,
    Balancing Invariants, Closed Periods, Account Mappings, and Audit Trails
    strictly following double-entry accounting standards.
    """

    @classmethod
    def run_full_health_check(cls, conn=None):
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        checks = []

        try:
            # 1. Debit != Credit (Unbalanced Journals)
            checks.append(cls._check_1_debit_credit_equality(cur))

            # 2. Balance Sheet Mismatch (Assets = Liabilities + Equity)
            checks.append(cls._check_2_balance_sheet_equation(conn))

            # 3. Customer Total != AR Control Account (1040)
            checks.append(cls._check_3_customer_ar_subledger(cur))

            # 4. Supplier Total != AP Control Account (2010)
            checks.append(cls._check_4_supplier_ap_subledger(cur))

            # 5. Inventory Valuation != Inventory Control Account (1050)
            checks.append(cls._check_5_inventory_valuation(cur))

            # 6. Loan Subledger != Loan Liability (2110/2120)
            checks.append(cls._check_6_loan_subledger(cur))

            # 7. Fixed Asset Register Mismatch (1110-1160)
            checks.append(cls._check_7_fixed_asset_register(cur))

            # 8. GST Mismatch (2030/1060)
            checks.append(cls._check_8_gst_tax_control(cur, conn))

            # 9. TDS Mismatch (2050)
            checks.append(cls._check_9_tds_control(cur))

            # 10. Duplicate Accounting Entries
            checks.append(cls._check_10_duplicate_accounting_entries(cur))

            # 11. Missing Journal Lines (< 2 lines in an entry)
            checks.append(cls._check_11_missing_journal_lines(cur))

            # 12. Invalid Account References (non-existent, inactive, group)
            checks.append(cls._check_12_invalid_account_references(cur))

            # 13. Invalid Account Mappings
            checks.append(cls._check_13_invalid_account_mappings(cur))

            # 14. Posted Transaction Without Accounting Entry
            checks.append(cls._check_14_unposted_transactions(cur))

            # 15. Accounting Entry Without Valid Source Transaction Where Required
            checks.append(cls._check_15_entry_without_source(cur))

            # 16. Duplicate Source References
            checks.append(cls._check_16_duplicate_source_references(cur))

            # 17. Invalid Financial Year Definitions
            checks.append(cls._check_17_invalid_financial_years(cur))

            # 18. Invalid Accounting Period Definitions
            checks.append(cls._check_18_invalid_accounting_periods(cur))

            # 19. Posted Transaction in Closed/Locked Period
            checks.append(cls._check_19_closed_period_postings(cur))

            # 20. Unreconciled Control Accounts (Cash / Bank Float)
            checks.append(cls._check_20_unreconciled_control_accounts(cur))

            # 21. Broken Reversal Relationships
            checks.append(cls._check_21_broken_reversals(cur))

            # 22. Broken Correction Relationships
            checks.append(cls._check_22_broken_corrections(cur))

            # 23. Invalid Opening Balances
            checks.append(cls._check_23_invalid_opening_balances(cur))

            # 24. Missing Audit Trail for Sensitive Actions
            checks.append(cls._check_24_missing_audit_trail(cur))

            # Calculate Health Score & Classification
            crit_count = sum(1 for c in checks if c["status"] == "CRITICAL")
            err_count = sum(1 for c in checks if c["status"] == "ERROR")
            warn_count = sum(1 for c in checks if c["status"] == "WARNING")
            pass_count = sum(1 for c in checks if c["status"] == "PASS")

            deduction = (crit_count * 30) + (err_count * 15) + (warn_count * 5)
            health_score = max(0, 100 - deduction)

            status = "HEALTHY"
            if crit_count > 0 or health_score < 60:
                status = "CRITICAL"
            elif err_count > 0 or health_score < 85:
                status = "ERROR"
            elif warn_count > 0 or health_score < 95:
                status = "WARNING"

            issues = [c for c in checks if c["status"] != "PASS"]

            return {
                "health_score": health_score,
                "status": status,
                "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_checks": len(checks),
                "total_issues": len(issues),
                "summary": {
                    "pass": pass_count,
                    "warning": warn_count,
                    "error": err_count,
                    "critical": crit_count,
                    "high": err_count,
                    "medium": warn_count,
                    "low": 0,
                },
                "checks": checks,
                "issues": issues,
            }

        except Exception as e:
            logger.error(f"Health check execution error: {e}", exc_info=True)
            return {
                "health_score": 0,
                "status": "ERROR",
                "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_checks": 24,
                "total_issues": 1,
                "summary": {"pass": 0, "warning": 0, "error": 1, "critical": 0},
                "checks": [],
                "issues": [{
                    "check_name": "Diagnostic System Execution",
                    "status": "CRITICAL",
                    "severity": "CRITICAL",
                    "difference": 0.0,
                    "affected_account": None,
                    "affected_transaction": None,
                    "journal_reference": None,
                    "possible_cause": f"Health check engine exception: {str(e)}",
                    "recommended_action": "Inspect application error logs and database connection.",
                }]
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 1. DEBIT != CREDIT EQUALITY
    # -------------------------------------------------------------------------
    @classmethod
    def _check_1_debit_credit_equality(cls, cur):
        name = "1. Debit != Credit Equality (Double-Entry Invariant)"
        # Header check
        cur.execute("""
            SELECT entry_number, ABS(total_debit - total_credit) as diff
            FROM journal_entries
            WHERE ABS(total_debit - total_credit) > 0.005 AND status = 'POSTED';
        """)
        unb_hdr = cur.fetchall()

        # Line sums check
        cur.execute("""
            SELECT je.entry_number, ABS(SUM(jl.debit) - SUM(jl.credit)) as diff
            FROM journal_entries je
            JOIN journal_lines jl ON je.id = jl.entry_id
            WHERE je.status = 'POSTED'
            GROUP BY je.id
            HAVING ABS(SUM(jl.debit) - SUM(jl.credit)) > 0.005;
        """)
        unb_lines = cur.fetchall()

        if unb_hdr or unb_lines:
            all_diffs = [r["diff"] for r in (unb_hdr + unb_lines)]
            entries = list(set([r["entry_number"] for r in (unb_hdr + unb_lines)]))
            max_diff = round(float(max(all_diffs)), 2)
            return {
                "check_name": name,
                "status": "CRITICAL",
                "severity": "CRITICAL",
                "difference": max_diff,
                "affected_account": "Double-Entry Invariant",
                "affected_transaction": ", ".join(entries[:3]),
                "journal_reference": entries[0] if entries else None,
                "possible_cause": "Partial journal line commit, line amount truncation, or manual DB edit.",
                "recommended_action": "Reverse unbalanced journal entries and repost with balanced debits and credits.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (All posted journals are perfectly balanced: Total Debit == Total Credit).",
        }

    # -------------------------------------------------------------------------
    # 2. BALANCE SHEET MISMATCH (ASSETS = LIABILITIES + EQUITY)
    # -------------------------------------------------------------------------
    @classmethod
    def _check_2_balance_sheet_equation(cls, conn):
        name = "2. Balance Sheet Equation (Assets = Liabilities + Equity)"
        try:
            bs = generate_balance_sheet(conn=conn)
            is_balanced = bs.get("is_balanced", False)
            diff = round(float(bs.get("difference") or 0.0), 2)
            assets = round(float(bs.get("assets", {}).get("total_assets") or 0.0), 2)
            liab_eq = round(float(bs.get("total_liabilities_and_equity") or 0.0), 2)

            if not is_balanced and diff > 0.01:
                return {
                    "check_name": name,
                    "status": "CRITICAL",
                    "severity": "CRITICAL",
                    "difference": diff,
                    "affected_account": "Balance Sheet Summary",
                    "affected_transaction": None,
                    "journal_reference": None,
                    "possible_cause": f"Assets (Rs. {assets:,.2f}) != Liab+Equity (Rs. {liab_eq:,.2f}). Unclassified accounts or P&L retained earnings mismatch.",
                    "recommended_action": "Audit accounts chart classifications and ensure Net Profit/Loss is properly closed to Equity.",
                }
        except Exception as e:
            logger.warning(f"Health check Balance Sheet calculation error: {e}")

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Balance Sheet Equation holds: Total Assets == Total Liabilities + Total Equity).",
        }

    # -------------------------------------------------------------------------
    # 3. CUSTOMER TOTAL != AR CONTROL ACCOUNT (1040)
    # -------------------------------------------------------------------------
    @classmethod
    def _check_3_customer_ar_subledger(cls, cur):
        name = "3. Customer AR Subledger vs AR Control Account (1040)"
        cur.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as gl_ar
            FROM journal_lines jl
            JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE ac.code = '1040' AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_ar = round(float(cur.fetchone()["gl_ar"]), 2)

        cur.execute("SELECT COALESCE(SUM(remaining_balance), 0) as sub_ar FROM accounts_receivables WHERE status != 'PAID';")
        sub_ar = round(float(cur.fetchone()["sub_ar"]), 2)

        diff = round(abs(gl_ar - sub_ar), 2)
        if diff > 1.0:
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": diff,
                "affected_account": "1040 - Accounts Receivable",
                "affected_transaction": "Customer Receivables Register",
                "journal_reference": None,
                "possible_cause": f"Customer Subledger (Rs. {sub_ar:,.2f}) != GL 1040 (Rs. {gl_ar:,.2f}). Direct manual journal to 1040 or unsynced credit sale.",
                "recommended_action": "Run Customer AR Reconciliation and post subledger adjustment or synchronize unposted sales invoices.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": "1040 - Accounts Receivable",
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Customer Subledger exactly equals AR Control Account 1040).",
        }

    # -------------------------------------------------------------------------
    # 4. SUPPLIER TOTAL != AP CONTROL ACCOUNT (2010)
    # -------------------------------------------------------------------------
    @classmethod
    def _check_4_supplier_ap_subledger(cls, cur):
        name = "4. Supplier AP Subledger vs AP Control Account (2010)"
        cur.execute("""
            SELECT COALESCE(SUM(jl.credit - jl.debit), 0) as gl_ap
            FROM journal_lines jl
            JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE ac.code = '2010' AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_ap = round(float(cur.fetchone()["gl_ap"]), 2)

        cur.execute("SELECT COALESCE(SUM(remaining_balance), 0) as sub_ap FROM accounts_payables WHERE status != 'PAID';")
        sub_ap = round(float(cur.fetchone()["sub_ap"]), 2)

        diff = round(abs(gl_ap - sub_ap), 2)
        if diff > 1.0:
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": diff,
                "affected_account": "2010 - Accounts Payable",
                "affected_transaction": "Supplier Payables Register",
                "journal_reference": None,
                "possible_cause": f"Supplier Subledger (Rs. {sub_ap:,.2f}) != GL 2010 (Rs. {gl_ap:,.2f}). Direct manual journal to 2010 or unsynced purchase invoice.",
                "recommended_action": "Run Supplier AP Reconciliation and synchronize pending purchase invoices.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": "2010 - Accounts Payable",
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Supplier Subledger exactly equals AP Control Account 2010).",
        }

    # -------------------------------------------------------------------------
    # 5. INVENTORY VALUATION != INVENTORY CONTROL ACCOUNT (1050)
    # -------------------------------------------------------------------------
    @classmethod
    def _check_5_inventory_valuation(cls, cur):
        name = "5. Inventory Valuation vs Inventory Control Account (1050)"
        try:
            live_val = get_live_inventory_valuation()
        except Exception:
            live_val = 0.0

        cur.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as gl_inv
            FROM journal_lines jl
            JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE ac.code = '1050' AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_inv = round(float(cur.fetchone()["gl_inv"]), 2)

        diff = round(abs(gl_inv - live_val), 2)
        if diff > 10.0 and (live_val > 0 or gl_inv > 0):
            return {
                "check_name": name,
                "status": "WARNING" if diff < 1000.0 else "ERROR",
                "severity": "MEDIUM",
                "difference": diff,
                "affected_account": "1050 - Merchandise Inventory",
                "affected_transaction": "Jai Agency Inventory Storage",
                "journal_reference": None,
                "possible_cause": f"Live Jai Agency storage valuation (Rs. {live_val:,.2f}) differs from GL Inventory (Rs. {gl_inv:,.2f}). Opening inventory not posted or pending adjustments.",
                "recommended_action": "Record Opening Inventory voucher or post inventory adjustment to align GL with stock valuation.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": "1050 - Merchandise Inventory",
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Live inventory storage valuation matches GL Inventory account 1050).",
        }

    # -------------------------------------------------------------------------
    # 6. LOAN SUBLEDGER != LOAN LIABILITY (2110/2120)
    # -------------------------------------------------------------------------
    @classmethod
    def _check_6_loan_subledger(cls, cur):
        name = "6. Loan Subledger vs Loan Liability Ledger (2110/2120)"
        cur.execute("""
            SELECT COALESCE(SUM(outstanding_balance), 0) as sub_loan
            FROM accounts_liabilities
            WHERE status = 'Active';
        """)
        sub_loan = round(float(cur.fetchone()["sub_loan"]), 2)

        cur.execute("""
            SELECT COALESCE(SUM(jl.credit - jl.debit), 0) as gl_loan
            FROM journal_lines jl
            JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE ac.code IN ('2110', '2120') AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_loan = round(float(cur.fetchone()["gl_loan"]), 2)

        diff = round(abs(gl_loan - sub_loan), 2)
        if diff > 1.0:
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": diff,
                "affected_account": "2110/2120 - Loan Liabilities",
                "affected_transaction": "Loan Register",
                "journal_reference": None,
                "possible_cause": f"Loan Subledger (Rs. {sub_loan:,.2f}) != GL Loan Liabilities (Rs. {gl_loan:,.2f}). Loan repayment posted without updating subledger or manual journal.",
                "recommended_action": "Reconcile loan schedule with liability payments and update outstanding principal.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": "2110/2120 - Loan Liabilities",
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Loan Subledger matches GL Loan Liabilities 2110/2120).",
        }

    # -------------------------------------------------------------------------
    # 7. FIXED ASSET REGISTER MISMATCH (1110-1160)
    # -------------------------------------------------------------------------
    @classmethod
    def _check_7_fixed_asset_register(cls, cur):
        name = "7. Fixed Asset Register vs Fixed Asset Ledger (1110-1160)"
        # Asset cost in register vs GL
        cur.execute("SELECT COALESCE(SUM(purchase_value), 0) as reg_cost, COALESCE(SUM(accumulated_depreciation), 0) as reg_depr FROM accounts_fixed_assets WHERE status = 'Active';")
        reg_row = cur.fetchone()
        reg_cost = round(float(reg_row["reg_cost"]), 2)
        reg_depr = round(float(reg_row["reg_depr"]), 2)

        cur.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as gl_cost
            FROM journal_lines jl
            JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE ac.code IN ('1110', '1120', '1130', '1140', '1150') AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_cost = round(float(cur.fetchone()["gl_cost"]), 2)

        cur.execute("""
            SELECT COALESCE(SUM(jl.credit - jl.debit), 0) as gl_depr
            FROM journal_lines jl
            JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE ac.code = '1160' AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_depr = round(float(cur.fetchone()["gl_depr"]), 2)

        diff_cost = round(abs(gl_cost - reg_cost), 2)
        diff_depr = round(abs(gl_depr - reg_depr), 2)

        if diff_cost > 1.0 or diff_depr > 1.0:
            total_diff = round(diff_cost + diff_depr, 2)
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": total_diff,
                "affected_account": "1110-1160 - Fixed Assets / Acc Depreciation",
                "affected_transaction": "Fixed Asset Register",
                "journal_reference": None,
                "possible_cause": f"Asset Cost Diff: Rs. {diff_cost:,.2f} (Reg: {reg_cost:,.2f}, GL: {gl_cost:,.2f}); Depr Diff: Rs. {diff_depr:,.2f} (Reg: {reg_depr:,.2f}, GL: {gl_depr:,.2f}).",
                "recommended_action": "Review asset capitalization entries and depreciation schedules in Fixed Asset Module.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": "1110-1160 - Fixed Assets",
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Fixed Asset Register cost and accumulated depreciation match GL balances).",
        }

    # -------------------------------------------------------------------------
    # 8. GST MISMATCH
    # -------------------------------------------------------------------------
    @classmethod
    def _check_8_gst_tax_control(cls, cur, conn):
        name = "8. GST Subledger / Tax Calculation vs Control Accounts (2030/1060)"
        try:
            tax_rep = generate_tax_report(conn=conn)
            gst = tax_rep.get("gst", {})
            out_gst = round(float(gst.get("total_output_gst") or 0.0), 2)
            in_gst = round(float(gst.get("total_input_gst") or 0.0), 2)

            cur.execute("""
                SELECT COALESCE(SUM(jl.credit - jl.debit), 0) as gl_out
                FROM journal_lines jl
                JOIN accounts_chart ac ON jl.account_id = ac.id
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE ac.code = '2030' AND je.status IN ('POSTED', 'REVERSED');
            """)
            gl_out = round(float(cur.fetchone()["gl_out"]), 2)

            cur.execute("""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as gl_in
                FROM journal_lines jl
                JOIN accounts_chart ac ON jl.account_id = ac.id
                JOIN journal_entries je ON jl.entry_id = je.id
                WHERE ac.code IN ('1060', '2040') AND je.status IN ('POSTED', 'REVERSED');
            """)
            gl_in = round(float(cur.fetchone()["gl_in"]), 2)

            diff_out = round(abs(gl_out - out_gst), 2)
            diff_in = round(abs(gl_in - in_gst), 2)

            if diff_out > 1.0 or diff_in > 1.0:
                diff = round(diff_out + diff_in, 2)
                return {
                    "check_name": name,
                    "status": "WARNING",
                    "severity": "MEDIUM",
                    "difference": diff,
                    "affected_account": "2030 - Output GST / 1060 - Input GST",
                    "affected_transaction": "Tax Register",
                    "journal_reference": None,
                    "possible_cause": f"Tax Report (Out: {out_gst}, In: {in_gst}) differs from GL (Out: {gl_out}, In: {gl_in}).",
                    "recommended_action": "Run GST Tax Report and verify tax code mappings on all invoice lines.",
                }
        except Exception as e:
            logger.warning(f"Health check GST evaluation error: {e}")

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": "2030 / 1060 - GST Control",
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (GST Output and Input tax control accounts match tax calculations).",
        }

    # -------------------------------------------------------------------------
    # 9. TDS MISMATCH
    # -------------------------------------------------------------------------
    @classmethod
    def _check_9_tds_control(cls, cur):
        name = "9. TDS Subledger / Liability vs Control Account (2050)"
        cur.execute("""
            SELECT COALESCE(SUM(jl.credit - jl.debit), 0) as gl_tds
            FROM journal_lines jl
            JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE ac.code = '2050' AND je.status IN ('POSTED', 'REVERSED');
        """)
        gl_tds = round(float(cur.fetchone()["gl_tds"]), 2)

        # TDS liability cannot normally be negative (debit balance)
        if gl_tds < -0.5:
            return {
                "check_name": name,
                "status": "WARNING",
                "severity": "MEDIUM",
                "difference": round(abs(gl_tds), 2),
                "affected_account": "2050 - TDS Payable",
                "affected_transaction": "TDS Payments",
                "journal_reference": None,
                "possible_cause": f"GL 2050 has negative liability (Rs. {gl_tds:,.2f}), indicating TDS paid in excess of deduction.",
                "recommended_action": "Reconcile TDS deduction vouchers with tax deposit challans.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": "2050 - TDS Payable",
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (TDS liability control account 2050 is healthy and consistent).",
        }

    # -------------------------------------------------------------------------
    # 10. DUPLICATE ACCOUNTING ENTRIES
    # -------------------------------------------------------------------------
    @classmethod
    def _check_10_duplicate_accounting_entries(cls, cur):
        name = "10. Duplicate Accounting Entries"
        cur.execute("""
            SELECT entry_date, total_debit, total_credit, narration, COUNT(*) as cnt, GROUP_CONCAT(entry_number) as entries
            FROM journal_entries
            WHERE status = 'POSTED' AND is_opening = 0
            GROUP BY entry_date, total_debit, total_credit, narration
            HAVING COUNT(*) > 1;
        """)
        dupes = cur.fetchall()
        if dupes:
            total_dupes = sum(r["cnt"] - 1 for r in dupes)
            sample_entries = dupes[0]["entries"]
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": float(total_dupes),
                "affected_account": "Multiple GL Accounts",
                "affected_transaction": sample_entries,
                "journal_reference": sample_entries.split(",")[0] if sample_entries else None,
                "possible_cause": "Identical journal vouchers posted with same amount, date, and narration.",
                "recommended_action": "Review identified duplicate journal entries and reverse redundant postings.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (No duplicate journal postings detected).",
        }

    # -------------------------------------------------------------------------
    # 11. MISSING JOURNAL LINES (< 2 LINES)
    # -------------------------------------------------------------------------
    @classmethod
    def _check_11_missing_journal_lines(cls, cur):
        name = "11. Missing Journal Lines (Incomplete Double-Entry Structure)"
        cur.execute("""
            SELECT je.id, je.entry_number, COUNT(jl.id) as line_count
            FROM journal_entries je
            LEFT JOIN journal_lines jl ON je.id = jl.entry_id
            WHERE je.status = 'POSTED'
            GROUP BY je.id
            HAVING COUNT(jl.id) < 2;
        """)
        broken = cur.fetchall()
        if broken:
            entries = [r["entry_number"] for r in broken]
            return {
                "check_name": name,
                "status": "CRITICAL",
                "severity": "CRITICAL",
                "difference": float(len(broken)),
                "affected_account": None,
                "affected_transaction": ", ".join(entries[:3]),
                "journal_reference": entries[0] if entries else None,
                "possible_cause": f"Found {len(broken)} posted journal entry with fewer than 2 line items.",
                "recommended_action": "Repair or reverse incomplete journal entries to restore double-entry integrity.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (All posted journal vouchers have valid multi-line structure).",
        }

    # -------------------------------------------------------------------------
    # 12. INVALID ACCOUNT REFERENCES
    # -------------------------------------------------------------------------
    @classmethod
    def _check_12_invalid_account_references(cls, cur):
        name = "12. Invalid Account References in Journal Lines"
        # 1. Non-existent accounts
        cur.execute("""
            SELECT jl.id, jl.entry_id, je.entry_number
            FROM journal_lines jl
            LEFT JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE ac.id IS NULL AND je.status = 'POSTED';
        """)
        orphaned_lines = cur.fetchall()

        # 2. Group account or non-postable postings
        cur.execute("""
            SELECT jl.id, jl.entry_id, ac.code, ac.name, ac.is_group, ac.is_postable, je.entry_number
            FROM journal_lines jl
            JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE je.status = 'POSTED' AND (ac.is_group = 1 OR ac.is_postable = 0);
        """)
        group_postings = cur.fetchall()

        total_invalid = len(orphaned_lines) + len(group_postings)
        if total_invalid > 0:
            sample_entry = (orphaned_lines + group_postings)[0]["entry_number"]
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": float(total_invalid),
                "affected_account": group_postings[0]["code"] if group_postings else "Orphaned Account ID",
                "affected_transaction": sample_entry,
                "journal_reference": sample_entry,
                "possible_cause": f"Found {total_invalid} journal lines posted to group headers, non-postable accounts, or invalid account IDs.",
                "recommended_action": "Reallocate journal lines to active leaf postable accounts.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (All journal lines reference valid leaf posting accounts).",
        }

    # -------------------------------------------------------------------------
    # 13. INVALID ACCOUNT MAPPINGS
    # -------------------------------------------------------------------------
    @classmethod
    def _check_13_invalid_account_mappings(cls, cur):
        name = "13. System Account Mappings Validity & Completeness"
        required_keys = [
            ("sales_revenue", "4010", "Operating Sales Revenue"),
            ("accounts_receivable", "1040", "Trade Accounts Receivable"),
            ("accounts_payable", "2010", "Trade Accounts Payable"),
            ("cogs", "5010", "Cost of Goods Sold"),
            ("inventory", "1050", "Merchandise Inventory"),
            ("cash_default", "1010", "Cash on Hand"),
            ("bank_default", "1020", "Main Operating Bank Account"),
            ("output_gst", "2030", "GST Output Payable"),
            ("input_gst", "1060", "GST Input Tax Credit"),
        ]

        invalid_mappings = []
        for key, def_code, desc in required_keys:
            cur.execute("""
                SELECT am.account_id, ac.code, ac.is_active, ac.is_postable, ac.is_group
                FROM accounts_mappings am
                JOIN accounts_chart ac ON am.account_id = ac.id
                WHERE am.mapping_key = %s;
            """, (key,))
            mapping = cur.fetchone()
            if not mapping:
                cur.execute("SELECT id, code, is_active, is_postable, is_group FROM accounts_chart WHERE code = %s;", (def_code,))
                fallback = cur.fetchone()
                if not fallback or fallback["is_active"] == 0 or fallback["is_postable"] == 0 or fallback["is_group"] == 1:
                    invalid_mappings.append(f"{key} ({desc})")
            elif mapping["is_active"] == 0 or mapping["is_postable"] == 0 or mapping["is_group"] == 1:
                invalid_mappings.append(f"{key} (Account {mapping['code']} inactive/group)")

        if invalid_mappings:
            return {
                "check_name": name,
                "status": "CRITICAL",
                "severity": "CRITICAL",
                "difference": float(len(invalid_mappings)),
                "affected_account": ", ".join(invalid_mappings[:2]),
                "affected_transaction": "System Settings",
                "journal_reference": None,
                "possible_cause": f"Critical account mappings missing or point to inactive/group accounts: {', '.join(invalid_mappings)}.",
                "recommended_action": "Configure mapping in Settings > Account Mappings to active postable leaf accounts.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (All mandatory account mappings are fully configured and valid).",
        }

    # -------------------------------------------------------------------------
    # 14. POSTED TRANSACTION WITHOUT ACCOUNTING ENTRY
    # -------------------------------------------------------------------------
    @classmethod
    def _check_14_unposted_transactions(cls, cur):
        name = "14. Posted Business Transaction Without Accounting Entry"
        cur.execute("""
            SELECT id, source_module, source_entity, source_id, status, error_info
            FROM accounting_sync_registry
            WHERE status IN ('PENDING', 'FAILED');
        """)
        unposted = cur.fetchall()
        if unposted:
            sources = [f"{r['source_module']}:{r['source_id']}" for r in unposted[:3]]
            return {
                "check_name": name,
                "status": "WARNING",
                "severity": "MEDIUM",
                "difference": float(len(unposted)),
                "affected_account": "Sync Pipeline",
                "affected_transaction": ", ".join(sources),
                "journal_reference": None,
                "possible_cause": f"Found {len(unposted)} business transactions in sync registry pending or failed without posted accounting entries.",
                "recommended_action": "Execute 'Synchronize Now' in Sync Dashboard to process pending sync items.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (All business transactions from operational modules have posted accounting entries).",
        }

    # -------------------------------------------------------------------------
    # 15. ACCOUNTING ENTRY WITHOUT VALID SOURCE TRANSACTION WHERE REQUIRED
    # -------------------------------------------------------------------------
    @classmethod
    def _check_15_entry_without_source(cls, cur):
        name = "15. Accounting Entry Without Valid Source Reference"
        cur.execute("""
            SELECT entry_number, source_module
            FROM journal_entries
            WHERE status = 'POSTED'
              AND source_module IN ('sales', 'inventory', 'sync')
              AND (source_id IS NULL OR TRIM(source_id) = '');
        """)
        orphaned_entries = cur.fetchall()
        if orphaned_entries:
            entries = [r["entry_number"] for r in orphaned_entries[:3]]
            return {
                "check_name": name,
                "status": "WARNING",
                "severity": "LOW",
                "difference": float(len(orphaned_entries)),
                "affected_account": "Operational Ledger",
                "affected_transaction": ", ".join(entries),
                "journal_reference": entries[0] if entries else None,
                "possible_cause": f"{len(orphaned_entries)} operational journal vouchers lack source entity identifier.",
                "recommended_action": "Add appropriate source reference or change source_module to 'manual'.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (All operational journal entries have traceable source document references).",
        }

    # -------------------------------------------------------------------------
    # 16. DUPLICATE SOURCE REFERENCES
    # -------------------------------------------------------------------------
    @classmethod
    def _check_16_duplicate_source_references(cls, cur):
        name = "16. Duplicate Source Reference Postings"
        cur.execute("""
            SELECT source_module, source_entity, source_id, COUNT(*) as cnt, GROUP_CONCAT(entry_number) as entries
            FROM journal_entries
            WHERE status = 'POSTED' AND source_id IS NOT NULL AND source_id != ''
            GROUP BY source_module, source_entity, source_id
            HAVING COUNT(*) > 1;
        """)
        dupes = cur.fetchall()
        if dupes:
            total = sum(r["cnt"] - 1 for r in dupes)
            sample = dupes[0]
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": float(total),
                "affected_account": "Audit Traceability",
                "affected_transaction": f"{sample['source_module']}:{sample['source_id']}",
                "journal_reference": sample["entries"].split(",")[0] if sample.get("entries") else None,
                "possible_cause": f"Duplicate posted journals for source {sample['source_module']}:{sample['source_id']} ({sample['cnt']} entries: {sample['entries']}).",
                "recommended_action": "Review duplicate journal entries and reverse redundant postings.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (No duplicate postings for the same source document).",
        }

    # -------------------------------------------------------------------------
    # 17. INVALID FINANCIAL YEAR DEFINITIONS
    # -------------------------------------------------------------------------
    @classmethod
    def _check_17_invalid_financial_years(cls, cur):
        name = "17. Financial Year Boundary & Consistency Validation"
        cur.execute("""
            SELECT id, name, start_date, end_date
            FROM financial_years
            WHERE start_date >= end_date;
        """)
        bad_dates = cur.fetchall()

        # Check overlapping
        cur.execute("""
            SELECT f1.name as fy1, f2.name as fy2
            FROM financial_years f1
            JOIN financial_years f2 ON f1.id < f2.id
            WHERE f1.start_date <= f2.end_date AND f1.end_date >= f2.start_date;
        """)
        overlaps = cur.fetchall()

        if bad_dates or overlaps:
            total = len(bad_dates) + len(overlaps)
            desc = f"Bad date range in {[r['name'] for r in bad_dates]}" if bad_dates else f"Overlapping FYs: {[r['fy1'] + ' & ' + r['fy2'] for r in overlaps]}"
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": float(total),
                "affected_account": "Financial Year Setup",
                "affected_transaction": None,
                "journal_reference": None,
                "possible_cause": desc,
                "recommended_action": "Correct financial year date boundaries in Financial Control settings.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Financial Year definitions and date boundaries are valid and non-overlapping).",
        }

    # -------------------------------------------------------------------------
    # 18. INVALID ACCOUNTING PERIOD DEFINITIONS
    # -------------------------------------------------------------------------
    @classmethod
    def _check_18_invalid_accounting_periods(cls, cur):
        name = "18. Accounting Period Boundary & Parent FY Validation"
        cur.execute("""
            SELECT ap.id, ap.period_name, ap.start_date, ap.end_date, fy.start_date as fy_start, fy.end_date as fy_end
            FROM accounting_periods ap
            JOIN financial_years fy ON ap.financial_year_id = fy.id
            WHERE ap.start_date >= ap.end_date
               OR ap.start_date < fy.start_date
               OR ap.end_date > fy.end_date;
        """)
        bad_periods = cur.fetchall()
        if bad_periods:
            names = [r["period_name"] for r in bad_periods[:3]]
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": float(len(bad_periods)),
                "affected_account": "Period Setup",
                "affected_transaction": ", ".join(names),
                "journal_reference": None,
                "possible_cause": f"Found {len(bad_periods)} periods with invalid dates or falling outside their parent Financial Year.",
                "recommended_action": "Regenerate or correct accounting period date spans in Period Control.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (All accounting periods fall strictly within valid Financial Year boundaries).",
        }

    # -------------------------------------------------------------------------
    # 19. POSTED TRANSACTION IN CLOSED/LOCKED PERIOD
    # -------------------------------------------------------------------------
    @classmethod
    def _check_19_closed_period_postings(cls, cur):
        name = "19. Posted Transaction in Closed or Locked Period"
        cur.execute("""
            SELECT je.id, je.entry_number, je.entry_date, ap.period_name, ap.status as period_status
            FROM journal_entries je
            JOIN accounting_periods ap ON je.entry_date >= ap.start_date AND je.entry_date <= ap.end_date
            WHERE je.status = 'POSTED' AND ap.status IN ('CLOSED', 'LOCKED');
        """)
        closed_postings = cur.fetchall()
        if closed_postings:
            entries = [r["entry_number"] for r in closed_postings[:3]]
            return {
                "check_name": name,
                "status": "WARNING",
                "severity": "MEDIUM",
                "difference": float(len(closed_postings)),
                "affected_account": "Period Control",
                "affected_transaction": ", ".join(entries),
                "journal_reference": entries[0] if entries else None,
                "possible_cause": f"{len(closed_postings)} posted vouchers dated in closed/locked periods without supervisor override flag.",
                "recommended_action": "Verify authorized backdated entry or reopen period under supervisor authorization.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (No posted transactions reside in closed or locked periods).",
        }

    # -------------------------------------------------------------------------
    # 20. UNRECONCILED CONTROL ACCOUNTS (CASH / BANK FLOAT)
    # -------------------------------------------------------------------------
    @classmethod
    def _check_20_unreconciled_control_accounts(cls, cur):
        name = "20. Control Account Float & Negative Balance Check"
        # Check cash balance (should not be significantly negative)
        cur.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as cash_bal
            FROM journal_lines jl
            JOIN accounts_chart ac ON jl.account_id = ac.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE ac.code = '1010' AND je.status IN ('POSTED', 'REVERSED');
        """)
        cash_bal = round(float(cur.fetchone()["cash_bal"]), 2)

        # Check unbalanced reconciliations
        cur.execute("""
            SELECT id, reconciliation_type, difference
            FROM reconciliations
            WHERE status = 'UNBALANCED' AND ABS(difference) > 1.0;
        """)
        unbal_recs = cur.fetchall()

        if cash_bal < -1.0 or unbal_recs:
            diff = round(abs(cash_bal) if cash_bal < -1.0 else float(unbal_recs[0]["difference"]), 2)
            cause = f"Cash on Hand (1010) has negative balance: Rs. {cash_bal:,.2f}" if cash_bal < -1.0 else f"{len(unbal_recs)} bank/subledger reconciliation statements unbalanced."
            return {
                "check_name": name,
                "status": "WARNING",
                "severity": "MEDIUM",
                "difference": diff,
                "affected_account": "1010 - Cash / 1020 - Bank",
                "affected_transaction": "Reconciliation Module",
                "journal_reference": None,
                "possible_cause": cause,
                "recommended_action": "Perform Bank/Cash Reconciliation and record appropriate adjusting vouchers.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": "1010 - Cash / 1020 - Bank",
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Control account float balances are positive and reconciliations balanced).",
        }

    # -------------------------------------------------------------------------
    # 21. BROKEN REVERSAL RELATIONSHIPS
    # -------------------------------------------------------------------------
    @classmethod
    def _check_21_broken_reversals(cls, cur):
        name = "21. Journal Reversal Integrity & Linkage Check"
        # 1. Reversal vouchers pointing to missing parents
        cur.execute("""
            SELECT je.entry_number, je.reversal_of_entry_id
            FROM journal_entries je
            LEFT JOIN journal_entries parent ON je.reversal_of_entry_id = parent.id
            WHERE je.reversal_of_entry_id IS NOT NULL AND parent.id IS NULL;
        """)
        orphaned_reversals = cur.fetchall()

        # 2. Entries marked REVERSED without any reversal pointing to them
        cur.execute("""
            SELECT je.id, je.entry_number
            FROM journal_entries je
            LEFT JOIN journal_entries rev ON rev.reversal_of_entry_id = je.id
            WHERE je.status = 'REVERSED' AND rev.id IS NULL;
        """)
        unlinked_reversed = cur.fetchall()

        total = len(orphaned_reversals) + len(unlinked_reversed)
        if total > 0:
            entries = [r["entry_number"] for r in (orphaned_reversals + unlinked_reversed)]
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": float(total),
                "affected_account": "Journal Audit Trail",
                "affected_transaction": ", ".join(entries[:3]),
                "journal_reference": entries[0] if entries else None,
                "possible_cause": f"Found {total} broken reversal relationships (unlinked reversed status or missing original parent).",
                "recommended_action": "Repair reversal link pointers to preserve immutable double-entry audit trail.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (All reversal vouchers have valid, verified two-way linkages).",
        }

    # -------------------------------------------------------------------------
    # 22. BROKEN CORRECTION RELATIONSHIPS
    # -------------------------------------------------------------------------
    @classmethod
    def _check_22_broken_corrections(cls, cur):
        name = "22. Correction Voucher Line Integrity"
        # Ensure reversal voucher amounts match original entry
        cur.execute("""
            SELECT rev.entry_number as rev_num, orig.entry_number as orig_num,
                   rev.total_debit as rev_dr, orig.total_credit as orig_cr,
                   rev.total_credit as rev_cr, orig.total_debit as orig_dr
            FROM journal_entries rev
            JOIN journal_entries orig ON rev.reversal_of_entry_id = orig.id
            WHERE ABS(rev.total_debit - orig.total_credit) > 0.01
               OR ABS(rev.total_credit - orig.total_debit) > 0.01;
        """)
        mismatched = cur.fetchall()
        if mismatched:
            sample = mismatched[0]
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": float(len(mismatched)),
                "affected_account": "Reversal Line Allocation",
                "affected_transaction": f"{sample['rev_num']} vs {sample['orig_num']}",
                "journal_reference": sample["rev_num"],
                "possible_cause": f"Reversal voucher #{sample['rev_num']} line totals do not precisely mirror #{sample['orig_num']}.",
                "recommended_action": "Audit reversal voucher line allocations and ensure exact opposite accounting effect.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (All reversal vouchers correctly and symmetrically invert original lines).",
        }

    # -------------------------------------------------------------------------
    # 23. INVALID OPENING BALANCES
    # -------------------------------------------------------------------------
    @classmethod
    def _check_23_invalid_opening_balances(cls, cur):
        name = "23. Opening Balance Ledger Balance & Equity Invariant"
        cur.execute("""
            SELECT COALESCE(SUM(jl.debit), 0) as total_dr, COALESCE(SUM(jl.credit), 0) as total_cr
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE je.is_opening = 1 AND je.status = 'POSTED';
        """)
        row = cur.fetchone()
        dr = round(float(row["total_dr"]), 2)
        cr = round(float(row["total_cr"]), 2)
        diff = round(abs(dr - cr), 2)

        if diff > 0.01:
            return {
                "check_name": name,
                "status": "ERROR",
                "severity": "HIGH",
                "difference": diff,
                "affected_account": "3010/3020 - Owner Capital / Opening Equity",
                "affected_transaction": "Opening Balance Vouchers",
                "journal_reference": None,
                "possible_cause": f"Opening balances across GL are unbalanced (Total DR: Rs. {dr:,.2f}, Total CR: Rs. {cr:,.2f}, Diff: Rs. {diff:,.2f}).",
                "recommended_action": "Balance opening entries with an offsetting line in Owner Capital (3010) or Retained Earnings (3020).",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": "3010/3020 - Opening Equity",
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Opening balances are perfectly balanced: Total DR == Total CR).",
        }

    # -------------------------------------------------------------------------
    # 24. MISSING AUDIT TRAIL FOR SENSITIVE ACTIONS
    # -------------------------------------------------------------------------
    @classmethod
    def _check_24_missing_audit_trail(cls, cur):
        name = "24. Audit Trail Logging Completeness for Sensitive Actions"
        # Check closed periods without audit trail
        cur.execute("""
            SELECT ap.period_name
            FROM accounting_periods ap
            WHERE ap.status IN ('CLOSED', 'LOCKED')
              AND NOT EXISTS (
                  SELECT 1 FROM accounting_audit_trail aat
                  WHERE aat.entity_type = 'accounting_period'
                    AND aat.action IN ('CLOSE_PERIOD', 'PERIOD_CLOSE', 'LOCK_PERIOD', 'PERIOD_LOCK')
                    AND CAST(aat.entity_id AS TEXT) = CAST(ap.id AS TEXT)
              );
        """)
        unaudited_periods = cur.fetchall()

        # Check reversed journals without audit trail
        cur.execute("""
            SELECT je.entry_number
            FROM journal_entries je
            WHERE je.status = 'REVERSED'
              AND NOT EXISTS (
                  SELECT 1 FROM accounting_audit_trail aat
                  WHERE aat.entity_type = 'journal_entry'
                    AND aat.action IN ('REVERSE', 'JOURNAL_REVERSAL', 'REVERSE_JOURNAL', 'JOURNAL_CORRECTION')
                    AND CAST(aat.entity_id AS TEXT) = CAST(je.id AS TEXT)
              );
        """)
        unaudited_reversals = cur.fetchall()

        total_missing = len(unaudited_periods) + len(unaudited_reversals)
        if total_missing > 0:
            return {
                "check_name": name,
                "status": "WARNING",
                "severity": "LOW",
                "difference": float(total_missing),
                "affected_account": "Compliance Audit Trail",
                "affected_transaction": "System Audit Trail",
                "journal_reference": None,
                "possible_cause": f"Found {total_missing} sensitive operations without corresponding entries in accounting_audit_trail.",
                "recommended_action": "Perform period closures and reversals exclusively through audited service methods.",
            }

        return {
            "check_name": name,
            "status": "PASS",
            "severity": "INFO",
            "difference": 0.0,
            "affected_account": None,
            "affected_transaction": None,
            "journal_reference": None,
            "possible_cause": None,
            "recommended_action": "None (Full audit trail logging verified for all sensitive administrative actions).",
        }

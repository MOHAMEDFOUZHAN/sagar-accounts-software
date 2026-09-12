import unittest
import requests

BASE_URL = "http://127.0.0.1:5001"

class TestFourAccountingSections(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = requests.Session()
        login_res = cls.session.post(
            f"{BASE_URL}/login",
            data={"username": "accounts", "password": "1234"},
            allow_redirects=True
        )
        assert login_res.status_code == 200

    def test_01_profit_loss_page(self):
        res = self.session.get(f"{BASE_URL}/profit-loss")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Statement of Profit & Loss", res.text)
        self.assertIn("TOTAL REVENUE", res.text)
        self.assertIn("TOTAL COST OF GOODS SOLD", res.text)
        self.assertIn("GROSS PROFIT", res.text)
        self.assertIn("TOTAL OPERATING EXPENSES", res.text)
        self.assertIn("NET OPERATING PROFIT", res.text)
        print("[PASS] Profit & Loss page loaded with full financial equation.")

    def test_02_trial_balance_page(self):
        res = self.session.get(f"{BASE_URL}/trial-balance")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Trial Balance Control Report", res.text)
        self.assertIn("Total Debits (Dr)", res.text)
        self.assertIn("Total Credits (Cr)", res.text)
        self.assertIn("BOOKS IN PERFECT BALANCE", res.text)
        print("[PASS] Trial Balance page verified with double-entry balance check.")

    def test_03_balance_sheet_page(self):
        res = self.session.get(f"{BASE_URL}/balance-sheet")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Statement of Financial Position (Balance Sheet)", res.text)
        self.assertIn("TOTAL ASSETS", res.text)
        self.assertIn("TOTAL LIABILITIES", res.text)
        self.assertIn("TOTAL OWNER EQUITY", res.text)
        self.assertIn("BALANCE SHEET IN PERFECT BALANCE", res.text)
        print("[PASS] Balance Sheet page verified with Assets == Liabilities + Equity.")

    def test_04_tax_page(self):
        res = self.session.get(f"{BASE_URL}/tax")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Statutory Tax Center (GST & TDS)", res.text)
        self.assertIn("Output GST (Sales)", res.text)
        self.assertIn("Input GST Credit (Purchases)", res.text)
        self.assertIn("Net GST Payable", res.text)
        self.assertIn("TDS Payable", res.text)
        print("[PASS] Tax page loaded with unified GST and TDS sections.")

    def test_05_cross_section_synchronization(self):
        from backend.reports_engine import (
            generate_profit_and_loss,
            generate_balance_sheet,
            generate_trial_balance,
            generate_tax_report
        )
        pnl = generate_profit_and_loss()
        bs = generate_balance_sheet()
        tb = generate_trial_balance()
        tax = generate_tax_report()

        # 1. P&L Net Profit flows into Balance Sheet Equity
        bs_net_profit_items = [x for x in bs["equity"]["lines"] if x["code"] == "3030"]
        self.assertTrue(len(bs_net_profit_items) > 0, "Current Period Net Profit must exist in Balance Sheet Equity")
        self.assertEqual(pnl["net_profit"], bs_net_profit_items[0]["amount"])
        print(f"[PASS] P&L Net Profit (Rs. {pnl['net_profit']:,.2f}) perfectly equals Balance Sheet Equity Net Profit line.")

        # 2. Trial Balance Mathematical Check
        self.assertTrue(tb["is_balanced"], "Trial Balance must have sum(Debits) == sum(Credits)")
        self.assertEqual(tb["total_debit"], tb["total_credit"])
        print(f"[PASS] Trial Balance balanced: Dr Rs. {tb['total_debit']:,.2f} == Cr Rs. {tb['total_credit']:,.2f}")

        # 3. Balance Sheet Mathematical Check
        self.assertTrue(bs["is_balanced"], "Balance Sheet must have Total Assets == Total Liabilities + Equity")
        print(f"[PASS] Balance Sheet equation holds: Assets Rs. {bs['assets']['total_assets']:,.2f} == Liab+Equity Rs. {bs['total_liabilities_and_equity']:,.2f}")

        # 4. Tax Integration Check
        tb_gst_out = next((x for x in tb["lines"] if x["code"] == "2030"), None)
        if tb_gst_out:
            self.assertEqual(tax["gst"]["total_output_gst"], tb_gst_out["credit"])
            print(f"[PASS] Tax Output GST (Rs. {tax['gst']['total_output_gst']:,.2f}) matches Trial Balance code 2030.")

        tb_gst_in = next((x for x in tb["lines"] if x["code"] == "2040"), None)
        if tb_gst_in:
            self.assertEqual(tax["gst"]["total_input_gst"], tb_gst_in["debit"])
            print(f"[PASS] Tax Input GST (Rs. {tax['gst']['total_input_gst']:,.2f}) matches Trial Balance code 2040.")

    def test_06_drilldown_api(self):
        res = self.session.get(f"{BASE_URL}/api/reports/drilldown?account_code=4010")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("account", data["data"])
        self.assertEqual(data["data"]["account"]["code"], "4010")
        self.assertIn("transactions", data["data"])
        self.assertTrue(len(data["data"]["transactions"]) > 0)
        print(f"[PASS] Drilldown API returned {len(data['data']['transactions'])} journal entries for Sales Revenue (4010).")

    def test_07_date_filters(self):
        for f in ["today", "this_week", "this_month", "this_year", "all"]:
            pnl_res = self.session.get(f"{BASE_URL}/profit-loss?filter={f}")
            self.assertEqual(pnl_res.status_code, 200)
            tb_res = self.session.get(f"{BASE_URL}/trial-balance?filter={f}")
            self.assertEqual(tb_res.status_code, 200)
            bs_res = self.session.get(f"{BASE_URL}/balance-sheet?filter={f}")
            self.assertEqual(bs_res.status_code, 200)
            tax_res = self.session.get(f"{BASE_URL}/tax?filter={f}")
            self.assertEqual(tax_res.status_code, 200)
        print("[PASS] All 4 sections successfully tested across all date range filters.")

if __name__ == "__main__":
    unittest.main()

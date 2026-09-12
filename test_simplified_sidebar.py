"""
Verification script for simplified sidebar routes and functionality.
"""
import sys
import unittest
from app import app

class TestSimplifiedSidebar(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'admin'
            sess['full_name'] = 'Admin User'
            sess['role'] = 'ADMIN'

    def test_all_sidebar_routes(self):
        routes_to_test = [
            ('/dashboard', b'Dashboard', 'Dashboard'),
            ('/entry', b'Add Entry', 'Add Entry'),
            ('/sales-ledger', b'Sales', 'Sales'),
            ('/payables', b'Purchases', 'Purchases'),
            ('/receivables', b'Customers', 'Customers'),
            ('/expenses', b'Expenses', 'Expenses'),
            ('/assets', b'Assets', 'Assets'),
            ('/liabilities', b'Liabilities', 'Liabilities'),
            ('/assets-liabilities', b'Assets', 'Assets & Liabilities'),
            ('/equity', b'Capital &amp; Drawings', 'Capital & Drawings'),
            ('/tax', b'GST &amp; TDS', 'GST & TDS'),
            ('/ledger', b'Ledger', 'Ledger'),
            ('/reports', b'Reports', 'Reports'),
            ('/sync', b'Sync', 'Sync'),
        ]

        for path, expected_snippet, label in routes_to_test:
            with self.subTest(path=path, label=label):
                res = self.client.get(path)
                self.assertEqual(res.status_code, 200, f"Failed for {label} on {path}: status {res.status_code}")
                # Ensure the sidebar is present in the response
                self.assertIn(b'SAGAR', res.data, f"Sidebar brand missing on {path}")
                # Ensure old textbook headers are NOT in the rendered response
                self.assertNotIn(b'CORE FINANCIALS', res.data, f"Outdated header 'CORE FINANCIALS' found on {path}")
                self.assertNotIn(b'BALANCE SHEET ACCOUNTS', res.data, f"Outdated header 'BALANCE SHEET ACCOUNTS' found on {path}")
                self.assertNotIn(b'AUDIT & REPORTS', res.data, f"Outdated header 'AUDIT & REPORTS' found on {path}")
                print(f"[PASS] {label} ({path}) returned HTTP 200 with clean sidebar.")

    def test_customers_kpis(self):
        res = self.client.get('/receivables')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Total Receivable', res.data)
        self.assertIn(b'Paid', res.data)
        self.assertIn(b'Pending', res.data)
        self.assertIn(b'Overdue', res.data)
        print("[PASS] Customers page verified with 4 KPI cards (Total Receivable, Paid, Pending, Overdue).")

    def test_assets_and_liabilities_views(self):
        res_assets = self.client.get('/assets')
        self.assertEqual(res_assets.status_code, 200)
        self.assertIn(b'Fixed Assets', res_assets.data)
        self.assertIn(b'Current Assets', res_assets.data)

        res_liab = self.client.get('/liabilities')
        self.assertEqual(res_liab.status_code, 200)
        self.assertIn(b'Active Loan Obligations', res_liab.data)
        self.assertIn(b'Current Liabilities', res_liab.data)
        print("[PASS] Assets and Liabilities views verified with respective breakdowns.")

if __name__ == '__main__':
    unittest.main()

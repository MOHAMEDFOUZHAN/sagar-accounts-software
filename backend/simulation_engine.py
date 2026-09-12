import datetime
import logging

from backend.db import get_db_connection
from backend.accounts_engine import (
    purge_all_transaction_data,
    get_dashboard_summary,
    create_receivable,
    create_payable,
)
from backend.reports_engine import generate_profit_and_loss, generate_cash_flow

logger = logging.getLogger(__name__)


def run_accounting_simulation(purge_first=True):
    if purge_first:
        purge_all_transaction_data()

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    today = datetime.datetime.now()

    try:
        sales_data = [
            ("TXN-SIM-S01", (today - datetime.timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"), "Income", "Revenue", "Sales Revenue", 50000.00, "Cash", "POS-INV-101", "Counter retail cash sales billing", "sales_cashier"),
            ("TXN-SIM-S02", (today - datetime.timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S"), "Income", "Revenue", "Sales Revenue", 35000.00, "UPI", "UPI-QR-8801", "Counter QR / UPI customer payments", "sales_cashier"),
            ("TXN-SIM-S03", (today - datetime.timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"), "Income", "Revenue", "Sales Revenue", 25000.00, "Card", "POS-CARD-441", "Counter POS card swipe settlements", "sales_cashier"),
            ("TXN-SIM-E01", (today - datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"), "Expense", "Operating Expense", "Shop Expense", 250.00, "UPI", "UPI-AUTO-01", "Cashier auto transport charges for emergency stock pickup", "sales_cashier"),
            ("TXN-SIM-E02", (today - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"), "Expense", "Operating Expense", "Shop Expense", 150.00, "Cash", "PETTY-001", "Cashier packing bags and counter tea refreshments", "sales_cashier"),
        ]

        for txn_no, t_date, t_type, acc_type, cat_name, amt, p_method, ref_no, desc, user in sales_data:
            cursor.execute("SELECT id FROM accounts_categories WHERE name = %s AND type = %s;", (cat_name, acc_type))
            r = cursor.fetchone()
            cat_id = r["id"] if r else None
            cursor.execute(
                """INSERT INTO accounts_transactions
                    (txn_number, txn_date, txn_type, account_type, category_id, category_name,
                     amount, payment_method, reference_no, description, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);""",
                (txn_no, t_date, t_type, acc_type, cat_id, cat_name, amt, p_method, ref_no, desc, user),
            )
        conn.commit()

        accounts_data = [
            ("TXN-SIM-C01", (today - datetime.timedelta(days=1, hours=2)).strftime("%Y-%m-%d %H:%M:%S"), "Expense", "Direct Expense", "Raw Materials", 40000.00, "Bank Transfer", "PO-RAW-901", "Procurement of primary production raw materials", "accountant"),
            ("TXN-SIM-C02", (today - datetime.timedelta(days=1, hours=1)).strftime("%Y-%m-%d %H:%M:%S"), "Expense", "Direct Expense", "Direct Wages", 15000.00, "Cash", "WAGE-WK-3", "Factory processing staff weekly direct wages", "accountant"),
            ("TXN-SIM-O01", (today - datetime.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"), "Expense", "Operating Expense", "Shop Rent", 20000.00, "Bank Transfer", "RENT-SHOP-MAR", "Monthly commercial retail store rent", "accountant"),
            ("TXN-SIM-O02", (today - datetime.timedelta(days=2, hours=3)).strftime("%Y-%m-%d %H:%M:%S"), "Expense", "Operating Expense", "Employee Salary", 30000.00, "Bank Transfer", "PAYROLL-MAR-01", "Monthly employee salary disbursements", "accountant"),
            ("TXN-SIM-O03", (today - datetime.timedelta(days=1, hours=4)).strftime("%Y-%m-%d %H:%M:%S"), "Expense", "Operating Expense", "Office Electricity/Water", 3500.00, "UPI", "EB-BILL-7731", "Administrative office power and utility charges", "accountant"),
        ]

        for txn_no, t_date, t_type, acc_type, cat_name, amt, p_method, ref_no, desc, user in accounts_data:
            cursor.execute("SELECT id FROM accounts_categories WHERE name = %s AND type = %s;", (cat_name, acc_type))
            r = cursor.fetchone()
            cat_id = r["id"] if r else None
            cursor.execute(
                """INSERT INTO accounts_transactions
                    (txn_number, txn_date, txn_type, account_type, category_id, category_name,
                     amount, payment_method, reference_no, description, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);""",
                (txn_no, t_date, t_type, acc_type, cat_id, cat_name, amt, p_method, ref_no, desc, user),
            )
        conn.commit()

        rec_due = (datetime.date.today() + datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        create_receivable(
            {
                "customer_name": "Sri Balaji Wholesale Traders",
                "contact_phone": "9842109988",
                "invoice_ref": "INV-CR-701",
                "total_amount": 30000.00,
                "paid_amount": 10000.00,
                "due_date": rec_due,
                "notes": "Credit sale batch, Rs.10k advance received via NEFT, Rs.20k balance due",
                "payment_method": "Bank Transfer",
                "reference_no": "NEFT-ADV-883",
            },
            "sales_cashier",
        )

        pay_due = (datetime.date.today() + datetime.timedelta(days=15)).strftime("%Y-%m-%d")
        create_payable(
            {
                "supplier_name": "Nilgiri Spice Mills & Supplies",
                "contact_phone": "9488110022",
                "invoice_ref": "SUP-CR-402",
                "total_amount": 45000.00,
                "paid_amount": 15000.00,
                "due_date": pay_due,
                "notes": "Raw spice consignment, Rs.15k paid, Rs.30k remaining",
                "payment_method": "Bank Transfer",
                "reference_no": "NEFT-SUP-402",
            },
            "accountant",
        )

    finally:
        cursor.close()
        conn.close()

    summary = get_dashboard_summary()
    pnl = generate_profit_and_loss()
    cash_flow = generate_cash_flow()

    exp_revenue = 110000.00
    exp_cogs = 55000.00
    exp_gross_profit = 55000.00
    exp_opex = 53900.00
    exp_net_profit = 1100.00
    exp_receivables = 20000.00
    exp_payables = 30000.00
    exp_net_cash_flow = 1100.00

    checks = [
        {"metric": "Total Revenue (Sales Income)", "formula": "Sum of Cash (50,000) + UPI (35,000) + Card (25,000)", "expected": exp_revenue, "actual": summary["total_income"], "passed": abs(summary["total_income"] - exp_revenue) < 0.01},
        {"metric": "Total Direct Expenses (COGS)", "formula": "Sum of Raw Materials (40,000) + Factory Wages (15,000)", "expected": exp_cogs, "actual": summary["total_cogs"], "passed": abs(summary["total_cogs"] - exp_cogs) < 0.01},
        {"metric": "Gross Profit", "formula": "Total Revenue (110,000) - Total COGS (55,000)", "expected": exp_gross_profit, "actual": summary["gross_profit"], "passed": abs(summary["gross_profit"] - exp_gross_profit) < 0.01},
        {"metric": "Total Operating Expenses (OpEx)", "formula": "Rent (20,000) + Salaries (30,000) + Electricity (3,500) + Shop Auto (250) + Shop Tea (150)", "expected": exp_opex, "actual": summary["total_opex"], "passed": abs(summary["total_opex"] - exp_opex) < 0.01},
        {"metric": "Net Profit Before Tax", "formula": "Gross Profit (55,000) - Total Operating Expenses (53,900)", "expected": exp_net_profit, "actual": summary["net_profit"], "passed": abs(summary["net_profit"] - exp_net_profit) < 0.01},
        {"metric": "P&L Statement Net Profit Consistency", "formula": "P&L Report Revenue (110,000) - COGS (55,000) - OpEx (53,900)", "expected": exp_net_profit, "actual": pnl["net_profit"], "passed": abs(pnl["net_profit"] - exp_net_profit) < 0.01},
        {"metric": "Outstanding Accounts Receivable", "formula": "Total Invoiced (30,000) - Received Advance (10,000)", "expected": exp_receivables, "actual": summary["total_receivables"], "passed": abs(summary["total_receivables"] - exp_receivables) < 0.01},
        {"metric": "Outstanding Accounts Payable", "formula": "Total Supplier Consignment (45,000) - Paid Advance (15,000)", "expected": exp_payables, "actual": summary["total_payables"], "passed": abs(summary["total_payables"] - exp_payables) < 0.01},
        {"metric": "Net Cash Flow", "formula": "Total Inflows (110,000) - Total Outflows (108,900)", "expected": exp_net_cash_flow, "actual": cash_flow["net_cash_flow"], "passed": abs(cash_flow["net_cash_flow"] - exp_net_cash_flow) < 0.01},
    ]

    all_passed = all(c["passed"] for c in checks)

    return {
        "status": "success" if all_passed else "failed",
        "all_passed": all_passed,
        "summary": summary,
        "pnl": pnl,
        "cash_flow": cash_flow,
        "checks": checks,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def restore_default_demo_dataset():
    from init_db import init_database

    purge_all_transaction_data()
    init_database()
    return {"status": "success", "message": "Default demo dataset restored successfully."}

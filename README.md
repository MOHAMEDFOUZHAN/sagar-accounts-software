# Sagar Accounts Software

A standalone double-entry accounting system built with Python (Flask) + MySQL/SQLite.

## Features
- Dashboard with Revenue, COGS, Gross Profit, Operating Expenses, Net Profit, Receivables, Payables
- Income / Revenue tracking
- Direct Expenses (COGS) + Operating Expenses management
- Accounts Receivable (credit sales, installment payments)
- Accounts Payable (supplier invoices, disbursements)
- P&L Reports with Cash Flow breakdown
- Quick Entry screen
- System Test & Calculation Verification Center
- Full dummy data purge and demo data restore

## Accounting Formulas
```
TOTAL REVENUE - TOTAL COGS = GROSS PROFIT
GROSS PROFIT - OPERATING EXPENSES = NET PROFIT
```

## Default Credentials
- **Accounts:** `accounts` / `1234` (or `accounts`)
- **Accountant:** `accountant` / `1234`

## Setup

```bash
pip install -r requirements.txt
python init_db.py
python app.py
```

Server starts at: http://127.0.0.1:5001

## Database
- Uses **MySQL** by default (configure in `.env`)
- Falls back to **SQLite** automatically if MySQL is unavailable

## Environment Variables (.env)
```
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=
MYSQL_DB=accounts_db
FLASK_PORT=5001
SECRET_KEY=your_secret_key
```

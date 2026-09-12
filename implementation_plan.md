# Rebuilding the Accounts Module: Ground-Up Architecture & Integration Plan

## Executive Summary
This document outlines the complete architectural redesign and ground-up implementation plan for the **Accounts Module**. 

The goal is to provide a single source of financial truth by connecting:
1. **Existing Inventory Management System** (`C:\ProgramData\BenchmarkMMS\inventory.db` / `d:\projects\Material_Management_System`)
2. **Existing Sales Billing System** (`maple_pro_db` in MySQL on `127.0.0.1:3306` / `d:\projects\Sales\billing-software`)
3. **Manual Accounting Entries** (Fixed Assets, Liabilities, Equity, Capital/Drawings, Office Expenses, Loans, TDS, Adjustments)

The obsolete, isolated old Accounts implementation is completely superseded by a robust double-entry core with automated sync, strict idempotency to prevent duplicate entries, and exact Profit & Loss calculation:
$$\text{Gross Profit} = \text{Revenue} - \text{COGS}$$
$$\text{Net Profit} = \text{Gross Profit} - \text{Operating Expenses}$$

---

## 1. Existing Inventory Data Flow
* **Component**: Material Management System (`MMS.py`, SQLite database `C:\ProgramData\BenchmarkMMS\inventory.db`).
* **Entities & Tables**:
  * `materials`: Material code, description, category, quantity in stock, unit price (cost price), opening stock, lot number, last updated.
  * `vendors`: Vendor/supplier name, contact, place, GSTIN, material details.
  * `invoices`: Supplier purchase invoices (`invoice_no`, `vendor`, `date`, `total_excl_tax`, `total_gst`, `cgst_percent`, `sgst_percent`, `grand_total`, `payment_status` ['Pending', 'Paid'], `remarks`).
  * `invoice_items`: Line-level purchase entries (`material`, `quantity`, `unit`, `unit_price`, `discount_percentage`, `gst_percentage`, `item_subtotal`, `item_gst_value`, `item_total`, `batch_no`).
  * `dispatches` & `transfers`: Production consumption and department material movements.
* **Flow**:
  * Suppliers send raw materials $\rightarrow$ Inward invoice recorded in `invoices` + `invoice_items`.
  * Increases stock in `materials`, stores purchase unit cost $\rightarrow$ Current inventory valuation $= \sum (\text{quantity} \times \text{unit\_price})$.
  * Unpaid purchase invoices (`payment_status = 'Pending'`) represent accounts payable to suppliers.

---

## 2. Existing Sales Data Flow
* **Component**: Sales Billing System (`d:\projects\Sales\billing-software`, MySQL database `maple_pro_db`).
* **Entities & Tables**:
  * `bills`: Sales invoices (`invoice_no`, `bill_date`, `total_amount`, `payment_mode` ['CASH', 'UPI', 'CARD', etc.], `status` ['PAID', 'CORRECTION', 'CANCELLED', 'VOID'], `discount`, `tsc_percent`, `tsc_amount`, `balance`, `created_by`).
  * `bill_items`: Sold items (`product_code`, `product_name`, `qty`, `rate`, `amount`, `bizz_percent`, `bizz_amount`).
  * `products`: Finished retail merchandise (`barcode`, `name`, `category`, `price`, `current_stock`, `unit`).
  * `stock_movements`: Deductions in finished goods stock upon sale.
  * `returns_log`: Sales returns and customer refunds (`bill_id`, `product_name`, `qty`, `amount`, `reason`, `returned_at`).
  * `expenses`: Counter operational expenses (`category`, `amount`, `expense_date`, `description`, `expense_group` ['SHOP', 'OFFICE']).
  * `cash_balance` & `denominations`: Daily counter cash tallies and closing balances.
* **Flow**:
  * POS Counter completes a sale $\rightarrow$ Inserts record in `bills` + `bill_items` with payment mode (Cash/UPI/etc.).
  * Sales return recorded $\rightarrow$ Logged in `returns_log`.
  * Counter logs daily shop expenses (e.g. flower, refreshments, beta) $\rightarrow$ Inserted into `expenses` with `expense_group = 'SHOP'`.

---

## 3. Existing Database Models Relevant to Accounts

| System | Database | Table Name | Relevant Financial Columns | Role in Accounts |
| :--- | :--- | :--- | :--- | :--- |
| **Sales** | `maple_pro_db` (MySQL) | `bills` | `id`, `invoice_no`, `bill_date`, `total_amount`, `payment_mode`, `status`, `discount`, `balance` | Revenue recognition, Cash/Bank/UPI inflow, Receivables |
| **Sales** | `maple_pro_db` (MySQL) | `bill_items` | `bill_id`, `product_code`, `product_name`, `qty`, `rate`, `amount` | Revenue line breakdown, COGS volume calculation |
| **Sales** | `maple_pro_db` (MySQL) | `returns_log` | `bill_id`, `product_name`, `qty`, `amount`, `returned_at` | Sales returns / revenue deduction / refund tracking |
| **Sales** | `maple_pro_db` (MySQL) | `expenses` | `id`, `category`, `amount`, `expense_date`, `expense_group`, `description` | Counter Shop Expenses $\rightarrow$ Operating Expenses |
| **Sales** | `maple_pro_db` (MySQL) | `products` | `id`, `barcode`, `name`, `category`, `price`, `current_stock` | Finished goods stock levels |
| **Sales** | `maple_pro_db` (MySQL) | `users` | `id`, `username`, `password_hash`, `role` | Shared authentication and role authorization |
| **Inventory** | `inventory.db` (SQLite) | `invoices` | `id`, `invoice_no`, `vendor`, `date`, `total_excl_tax`, `total_gst`, `cgst_percent`, `sgst_percent`, `grand_total`, `payment_status` | Purchase recognition, Input GST, Supplier Payables |
| **Inventory** | `inventory.db` (SQLite) | `invoice_items`| `invoice_id`, `material`, `quantity`, `unit_price`, `gst_percentage`, `item_subtotal`, `item_gst_value` | Raw materials procurement, Direct expenses breakdown |
| **Inventory** | `inventory.db` (SQLite) | `materials` | `id`, `material_code`, `description`, `category`, `quantity`, `unit_price` | Current Asset Inventory Valuation, Raw material cost base |
| **Inventory** | `inventory.db` (SQLite) | `vendors` | `id`, `name`, `contact`, `place`, `gstin` | Supplier directory & GST reconciliation |

---

## 4. Fields Fetched Automatically

1. **Sales Revenue**: `bills.total_amount` (less discounts) automatically booked under Operating Revenue.
2. **Customer Cash/UPI/Bank Collections**: Inflows into Cash or Bank asset accounts derived from `bills.payment_mode`.
3. **Accounts Receivable**: Unpaid or partial credit bills (`bills.balance > 0` or `status != 'PAID'`).
4. **Sales Returns / Refunds**: `returns_log.amount` reducing revenue and cash/bank balance.
5. **Output GST Liability**: Extracted from taxable bills / tax percentage config.
6. **Shop Expenses**: `expenses` where `expense_group = 'SHOP'` automatically pulled into Operating Expenses under Shop Expense.
7. **Purchases & Direct Material Costs**: `invoices.grand_total` and `total_excl_tax` from `inventory.db`.
8. **Input GST Credit**: `invoices.total_gst` (CGST + SGST) from `inventory.db`.
9. **Accounts Payable**: `invoices` where `payment_status = 'Pending'` categorized under Current Liabilities.
10. **Stock / Inventory Asset Value**: Current raw material asset value $= \sum(\text{materials.quantity} \times \text{materials.unit\_price})$.

---

## 5. Fields Requiring Manual Entry

1. **Fixed Assets**: Land, Building, Plant & Machinery, Equipment, Vehicles (Purchase date, Cost, Current valuation, Useful life, Depreciation rate).
2. **Owner Equity**: Capital contributions, Owner drawings (Personal withdrawals).
3. **Liabilities & Borrowings**: Bank commercial loans, Mortgages, Outstanding payroll/salary adjustments.
4. **Non-Shop Operating Expenses**: Office electricity, Office rent, Marketing/Advertisements, Courier/Postage, Stationery, Audit fees, Legal fees, Depreciation expenses, Miscellaneous office overhead.
5. **Tax & Statutory Deductions**: TDS payable, TDS adjustments, GST payments/challans.
6. **Financial Costs**: Bank ledger charges, Loan interest payments, POS swipe transaction fees.
7. **Other Income**: Fixed deposit interest, Non-operational scrap recovery.

---

## 6. Accounting Data Flow & Journal Architecture

```
                  ┌────────────────────────────────────────┐
                  │          DATA SOURCES (LIVE)           │
                  └────────────────────────────────────────┘
                      │                                │
                      ▼                                ▼
       ┌────────────────────────────┐    ┌────────────────────────────┐
       │   Sales (maple_pro_db)     │    │   Inventory (inventory.db) │
       │  * Bills (Cash/UPI/Card)   │    │  * Purchase Invoices       │
       │  * Shop Expenses           │    │  * Vendor Payables         │
       │  * Returns & Refunds       │    │  * Input GST & Stock Cost  │
       └──────────────┬─────────────┘    └─────────────┬──────────────┘
                      │                                │
                      └────────────────┬───────────────┘
                                       ▼
                  ┌────────────────────────────────────────┐
                  │    ACCOUNTS SYNC & DEDUPLICATION       │
                  │  Checks `accounting_sync_registry`     │
                  │  Idempotent / Prevents Duplicate Posts │
                  └────────────────────┬───────────────────┘
                                       │
                      ┌────────────────┴───────────────┐
                      │                                │
                      ▼                                ▼
       ┌────────────────────────────┐    ┌────────────────────────────┐
       │   AUTOMATED JOURNAL POSTS  │    │  MANUAL ACCOUNTING ENTRIES │
       │  * Dr Cash/Bank Cr Revenue │    │  * Fixed Assets / Depr.    │
       │  * Dr COGS Cr Inventory    │    │  * Capital & Drawings      │
       │  * Dr Purchases Cr Payable │    │  * Bank Loans & Liabilities│
       │  * Dr Shop Exp Cr Cash/UPI │    │  * Office Rent / Utilities │
       └──────────────┬─────────────┘    └─────────────┬──────────────┘
                      │                                │
                      └────────────────┬───────────────┘
                                       ▼
                  ┌────────────────────────────────────────┐
                  │   DOUBLE-ENTRY JOURNAL & LEDGER CORE   │
                  │   `journal_entries` + `journal_lines`  │
                  └────────────────────┬───────────────────┘
                                       ▼
                  ┌────────────────────────────────────────┐
                  │    DYNAMIC STATEMENTS & DASHBOARD      │
                  │  1. Executive Dashboard (KPIs, Cash)   │
                  │  2. Profit & Loss (Gross & Net Profit) │
                  │  3. Dynamic Balance Sheet              │
                  │  4. Accounts Receivable & Payable      │
                  │  5. Net GST Payable (Output - Input)   │
                  │  6. Account Ledgers & Trial Balance    │
                  └────────────────────────────────────────┘
```

---

## 7. Database Changes Required

In MySQL (`accounts_db` or shared instance alongside `maple_pro_db`):

```sql
-- 1. Master Chart of Accounts
CREATE TABLE IF NOT EXISTS accounts_chart (
    id INT AUTO_INCREMENT PRIMARY KEY,
    code VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(100) NOT NULL,
    major_type ENUM('Asset', 'Liability', 'Equity', 'Revenue', 'Direct Expense', 'Operating Expense', 'Tax', 'Financial Cost') NOT NULL,
    sub_type VARCHAR(100) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Master Journal Entries
CREATE TABLE IF NOT EXISTS journal_entries (
    id INT AUTO_INCREMENT PRIMARY KEY,
    entry_number VARCHAR(50) UNIQUE NOT NULL,
    entry_date DATETIME NOT NULL,
    source_module ENUM('sales', 'inventory', 'manual') NOT NULL,
    source_entity VARCHAR(50), -- e.g. 'bill', 'return', 'purchase_invoice', 'expense', 'manual_voucher'
    source_id VARCHAR(100),    -- Primary Key / Invoice No from source
    narration TEXT NOT NULL,
    status ENUM('POSTED', 'VOID', 'REVERSED') DEFAULT 'POSTED',
    created_by VARCHAR(50) DEFAULT 'system',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX (entry_date),
    INDEX (source_module, source_id)
);

-- 3. Journal Lines (Double Entry)
CREATE TABLE IF NOT EXISTS journal_lines (
    id INT AUTO_INCREMENT PRIMARY KEY,
    entry_id INT NOT NULL,
    account_id INT NOT NULL,
    debit DECIMAL(15, 2) DEFAULT 0.00,
    credit DECIMAL(15, 2) DEFAULT 0.00,
    INDEX (entry_id),
    INDEX (account_id),
    FOREIGN KEY (entry_id) REFERENCES journal_entries(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts_chart(id)
);

-- 4. Sync Registry (Idempotency & Deduplication Engine)
CREATE TABLE IF NOT EXISTS accounting_sync_registry (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source_module ENUM('sales', 'inventory') NOT NULL,
    source_entity VARCHAR(50) NOT NULL,
    source_id VARCHAR(100) NOT NULL,
    source_hash VARCHAR(64) NOT NULL,
    journal_entry_id INT,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    status VARCHAR(20) DEFAULT 'SYNCED',
    UNIQUE KEY unique_source_record (source_module, source_entity, source_id),
    FOREIGN KEY (journal_entry_id) REFERENCES journal_entries(id) ON DELETE SET NULL
);

-- 5. Fixed Assets Master
CREATE TABLE IF NOT EXISTS accounts_fixed_assets (
    id INT AUTO_INCREMENT PRIMARY KEY,
    asset_code VARCHAR(30) UNIQUE NOT NULL,
    asset_name VARCHAR(150) NOT NULL,
    category ENUM('Land', 'Building', 'Machinery', 'Equipment', 'Vehicles', 'Other') NOT NULL,
    purchase_date DATE NOT NULL,
    purchase_value DECIMAL(15, 2) NOT NULL,
    current_value DECIMAL(15, 2) NOT NULL,
    useful_life_years INT DEFAULT 5,
    depreciation_rate DECIMAL(5, 2) DEFAULT 10.00,
    accumulated_depreciation DECIMAL(15, 2) DEFAULT 0.00,
    payment_method VARCHAR(50),
    supplier VARCHAR(100),
    status ENUM('Active', 'Disposed', 'Written_Off') DEFAULT 'Active',
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 6. Liabilities & Loans Master
CREATE TABLE IF NOT EXISTS accounts_liabilities (
    id INT AUTO_INCREMENT PRIMARY KEY,
    liability_code VARCHAR(30) UNIQUE NOT NULL,
    title VARCHAR(150) NOT NULL,
    liability_type ENUM('Bank Loan', 'Personal Loan', 'Salary Payable', 'TDS Payable', 'GST Payable', 'Other') NOT NULL,
    principal_amount DECIMAL(15, 2) NOT NULL,
    interest_rate DECIMAL(5, 2) DEFAULT 0.00,
    tenure_months INT DEFAULT 0,
    outstanding_balance DECIMAL(15, 2) NOT NULL,
    lender VARCHAR(150),
    start_date DATE NOT NULL,
    status ENUM('Active', 'Closed') DEFAULT 'Active',
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 8. API Changes Required

1. **Synchronization Service (`backend/sync_service.py`)**:
   - `POST /api/sync/run`: Scans `maple_pro_db` (`bills`, `expenses`, `returns_log`) and `inventory.db` (`invoices`, `materials`), checks `accounting_sync_registry`, and posts/updates journals without duplicates.
   - `GET /api/sync/status`: Returns last sync time, record counts, and health status.
2. **Dashboard & Summary API**:
   - `GET /api/dashboard/metrics?range=today|week|month|quarter|year|custom`: Dynamic financial indicators (Revenue, COGS, Gross Profit, Operating Expenses, Net Profit, Cash, Bank, Inventory, Receivables, Payables, GST/TDS liabilities).
3. **Manual Entry API**:
   - `POST /api/entry/save`: Validates Description, Amount (>0), Payment Type (dropdown: Cash, Bank, UPI, Card, Cheque, Other), Account Type (dropdown from Chart of Accounts), Reference, Notes. Auto-records current timestamp and user.
   - `GET /api/entry/categories`: Returns hierarchical account types.
4. **Receivables & Payables APIs**:
   - `GET /api/receivables`: Fetches credit invoices from Sales, amounts paid, remaining balance, aging.
   - `POST /api/receivables/payment`: Records customer payment against receivable, updates cash/bank and journal.
   - `GET /api/payables`: Fetches supplier purchase invoices from Inventory, status, amounts paid, outstanding balance.
   - `POST /api/payables/payment`: Records payment made to vendor, updates cash/bank and journal.
5. **Assets, Liabilities & Equity APIs**:
   - `GET /api/assets`, `POST /api/assets/create`, `POST /api/assets/depreciate`
   - `GET /api/liabilities`, `POST /api/liabilities/create`
   - `GET /api/equity`, `POST /api/equity/create` (Capital and Drawings)
6. **Financial Statements & Reports APIs**:
   - `GET /api/reports/pnl`: Profit & Loss (Revenue $\rightarrow$ Less COGS $\rightarrow$ Gross Profit $\rightarrow$ Less Operating Expenses $\rightarrow$ Net Profit).
   - `GET /api/reports/balance-sheet`: Dynamic Balance Sheet ($Assets = Liabilities + Equity$).
   - `GET /api/reports/ledger?account_id=X&from=Y&to=Z`: Detailed ledger statement with running balance.
   - `GET /api/reports/gst`: Input GST vs Output GST breakdown and Net GST Payable.
   - `GET /api/reports/trial-balance`: Debits vs Credits reconciliation.

---

## 9. Screens and Pages Created

1. **Dashboard** (`/dashboard`): Modern, executive-level financial cockpit with top KPI summary cards, cash & bank balances, Gross vs Net profit cards, interactive visual charts, and recent multi-source transactions.
2. **Accounting Entry** (`/entry`): Clean, keyboard-friendly manual entry screen with validated dropdowns for Account Type and Payment Type, automatic timestamping, and instant preview.
3. **Sales Ledger & Invoices** (`/sales-ledger`): Real-time read from Sales module showing bill number, customer, amount, payment mode, GST output, and sync status.
4. **Purchases & Payables** (`/payables`): Automated feed from Inventory purchase invoices, showing vendor name, purchase date, total amount, paid, outstanding, and pay action.
5. **Customer Receivables** (`/receivables`): Customer credit tracking, balance outstanding, status (Paid, Partial, Overdue), and collection recording.
6. **Expenses Center** (`/expenses`): Clearly segregated view: Direct Expenses (COGS) vs Operating Expenses, incorporating counter Shop Expenses from Sales and administrative expenses from Accounts.
7. **Assets & Liabilities** (`/assets-liabilities`): Tracking Fixed Assets (Machinery, Equipment, Vehicles) with depreciation, and Long-Term Liabilities (Loans, Borrowings).
8. **Equity (Capital & Drawings)** (`/equity`): Owner investments and drawings with date, payment method, and running equity balance.
9. **Tax Center (GST & TDS)** (`/tax`): Input GST, Output GST, CGST, SGST, IGST breakdown, Net GST Payable, and TDS liability.
10. **General Ledger** (`/ledger`): Account selection dropdown, date range filter, running debit/credit balance, and clickable source audit link.
11. **Financial Reports** (`/reports`): Tabbed report suite: Profit & Loss Statement, Balance Sheet, Cash Flow Summary, Trial Balance, COGS Report, and Tax Summary with Print/Export.

---

## 10. How Duplicate Accounting Entries Will Be Prevented

1. **Unique Idempotency Keys**:
   Every transaction processed from Sales or Inventory is registered in `accounting_sync_registry` with a composite unique constraint:
   $$\text{UNIQUE}(\text{source\_module}, \text{source\_entity}, \text{source\_id})$$
   * Example: `('sales', 'bill', 'SS-1')` or `('inventory', 'purchase_invoice', 'INV-2026-001')`.
2. **Deterministic Payload Hashing**:
   A SHA-256 hash of `(amount, payment_mode, status, date)` is stored. 
   - If the source record already exists and the hash matches $\rightarrow$ **Skipped completely** (zero duplicate entries).
   - If the source record was edited in Sales/Inventory $\rightarrow$ Existing journal entry is **updated in-place** rather than creating a second entry.
   - If the source record is cancelled/voided $\rightarrow$ Reversing entry is automatically posted.
3. **Pre-Commit Validation**:
   The Sync Engine wraps each operation in a transactional unit; any duplicate attempt triggers an immediate deduplication bypass.

---

## Acceptance Test Scenario Verification
The rebuilt Accounts module will be validated against the exact acceptance scenario:
* **Inventory**: Raw material purchase = ₹3,000 $\rightarrow$ Accounts recognizes Raw Material Purchase / AP = ₹3,000.
* **Sales**: Sale = ₹5,000, GST = ₹900, Customer paid ₹5,900 via UPI $\rightarrow$ Accounts recognizes:
  * Revenue = ₹5,000
  * Output GST = ₹900
  * UPI Inflow = ₹5,900
  * Customer Receivable = ₹0 (Fully paid)
  * Inventory reduction & COGS based on unit cost
* **Manual Entries**: Bank charge = ₹150, Shop rent = ₹2,000.
* **Calculations Verified**:
  * $\text{Gross Profit} = \text{Revenue} (5,000) - \text{COGS}$
  * $\text{Operating Expenses} = 150 + 2,000 = 2,150$
  * $\text{Net Profit} = \text{Gross Profit} - 2,150$
  * Re-syncing verifies that the sale is **never duplicated**.

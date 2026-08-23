-- Standalone Accounts Management Database Schema
-- Database: accounts_db

CREATE DATABASE IF NOT EXISTS accounts_db;
USE accounts_db;

-- 1. Accounts Users Table
CREATE TABLE IF NOT EXISTS accounts_users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100),
    role ENUM('admin', 'accountant') DEFAULT 'accountant',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Categories Table (Chart of Accounts)
CREATE TABLE IF NOT EXISTS accounts_categories (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    type ENUM('Revenue', 'Direct Expense', 'Operating Expense', 'Asset', 'Liability', 'Equity') NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY idx_type_name (type, name)
);

-- 3. Central Financial Transactions Table
CREATE TABLE IF NOT EXISTS accounts_transactions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    txn_number VARCHAR(30) UNIQUE NOT NULL,
    txn_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    txn_type ENUM('Income', 'Expense', 'Asset_Purchase', 'Liability_Payment', 'Equity_Transfer') NOT NULL,
    account_type ENUM('Revenue', 'Direct Expense', 'Operating Expense', 'Asset', 'Liability', 'Equity') NOT NULL,
    category_id INT,
    category_name VARCHAR(100) NOT NULL,
    amount DECIMAL(12, 2) NOT NULL,
    payment_method ENUM('Cash', 'Bank Transfer', 'UPI', 'Card', 'Cheque', 'Other') DEFAULT 'Cash',
    reference_no VARCHAR(100),
    description TEXT,
    created_by VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX (txn_date),
    INDEX (txn_type),
    INDEX (account_type),
    INDEX (category_name),
    FOREIGN KEY (category_id) REFERENCES accounts_categories(id) ON DELETE SET NULL
);

-- 4. Accounts Receivable (Money business needs to receive)
CREATE TABLE IF NOT EXISTS accounts_receivables (
    id INT AUTO_INCREMENT PRIMARY KEY,
    receivable_no VARCHAR(30) UNIQUE NOT NULL,
    customer_name VARCHAR(100) NOT NULL,
    contact_phone VARCHAR(20),
    invoice_ref VARCHAR(100),
    total_amount DECIMAL(12, 2) NOT NULL,
    paid_amount DECIMAL(12, 2) DEFAULT 0.00,
    remaining_balance DECIMAL(12, 2) NOT NULL,
    due_date DATE NOT NULL,
    status ENUM('Pending', 'Partial', 'Paid') DEFAULT 'Pending',
    notes TEXT,
    created_by VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX (status),
    INDEX (due_date),
    INDEX (customer_name)
);

-- 5. Accounts Receivable Payment Log
CREATE TABLE IF NOT EXISTS accounts_receivable_payments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    receivable_id INT NOT NULL,
    payment_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    amount_paid DECIMAL(12, 2) NOT NULL,
    payment_method ENUM('Cash', 'Bank Transfer', 'UPI', 'Card', 'Cheque', 'Other') DEFAULT 'Cash',
    reference_no VARCHAR(100),
    notes TEXT,
    created_by VARCHAR(50) DEFAULT 'admin',
    FOREIGN KEY (receivable_id) REFERENCES accounts_receivables(id) ON DELETE CASCADE
);

-- 6. Accounts Payable (Money business needs to pay suppliers/vendors)
CREATE TABLE IF NOT EXISTS accounts_payables (
    id INT AUTO_INCREMENT PRIMARY KEY,
    payable_no VARCHAR(30) UNIQUE NOT NULL,
    supplier_name VARCHAR(100) NOT NULL,
    contact_phone VARCHAR(20),
    invoice_ref VARCHAR(100),
    total_amount DECIMAL(12, 2) NOT NULL,
    paid_amount DECIMAL(12, 2) DEFAULT 0.00,
    remaining_balance DECIMAL(12, 2) NOT NULL,
    due_date DATE NOT NULL,
    status ENUM('Pending', 'Partial', 'Paid') DEFAULT 'Pending',
    notes TEXT,
    created_by VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX (status),
    INDEX (due_date),
    INDEX (supplier_name)
);

-- 7. Accounts Payable Payment Log
CREATE TABLE IF NOT EXISTS accounts_payable_payments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    payable_id INT NOT NULL,
    payment_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    amount_paid DECIMAL(12, 2) NOT NULL,
    payment_method ENUM('Cash', 'Bank Transfer', 'UPI', 'Card', 'Cheque', 'Other') DEFAULT 'Cash',
    reference_no VARCHAR(100),
    notes TEXT,
    created_by VARCHAR(50) DEFAULT 'admin',
    FOREIGN KEY (payable_id) REFERENCES accounts_payables(id) ON DELETE CASCADE
);

-- 8. Default Seed User (Password: admin123)
INSERT IGNORE INTO accounts_users (username, password_hash, full_name, role)
VALUES ('admin', 'scrypt:32768:8:1$K7pX0vN3qYw1k8L2$e54f9a0c4961e6fa45d614efc689d023472091523ad637996c56114b308e2d431d165fb37d6e4cb79b6aa34b1a134a66a1255e2d6a599369f69ad9ebaa2377a0', 'Accounts Manager', 'admin');

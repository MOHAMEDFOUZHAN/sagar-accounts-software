import datetime
import logging
from decimal import Decimal
from backend.db import get_db_connection, generate_unique_number
from backend.coa_engine import ChartOfAccountsEngine
from backend.double_entry_engine import DoubleEntryEngine, DoubleEntryError
from backend.period_engine import PeriodControlEngine, PeriodClosedError, PeriodLockedError

logger = logging.getLogger(__name__)


class MissingAccountMappingError(DoubleEntryError):
    """Raised when an accounting rule requires an account mapping that is not configured."""
    def __init__(self, mapping_key, message=None):
        self.mapping_key = mapping_key
        msg = message or f"Missing required account mapping for key: '{mapping_key}'. Please configure this account mapping in Chart of Accounts."
        super().__init__(msg)


class AccountingRules:
    """
    Central Accounting Rules & Account Mapping Service.
    Determines WHAT accounting debits and credits should occur for each business transaction,
    resolves mapped COA accounts, and invokes DoubleEntryEngine to atomically post balanced journals.
    """

    @classmethod
    def get_mapped_account(cls, mapping_key, default_code=None, conn=None):
        """
        Resolves a business mapping key (e.g. 'sales_revenue', 'output_gst', 'cogs')
        to a verified active, postable account record.
        """
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT am.account_id, am.account_code, ac.id, ac.code, ac.name,
                       ac.major_type, ac.sub_type, ac.normal_balance, ac.is_active,
                       ac.is_group, ac.is_postable
                FROM accounts_mappings am
                JOIN accounts_chart ac ON am.account_id = ac.id
                WHERE am.mapping_key = %s;
            """, (mapping_key,))
            row = cur.fetchone()

            if row and row["is_active"] == 1 and row["is_group"] == 0 and row["is_postable"] == 1:
                return row

            # Fallback to default_code
            if default_code:
                acc = ChartOfAccountsEngine.get_account_by_code(default_code, conn=conn)
                if acc and acc["is_active"] == 1 and acc["is_group"] == 0 and acc["is_postable"] == 1:
                    return acc

            # Fallback to system_tag matching
            cur.execute("""
                SELECT id, code, name, major_type, sub_type, normal_balance, is_active, is_group, is_postable
                FROM accounts_chart
                WHERE system_tag = %s AND is_active = 1 AND is_group = 0;
            """, (mapping_key,))
            tag_row = cur.fetchone()
            if tag_row:
                return tag_row

            raise MissingAccountMappingError(
                mapping_key,
                f"Missing account mapping for '{mapping_key}' (default code '{default_code}' not found or non-postable)."
            )
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def resolve_liquid_account(cls, payment_method, conn=None):
        """
        Resolves the appropriate Cash, Bank, or UPI account based on payment method string.
        """
        m = str(payment_method or "CASH").upper().strip()
        if any(k in m for k in ("UPI", "GPAY", "PHONEPE", "PAYTM", "QR", "DIGITAL")):
            return cls.get_mapped_account("upi_default", default_code="1030", conn=conn)
        elif any(k in m for k in ("BANK", "NEFT", "RTGS", "CHEQUE", "CHECK", "CARD", "TRANSFER", "IMPS")):
            return cls.get_mapped_account("bank_default", default_code="1020", conn=conn)
        else:
            return cls.get_mapped_account("cash_default", default_code="1010", conn=conn)

    # -------------------------------------------------------------------------
    # 1. SALES ACCOUNTING
    # -------------------------------------------------------------------------
    @classmethod
    def post_sales_bill(cls, bill, cogs_amount=0.0, conn=None):
        """
        Posts double-entry journal for a finalized sales bill from JAI Agency sales_log.
        Credit Sale:
          Dr Liquid Account (if partial upfront payment)
          Dr Accounts Receivable (remaining balance)
          Cr Sales Revenue (base amount)
          Cr Output GST Payable (tax components)
        Cash/Bank/UPI Sale:
          Dr Liquid Account (full total)
          Cr Sales Revenue (base amount)
          Cr Output GST Payable (tax components)

        COGS & Inventory Movement:
          Dr Cost of Goods Sold (5010) [Actual cost of units sold]
          Cr Inventory Stock (1050) [Actual cost of units sold]
        """
        b_id = str(bill["id"])
        b_total = round(float(bill.get("total") or 0.0), 2)
        gross_total = round(float(bill.get("gross_total") or 0.0), 2)
        b_mode = str(bill.get("payment_method") or "CASH").upper()
        b_status = str(bill.get("status") or "ACTIVE").upper()
        balance = round(float(bill.get("balance") or 0.0), 2)
        amount_paid = round(float(bill.get("amount_paid") or 0.0), 2)
        cust_name = str(bill.get("customer_name") or "General Customer").strip()

        # Skip cancelled, void, or zero-total bills
        if b_total <= 0 or b_status in ("CANCELLED", "VOID"):
            return None

        # Resolve Accounts
        liquid_acc = cls.resolve_liquid_account(b_mode, conn=conn)
        ar_acc = cls.get_mapped_account("accounts_receivable", default_code="1040", conn=conn)
        sales_rev_acc = cls.get_mapped_account("sales_revenue", default_code="4010", conn=conn)
        gst_out_acc = cls.get_mapped_account("output_gst", default_code="2030", conn=conn)

        # Tax calculation
        if gross_total > 0 and b_total > gross_total:
            tax_amount = round(b_total - gross_total, 2)
            base_rev = round(gross_total, 2)
        else:
            tax_amount = 0.0
            base_rev = b_total

        lines = []

        # Debits (Revenue Side)
        is_credit = (b_mode == "CREDIT" or balance > 0)
        if is_credit:
            paid_part = amount_paid if amount_paid > 0 else (b_total - balance if balance < b_total else 0.0)
            bal_part = balance if balance > 0 else (b_total - paid_part)

            if paid_part > 0:
                lines.append({
                    "account_id": liquid_acc["id"],
                    "debit": paid_part,
                    "credit": 0.0,
                    "description": f"Upfront payment for Sale #{b_id} via {b_mode}",
                    "party_type": "customer",
                    "party_name": cust_name,
                })
            if bal_part > 0:
                lines.append({
                    "account_id": ar_acc["id"],
                    "debit": bal_part,
                    "credit": 0.0,
                    "description": f"Credit balance due for Sale #{b_id}",
                    "party_type": "customer",
                    "party_name": cust_name,
                })
            elif paid_part <= 0:
                lines.append({
                    "account_id": ar_acc["id"],
                    "debit": b_total,
                    "credit": 0.0,
                    "description": f"Full credit for Sale #{b_id}",
                    "party_type": "customer",
                    "party_name": cust_name,
                })
        else:
            lines.append({
                "account_id": liquid_acc["id"],
                "debit": b_total,
                "credit": 0.0,
                "description": f"Full payment for Sale #{b_id} via {b_mode}",
                "party_type": "customer",
                "party_name": cust_name,
            })

        # Credits (Revenue Side)
        if tax_amount > 0 and tax_amount < b_total:
            lines.append({
                "account_id": sales_rev_acc["id"],
                "debit": 0.0,
                "credit": base_rev,
                "description": f"Operating Sales Revenue #{b_id}",
                "party_type": "customer",
                "party_name": cust_name,
            })
            lines.append({
                "account_id": gst_out_acc["id"],
                "debit": 0.0,
                "credit": tax_amount,
                "description": f"GST Output on Sale #{b_id}",
                "party_type": "customer",
                "party_name": cust_name,
                "tax_code": "GST_OUTPUT",
            })
        else:
            lines.append({
                "account_id": sales_rev_acc["id"],
                "debit": 0.0,
                "credit": b_total,
                "description": f"Operating Sales Revenue #{b_id}",
                "party_type": "customer",
                "party_name": cust_name,
            })

        # COGS & Inventory Movement (Dual-Posting)
        cogs_amt = round(float(cogs_amount or bill.get("cogs_amount") or 0.0), 2)
        if cogs_amt > 0:
            cogs_acc = cls.get_mapped_account("cogs", default_code="5010", conn=conn)
            inv_acc = cls.get_mapped_account("inventory_raw", default_code="1050", conn=conn)
            lines.append({
                "account_id": cogs_acc["id"],
                "debit": cogs_amt,
                "credit": 0.0,
                "description": f"Cost of Goods Sold for Sale #{b_id}",
                "party_type": "customer",
                "party_name": cust_name,
            })
            lines.append({
                "account_id": inv_acc["id"],
                "debit": 0.0,
                "credit": cogs_amt,
                "description": f"Inventory stock relief for Sale #{b_id}",
                "party_type": "customer",
                "party_name": cust_name,
            })

        entry_number = f"JV-SLS-{b_id.zfill(6)}"
        narration = f"Jai Agency Sale #{b_id} ({cust_name}) via {b_mode} [{b_status}]"

        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": entry_number,
                "entry_date": bill.get("date"),
                "source_module": "sales",
                "source_entity": "bill",
                "source_id": b_id,
                "reference_no": f"INV-{b_id}",
                "narration": narration,
                "status": "POSTED",
            },
            lines_data=lines,
            user="jai_agency",
            external_conn=conn,
        )

    # -------------------------------------------------------------------------
    # 2. SALES RETURNS
    # -------------------------------------------------------------------------
    @classmethod
    def post_sales_return(cls, ret, inventory_cost=0.0, conn=None):
        """
        Sales return:
          Revenue/Refund reversal:
            Dr Sales Returns (4015)
            Cr Cash / Bank / Customer AR
          Inventory & COGS Restoration:
            Dr Inventory Stock (1050) [restored stock at cost]
            Cr Cost of Goods Sold (5010) [COGS reversal]
        """
        r_id = str(ret["id"])
        refund_amount = round(float(ret.get("refund_amount") or 0.0), 2)
        if refund_amount <= 0:
            return None

        p_name = ret.get("product_name") or ret.get("product_code") or "Item"
        sales_ret_acc = cls.get_mapped_account("sales_returns", default_code="4015", conn=conn)
        cash_acc = cls.get_mapped_account("cash_default", default_code="1010", conn=conn)

        lines = [
            {
                "account_id": sales_ret_acc["id"],
                "debit": refund_amount,
                "credit": 0.0,
                "description": f"Customer Return #{r_id}: {p_name} (Qty: {ret.get('qty', 1)})",
            },
            {
                "account_id": cash_acc["id"],
                "debit": 0.0,
                "credit": refund_amount,
                "description": f"Cash refund for Return #{r_id}",
            }
        ]

        # Inventory restoration & COGS reversal
        inv_cost = round(float(inventory_cost or ret.get("inventory_cost") or 0.0), 2)
        if inv_cost > 0:
            inv_acc = cls.get_mapped_account("inventory_raw", default_code="1050", conn=conn)
            cogs_acc = cls.get_mapped_account("cogs", default_code="5010", conn=conn)
            lines.append({
                "account_id": inv_acc["id"],
                "debit": inv_cost,
                "credit": 0.0,
                "description": f"Inventory restoration for Return #{r_id}: {p_name}",
            })
            lines.append({
                "account_id": cogs_acc["id"],
                "debit": 0.0,
                "credit": inv_cost,
                "description": f"COGS reversal for Return #{r_id}: {p_name}",
            })

        entry_number = f"JV-RTN-{r_id.zfill(6)}"
        narration = f"Jai Agency Sales Return #{r_id} for Bill #{ret.get('bill_id')} - {p_name}"

        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": entry_number,
                "entry_date": ret.get("date"),
                "source_module": "sales",
                "source_entity": "return",
                "source_id": r_id,
                "reference_no": f"RET-{r_id}",
                "narration": narration,
                "status": "POSTED",
            },
            lines_data=lines,
            user="jai_agency",
            external_conn=conn,
        )

    # -------------------------------------------------------------------------
    # 3. INVENTORY PURCHASES
    # -------------------------------------------------------------------------
    @classmethod
    def post_inventory_purchase(cls, item, conn=None):
        """
        Inward inventory purchase invoice:
          Dr Inventory Stock (1050) [cost amount]
          Dr GST Input Credit (2040) [tax amount, if any]
          Cr Accounts Payable (2010) (if credit/pending) OR Liquid Account (if paid)
        Purchasing goods increases Inventory Stock asset. COGS is recognized only upon sale.
        """
        import hashlib
        inv_no = str(item.get("invoice_no") or "").strip()
        arr_date_str = str(item.get("arrival_date") or item.get("date") or item.get("entry_time") or datetime.date.today().isoformat())
        st_id = str(item.get("unique_src_id") or item.get("id") or f"{inv_no}@{arr_date_str}")

        # Calculate cost and gst
        cost_amt = round(float(item.get("total_cost") or item.get("cost_total") or 0.0), 2)
        gst_amt = round(float(item.get("total_gst") or item.get("gst_amount") or item.get("gst_value") or 0.0), 2)
        if cost_amt == 0.0 and "cost" in item:
            unit_cost = round(float(item.get("cost") or 0.0), 2)
            qty = round(float(item.get("qty") or 0.0), 2)
            cost_amt = round(unit_cost * qty, 2) if qty > 0 and unit_cost > 0 else unit_cost
        grand_tot = round(float(item.get("grand_total") or (cost_amt + gst_amt)), 2)
        if grand_tot <= 0:
            return None

        is_credit = int(item.get("is_credit") or 0)
        amount_paid = round(float(item.get("amount_paid") or 0.0), 2)
        p_mode = str(item.get("payment_mode") or "BANK").upper()
        supp_name = str(item.get("supplier_name") or f"Supplier (Inv #{inv_no})").strip()

        # Account mappings: Debit Inventory Stock (1050), NOT COGS (5010)!
        inv_acc = cls.get_mapped_account("inventory_raw", default_code="1050", conn=conn)
        gst_in_acc = cls.get_mapped_account("input_gst", default_code="2040", conn=conn)
        ap_acc = cls.get_mapped_account("accounts_payable", default_code="2010", conn=conn)
        liquid_acc = cls.resolve_liquid_account(p_mode, conn=conn)

        lines = []
        # Debits: Inventory Capitalization
        lines.append({
            "account_id": inv_acc["id"],
            "debit": cost_amt,
            "credit": 0.0,
            "description": f"Inventory Inward Purchase for Inv #{inv_no}",
            "party_type": "supplier",
            "party_name": supp_name,
        })
        if gst_amt > 0:
            lines.append({
                "account_id": gst_in_acc["id"],
                "debit": gst_amt,
                "credit": 0.0,
                "description": f"Input GST on Inv #{inv_no}",
                "party_type": "supplier",
                "party_name": supp_name,
                "tax_code": "GST_INPUT",
            })

        # Credits:
        if is_credit == 1 and amount_paid < grand_tot:
            bal_ap = round(grand_tot - amount_paid, 2)
            if amount_paid > 0:
                lines.append({
                    "account_id": liquid_acc["id"],
                    "debit": 0.0,
                    "credit": amount_paid,
                    "description": f"Upfront payment for Inv #{inv_no} via {p_mode}",
                    "party_type": "supplier",
                    "party_name": supp_name,
                })
            lines.append({
                "account_id": ap_acc["id"],
                "debit": 0.0,
                "credit": bal_ap,
                "description": f"Accounts Payable for Inv #{inv_no}",
                "party_type": "supplier",
                "party_name": supp_name,
            })
        else:
            lines.append({
                "account_id": liquid_acc["id"],
                "debit": 0.0,
                "credit": grand_tot,
                "description": f"Settlement for Inv #{inv_no} via {p_mode}",
                "party_type": "supplier",
                "party_name": supp_name,
            })

        entry_num = f"JV-PUR-{hashlib.md5(st_id.encode()).hexdigest()[:6].upper()}"
        narration = f"Jai Agency Inventory Inward: Inv #{inv_no} from {supp_name} [{p_mode}]"

        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": entry_num,
                "entry_date": arr_date_str,
                "source_module": "inventory",
                "source_entity": "purchase_invoice",
                "source_id": st_id,
                "reference_no": inv_no,
                "narration": narration,
                "status": "POSTED",
            },
            lines_data=lines,
            user="jai_agency",
            external_conn=conn,
        )

    # -------------------------------------------------------------------------
    # 4. CUSTOMER RECEIPTS (CREDIT COLLECTIONS)
    # -------------------------------------------------------------------------
    @classmethod
    def post_customer_payment(cls, pay, conn=None):
        """
        Receipt from customer for existing credit balance:
          Dr Cash / Bank / UPI
          Cr Accounts Receivable (Customer)
          (Does not double-count revenue)
        """
        p_id = str(pay["id"])
        amt = round(float(pay.get("amount") or 0.0), 2)
        if amt <= 0:
            return None

        p_mode = str(pay.get("payment_method") or "CASH").upper()
        b_id = str(pay.get("bill_id") or "")
        cust_name = pay.get("customer_name") or f"Customer (Bill #{b_id})"

        liquid_acc = cls.resolve_liquid_account(p_mode, conn=conn)
        ar_acc = cls.get_mapped_account("accounts_receivable", default_code="1040", conn=conn)

        lines = [
            {
                "account_id": liquid_acc["id"],
                "debit": amt,
                "credit": 0.0,
                "description": f"Receipt from {cust_name} for Bill #{b_id} via {p_mode}",
                "party_type": "customer",
                "party_name": cust_name,
            },
            {
                "account_id": ar_acc["id"],
                "debit": 0.0,
                "credit": amt,
                "description": f"Credit settlement for Bill #{b_id}",
                "party_type": "customer",
                "party_name": cust_name,
            }
        ]

        entry_number = f"JV-REC-{p_id.zfill(6)}"
        narration = f"Customer Credit Settlement #{p_id} for Bill #{b_id} ({cust_name}) via {p_mode}"

        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": entry_number,
                "entry_date": pay.get("date"),
                "source_module": "sales",
                "source_entity": "credit_payment",
                "source_id": p_id,
                "reference_no": f"RCPT-{p_id}",
                "narration": narration,
                "status": "POSTED",
            },
            lines_data=lines,
            user="jai_agency",
            external_conn=conn,
        )

    # -------------------------------------------------------------------------
    # 5. SUPPLIER PAYMENTS (PAYABLE SETTLEMENTS)
    # -------------------------------------------------------------------------
    @classmethod
    def post_supplier_payment(cls, pay, conn=None):
        """
        Disbursement to vendor for trade payable:
          Dr Accounts Payable (Supplier)
          Cr Cash / Bank
          (Does not double-count purchases)
        """
        sp_id = str(pay["id"])
        amt = round(float(pay.get("amount") or 0.0), 2)
        if amt <= 0:
            return None

        p_mode = str(pay.get("payment_mode") or "Bank Transfer").upper()
        inv_no = str(pay.get("invoice_no") or "")
        supp_name = pay.get("supplier_name") or f"Supplier #{pay.get('supplier_id', 1)}"

        liquid_acc = cls.resolve_liquid_account(p_mode, conn=conn)
        ap_acc = cls.get_mapped_account("accounts_payable", default_code="2010", conn=conn)

        lines = [
            {
                "account_id": ap_acc["id"],
                "debit": amt,
                "credit": 0.0,
                "description": f"Settlement of payable for {inv_no} to {supp_name}",
                "party_type": "supplier",
                "party_name": supp_name,
            },
            {
                "account_id": liquid_acc["id"],
                "debit": 0.0,
                "credit": amt,
                "description": f"Disbursement to {supp_name} via {p_mode}",
                "party_type": "supplier",
                "party_name": supp_name,
            }
        ]

        entry_number = f"JV-DISB-{sp_id.zfill(6)}"
        narration = f"Supplier Disbursement #{sp_id} for {inv_no} to {supp_name} via {p_mode}"

        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": entry_number,
                "entry_date": pay.get("date"),
                "source_module": "inventory",
                "source_entity": "supplier_payment",
                "source_id": sp_id,
                "reference_no": f"PAY-{sp_id}",
                "narration": narration,
                "status": "POSTED",
            },
            lines_data=lines,
            user="jai_agency",
            external_conn=conn,
        )

    # -------------------------------------------------------------------------
    # 6. OPERATING EXPENSES
    # -------------------------------------------------------------------------
    @classmethod
    def post_expense(cls, exp, conn=None):
        """
        Retail counter & shop expenses:
          Dr Mapped Operating Expense Account
          Cr Cash / Bank / UPI
        """
        e_id = str(exp["id"])
        amt = round(float(exp.get("amount") or 0.0), 2)
        if amt <= 0:
            return None

        cat = str(exp.get("category") or "General").strip()
        desc = str(exp.get("description") or cat).strip()
        p_method = str(exp.get("payment_method") or "CASH").upper()

        # Map expense category to specific COA account code
        cat_lower = cat.lower() + " " + desc.lower()
        if "office rent" in cat_lower or ("office" in cat_lower and "rent" in cat_lower):
            exp_code = "6040"  # Office Rent
        elif "rent" in cat_lower:
            exp_code = "6030"  # Shop Rent
        elif "tea" in cat_lower or "snack" in cat_lower or "petty" in cat_lower:
            exp_code = "6160"  # Petty Expenses
        elif "stationery" in cat_lower or "print" in cat_lower or "paper" in cat_lower:
            exp_code = "6050"  # Stationery & Printing
        elif "electric" in cat_lower or "power" in cat_lower or "water" in cat_lower or "utility" in cat_lower:
            exp_code = "6110"  # Utilities
        elif "travel" in cat_lower or "petrol" in cat_lower or "conveyance" in cat_lower:
            exp_code = "6130"  # Travelling
        elif "repair" in cat_lower or "maintain" in cat_lower:
            exp_code = "6120"  # Repairs
        elif "salary" in cat_lower or "wage" in cat_lower:
            exp_code = "6140"  # Salary
        elif "delivery" in cat_lower or "courier" in cat_lower or "freight" in cat_lower:
            exp_code = "6070"  # Delivery
        elif "market" in cat_lower or "ad" in cat_lower:
            exp_code = "6020"  # Marketing
        else:
            exp_code = "6010"  # Shop Expense

        exp_acc = ChartOfAccountsEngine.get_account_by_code(exp_code, conn=conn) or cls.get_mapped_account("cogs", default_code="6010", conn=conn)
        liquid_acc = cls.resolve_liquid_account(p_method, conn=conn)

        lines = [
            {
                "account_id": exp_acc["id"],
                "debit": amt,
                "credit": 0.0,
                "description": f"Expense: {desc} ({cat})",
            },
            {
                "account_id": liquid_acc["id"],
                "debit": 0.0,
                "credit": amt,
                "description": f"Paid via {p_method}",
            }
        ]

        entry_number = f"JV-EXP-{e_id.zfill(6)}"
        narration = f"Jai Agency Counter Expense #{e_id} [{cat}]: {desc} via {p_method}"

        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_number": entry_number,
                "entry_date": exp.get("date"),
                "source_module": "sales",
                "source_entity": "expense",
                "source_id": e_id,
                "reference_no": f"EXP-{e_id}",
                "narration": narration,
                "status": "POSTED",
            },
            lines_data=lines,
            user="jai_agency",
            external_conn=conn,
        )

    # -------------------------------------------------------------------------
    # 7. CAPITAL & DRAWINGS
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # 7. CAPITAL & DRAWINGS
    # -------------------------------------------------------------------------
    @classmethod
    def post_capital(cls, amount_or_data, payment_mode="Bank Transfer", narration=None, date=None, user="admin", conn=None):
        if isinstance(amount_or_data, dict):
            d = amount_or_data
            val = round(float(d.get("amount") or 0.0), 2)
            payment_mode = d.get("payment_method") or d.get("payment_mode") or payment_mode
            narration = d.get("narration") or f"Owner capital introduced by {d.get('investor_name') or 'Owner'}"
            date = d.get("date") or date
            user = d.get("user") or user
        else:
            val = round(float(amount_or_data), 2)

        if val <= 0:
            raise ValueError("Capital amount must be greater than zero.")

        liquid_acc = cls.resolve_liquid_account(payment_mode, conn=conn)
        cap_acc = cls.get_mapped_account("owner_capital", default_code="3010", conn=conn)

        lines = [
            {"account_id": liquid_acc["id"], "debit": val, "credit": 0.0, "description": "Capital Infusion into business"},
            {"account_id": cap_acc["id"], "debit": 0.0, "credit": val, "description": "Owner Capital Contribution"}
        ]
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date or datetime.datetime.now(),
                "source_module": "manual",
                "source_entity": "capital",
                "narration": narration or f"Owner capital introduced via {payment_mode}",
            },
            lines_data=lines,
            user=user,
            external_conn=conn,
        )

    post_capital_introduction = post_capital

    @classmethod
    def post_drawings(cls, amount_or_data, payment_mode="Cash", narration=None, date=None, user="admin", conn=None):
        if isinstance(amount_or_data, dict):
            d = amount_or_data
            val = round(float(d.get("amount") or 0.0), 2)
            payment_mode = d.get("payment_method") or d.get("payment_mode") or payment_mode
            narration = d.get("narration") or f"Owner drawings by {d.get('owner_name') or 'Owner'}"
            date = d.get("date") or date
            user = d.get("user") or user
        else:
            val = round(float(amount_or_data), 2)

        if val <= 0:
            raise ValueError("Drawings amount must be greater than zero.")

        draw_acc = cls.get_mapped_account("owner_drawings", default_code="3020", conn=conn)
        liquid_acc = cls.resolve_liquid_account(payment_mode, conn=conn)

        lines = [
            {"account_id": draw_acc["id"], "debit": val, "credit": 0.0, "description": "Owner personal drawings"},
            {"account_id": liquid_acc["id"], "debit": 0.0, "credit": val, "description": f"Withdrawn from {payment_mode}"}
        ]
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date or datetime.datetime.now(),
                "source_module": "manual",
                "source_entity": "drawings",
                "narration": narration or f"Owner drawings for personal use via {payment_mode}",
            },
            lines_data=lines,
            user=user,
            external_conn=conn,
        )

    # -------------------------------------------------------------------------
    # 8. FIXED ASSETS & DEPRECIATION
    # -------------------------------------------------------------------------
    @classmethod
    def post_fixed_asset_purchase(cls, asset_data_or_code, asset_name=None, cost=None, account_code=None, payment_mode="Bank Transfer", supplier=None, date=None, user="admin", conn=None):
        if isinstance(asset_data_or_code, dict):
            d = asset_data_or_code
            asset_code = d.get("asset_code") or d.get("code") or "AST-01"
            asset_name = d.get("asset_name") or d.get("name") or "Fixed Asset"
            val = round(float(d.get("amount") or d.get("cost") or 0.0), 2)
            cat = d.get("category")
            account_code = d.get("account_code")
            if not account_code and cat:
                cat_lower = cat.lower()
                if "computer" in cat_lower or "equipment" in cat_lower:
                    account_code = "1140"
                elif "machinery" in cat_lower or "plant" in cat_lower:
                    account_code = "1130"
                elif "vehicle" in cat_lower:
                    account_code = "1150"
                elif "furniture" in cat_lower:
                    account_code = "1145"
                elif "land" in cat_lower or "building" in cat_lower:
                    account_code = "1110"
                else:
                    account_code = "1155"
            payment_mode = d.get("payment_method") or d.get("payment_mode") or payment_mode
            supplier = d.get("supplier") or supplier
            date = d.get("date") or date
            user = d.get("user") or user
        else:
            asset_code = asset_data_or_code
            val = round(float(cost or 0.0), 2)

        if val <= 0:
            raise ValueError("Asset cost must be greater than zero.")

        target_code = account_code or "1130"
        asset_acc = ChartOfAccountsEngine.get_account_by_code(target_code, conn=conn) or cls.get_mapped_account("cogs", default_code="1130", conn=conn)
        liquid_acc = cls.resolve_liquid_account(payment_mode, conn=conn)

        lines = [
            {
                "account_id": asset_acc["id"],
                "debit": val,
                "credit": 0.0,
                "description": f"Fixed Asset Acquisition: {asset_name} ({asset_code})",
                "party_type": "supplier" if supplier else None,
                "party_name": supplier
            },
            {
                "account_id": liquid_acc["id"],
                "debit": 0.0,
                "credit": val,
                "description": f"Payment for asset acquisition via {payment_mode}",
                "party_type": "supplier" if supplier else None,
                "party_name": supplier
            }
        ]
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date or datetime.datetime.now(),
                "source_module": "manual",
                "source_entity": "fixed_asset",
                "source_id": asset_code,
                "reference_no": asset_code,
                "narration": f"Acquisition of fixed asset: {asset_name} [{asset_code}] via {payment_mode}",
            },
            lines_data=lines,
            user=user,
            external_conn=conn,
        )

    @classmethod
    def post_depreciation(cls, depr_data_or_code, asset_name=None, amount=None, date=None, user="admin", conn=None):
        if isinstance(depr_data_or_code, dict):
            d = depr_data_or_code
            asset_code = d.get("asset_code") or "AST-01"
            asset_name = d.get("asset_name") or "Asset"
            val = round(float(d.get("depreciation_amount") or d.get("amount") or 0.0), 2)
            date = d.get("date") or date
            user = d.get("user") or user
        else:
            asset_code = depr_data_or_code
            val = round(float(amount or 0.0), 2)

        if val <= 0:
            raise ValueError("Depreciation amount must be greater than zero.")

        depr_exp_acc = cls.get_mapped_account("depreciation_expense", default_code="6090", conn=conn)
        accum_depr_acc = cls.get_mapped_account("accumulated_depreciation", default_code="1160", conn=conn)

        lines = [
            {"account_id": depr_exp_acc["id"], "debit": val, "credit": 0.0, "description": f"Depreciation write-off: {asset_name}"},
            {"account_id": accum_depr_acc["id"], "debit": 0.0, "credit": val, "description": f"Accumulated Depreciation on {asset_code}"}
        ]
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date or datetime.datetime.now(),
                "source_module": "manual",
                "source_entity": "depreciation",
                "source_id": f"depr_{asset_code}",
                "reference_no": f"DEP-{asset_code}",
                "narration": f"Depreciation write-off for {asset_name} [{asset_code}]",
            },
            lines_data=lines,
            user=user,
            external_conn=conn,
        )

    # -------------------------------------------------------------------------
    # 9. LOANS & LIABILITIES
    # -------------------------------------------------------------------------
    @classmethod
    def post_loan_disbursement(cls, loan_data_or_code, title=None, principal_amount=None, lender=None, date=None, user="admin", conn=None):
        if isinstance(loan_data_or_code, dict):
            d = loan_data_or_code
            loan_code = d.get("liability_code") or d.get("loan_code") or "LOAN-01"
            title = d.get("title") or "Loan"
            val = round(float(d.get("amount") or d.get("principal_amount") or 0.0), 2)
            lender = d.get("lender") or lender
            date = d.get("date") or date
            user = d.get("user") or user
        else:
            loan_code = loan_data_or_code
            val = round(float(principal_amount or 0.0), 2)

        if val <= 0:
            raise ValueError("Loan principal must be greater than zero.")

        bank_acc = cls.get_mapped_account("bank_default", default_code="1020", conn=conn)
        loan_acc = ChartOfAccountsEngine.get_account_by_code("2110", conn=conn) or cls.get_mapped_account("bank_loan", default_code="2110", conn=conn)

        lines = [
            {"account_id": bank_acc["id"], "debit": val, "credit": 0.0, "description": f"Loan proceeds received: {title}", "party_name": lender},
            {"account_id": loan_acc["id"], "debit": 0.0, "credit": val, "description": f"Loan Liability: {title} ({loan_code})", "party_name": lender}
        ]
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date or datetime.datetime.now(),
                "source_module": "manual",
                "source_entity": "loan",
                "source_id": loan_code,
                "reference_no": loan_code,
                "narration": f"Loan received: {title} from {lender or 'Bank'}",
            },
            lines_data=lines,
            user=user,
            external_conn=conn,
        )

    post_loan_received = post_loan_disbursement

    @classmethod
    def post_loan_repayment(cls, rep_data_or_code, title=None, principal_part=None, interest_part=0.0, date=None, user="admin", conn=None):
        payment_mode = "Bank Transfer"
        if isinstance(rep_data_or_code, dict):
            d = rep_data_or_code
            loan_code = d.get("liability_code") or d.get("loan_code") or "LOAN-01"
            title = d.get("title") or "Loan"
            p_val = round(float(d.get("principal_amount") or d.get("principal_part") or 0.0), 2)
            i_val = round(float(d.get("interest_amount") or d.get("interest_part") or 0.0), 2)
            payment_mode = d.get("payment_method") or d.get("payment_mode") or payment_mode
            date = d.get("date") or date
            user = d.get("user") or user
        else:
            loan_code = rep_data_or_code
            p_val = round(float(principal_part or 0.0), 2)
            i_val = round(float(interest_part or 0.0), 2)

        total_repaid = round(p_val + i_val, 2)
        if total_repaid <= 0:
            raise ValueError("Repayment amount must be greater than zero.")

        loan_acc = ChartOfAccountsEngine.get_account_by_code("2110", conn=conn) or cls.get_mapped_account("bank_loan", default_code="2110", conn=conn)
        interest_acc = ChartOfAccountsEngine.get_account_by_code("7020", conn=conn) or cls.get_mapped_account("loan_interest", default_code="7020", conn=conn)
        bank_acc = cls.resolve_liquid_account(payment_mode, conn=conn)

        lines = []
        if p_val > 0:
            lines.append({"account_id": loan_acc["id"], "debit": p_val, "credit": 0.0, "description": f"Loan Principal Repayment: {title}"})
        if i_val > 0:
            lines.append({"account_id": interest_acc["id"], "debit": i_val, "credit": 0.0, "description": f"Loan Interest Expense: {title}"})
        lines.append({"account_id": bank_acc["id"], "debit": 0.0, "credit": total_repaid, "description": f"Disbursement for loan installment: {title}"})

        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date or datetime.datetime.now(),
                "source_module": "manual",
                "source_entity": "loan_repayment",
                "source_id": f"pay_{loan_code}",
                "reference_no": f"LOAN-PAY-{loan_code}",
                "narration": f"Loan repayment for {title} (Principal: Rs. {p_val}, Interest: Rs. {i_val})",
            },
            lines_data=lines,
            user=user,
            external_conn=conn,
        )

    # -------------------------------------------------------------------------
    # 10. ACCOUNTS RECEIVABLE EXTENSIONS (CREDIT NOTES, ADVANCES, WRITE-OFFS)
    # -------------------------------------------------------------------------
    @classmethod
    def post_customer_credit_note(cls, note_data, conn=None):
        """
        Customer Credit Note (Sales Return / Allowance / Discount):
          Dr Sales Returns (4015) [Base Amount]
          Dr GST Output Payable (2030) [Tax Amount, if applicable]
          Cr Accounts Receivable (1040) [Total Credit Note Amount]
        Atomically reduces customer remaining balance in accounts_receivables and registers credit note.
        """
        d = note_data
        amount = round(float(d.get("amount") or 0.0), 2)
        tax_amount = round(float(d.get("tax_amount") or 0.0), 2)
        base_amount = max(0.0, round(amount - tax_amount, 2)) if tax_amount > 0 else amount
        cust_name = str(d.get("customer_name") or "General Customer").strip()
        rec_id = d.get("receivable_id")
        reason = d.get("reason") or "Customer credit note / allowance"
        user = d.get("user") or "admin"
        date_str = str(d.get("date") or datetime.date.today().isoformat())

        if amount <= 0:
            raise ValueError("Credit note amount must be greater than zero.")

        # Resolve accounts
        sales_ret_acc = cls.get_mapped_account("sales_returns", default_code="4015", conn=conn)
        gst_out_acc = cls.get_mapped_account("output_gst", default_code="2030", conn=conn)
        ar_acc = cls.get_mapped_account("accounts_receivable", default_code="1040", conn=conn)

        lines = [
            {
                "account_id": sales_ret_acc["id"],
                "debit": base_amount,
                "credit": 0.0,
                "description": f"Credit Note: {reason}",
                "party_type": "customer",
                "party_name": cust_name
            }
        ]
        if tax_amount > 0:
            lines.append({
                "account_id": gst_out_acc["id"],
                "debit": tax_amount,
                "credit": 0.0,
                "description": f"GST Output adjustment on Credit Note",
                "party_type": "customer",
                "party_name": cust_name,
                "tax_code": "GST_OUTPUT"
            })
        lines.append({
            "account_id": ar_acc["id"],
            "debit": 0.0,
            "credit": amount,
            "description": f"Credit Note reduction for {cust_name}",
            "party_type": "customer",
            "party_name": cust_name
        })

        cn_no = d.get("credit_note_no") or generate_unique_number("CN", "accounts_credit_notes", "credit_note_no")
        entry = DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "sales",
                "source_entity": "credit_note",
                "source_id": cn_no,
                "reference_no": cn_no,
                "narration": f"Customer Credit Note #{cn_no} for {cust_name}: {reason}",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )
        entry_id = entry["entry_id"]

        # Update accounts_receivables and insert accounts_credit_notes
        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            if rec_id:
                cur.execute("SELECT id, total_amount, paid_amount, remaining_balance FROM accounts_receivables WHERE id = %s;", (rec_id,))
                rec = cur.fetchone()
                if rec:
                    new_rem = max(0.0, round(float(rec["remaining_balance"]) - amount, 2))
                    new_status = "Paid" if new_rem <= 0.01 else "Partial"
                    cur.execute("""
                        UPDATE accounts_receivables
                        SET remaining_balance = %s, status = %s
                        WHERE id = %s;
                    """, (new_rem, new_status, rec["id"]))

            cur.execute("""
                INSERT INTO accounts_credit_notes
                    (credit_note_no, receivable_id, customer_name, note_date, amount, tax_amount, reason, journal_entry_id, created_by)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (cn_no, rec_id, cust_name, date_str, amount, tax_amount, reason, entry_id, user))
            conn.commit()
            return {"status": "success", "credit_note_no": cn_no, "entry_id": entry_id, "journal_entry_id": entry_id, "amount": amount, "entry_number": entry.get("entry_number")}
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def post_customer_advance(cls, advance_data, conn=None):
        """
        Customer Advance Received (Unearned Revenue / Deposit Liability):
          Dr Cash / Bank / UPI (Liquid Asset)
          Cr Customer Advances (2060 Liability)
        """
        d = advance_data
        amount = round(float(d.get("amount") or 0.0), 2)
        if amount <= 0:
            raise ValueError("Advance amount must be greater than zero.")

        cust_name = str(d.get("customer_name") or "Customer").strip()
        p_mode = str(d.get("payment_method") or "Bank Transfer").strip()
        date_str = str(d.get("date") or datetime.date.today().isoformat())
        ref_no = d.get("reference_no") or f"ADV-CUST-{int(datetime.datetime.now().timestamp())}"
        notes = d.get("notes") or f"Customer advance from {cust_name}"
        user = d.get("user") or "admin"

        liquid_acc = cls.resolve_liquid_account(p_mode, conn=conn)
        adv_acc = ChartOfAccountsEngine.get_account_by_code("2060", conn=conn) or cls.get_mapped_account("customer_advances", default_code="2060", conn=conn)

        lines = [
            {
                "account_id": liquid_acc["id"],
                "debit": amount,
                "credit": 0.0,
                "description": f"Advance from {cust_name} via {p_mode}",
                "party_type": "customer",
                "party_name": cust_name
            },
            {
                "account_id": adv_acc["id"],
                "debit": 0.0,
                "credit": amount,
                "description": f"Customer Advance Deposit: {cust_name}",
                "party_type": "customer",
                "party_name": cust_name
            }
        ]

        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "sales",
                "source_entity": "customer_advance",
                "reference_no": ref_no,
                "narration": f"Customer Advance: {cust_name} (Rs. {amount:,.2f}) via {p_mode}",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    @classmethod
    def post_customer_writeoff(cls, writeoff_data, conn=None):
        """
        Customer Bad Debt Write-Off:
          Dr Miscellaneous Expenses / Bad Debts (6170)
          Cr Accounts Receivable (1040)
        Updates receivable record balance to 0 and marks as 'Written Off'.
        """
        d = writeoff_data
        amount = round(float(d.get("amount") or 0.0), 2)
        rec_id = d.get("receivable_id")
        reason = d.get("reason") or "Uncollectible bad debt write-off"
        user = d.get("user") or "admin"
        date_str = str(d.get("date") or datetime.date.today().isoformat())

        if amount <= 0:
            raise ValueError("Write-off amount must be greater than zero.")

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cust_name = "Customer"
            if rec_id:
                cur.execute("SELECT id, invoice_ref, customer_name, remaining_balance FROM accounts_receivables WHERE id = %s;", (rec_id,))
                rec = cur.fetchone()
                if rec:
                    cust_name = rec["customer_name"]
                    amount = min(amount, float(rec["remaining_balance"]))

            exp_acc = ChartOfAccountsEngine.get_account_by_code("6170", conn=conn) or cls.get_mapped_account("bad_debts", default_code="6170", conn=conn)
            ar_acc = cls.get_mapped_account("accounts_receivable", default_code="1040", conn=conn)

            lines = [
                {
                    "account_id": exp_acc["id"],
                    "debit": amount,
                    "credit": 0.0,
                    "description": f"Bad Debt Write-Off: {cust_name} ({reason})",
                    "party_type": "customer",
                    "party_name": cust_name
                },
                {
                    "account_id": ar_acc["id"],
                    "debit": 0.0,
                    "credit": amount,
                    "description": f"AR derecognition for {cust_name}",
                    "party_type": "customer",
                    "party_name": cust_name
                }
            ]

            res = DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_date": date_str,
                    "source_module": "sales",
                    "source_entity": "bad_debt_writeoff",
                    "source_id": str(rec_id or "WO"),
                    "reference_no": f"WO-{rec_id or int(datetime.datetime.now().timestamp())}",
                    "narration": f"Bad Debt Write-off for {cust_name}: Rs. {amount:,.2f} ({reason})",
                    "status": "POSTED"
                },
                lines_data=lines,
                user=user,
                external_conn=conn
            )

            if rec_id:
                new_rem = max(0.0, round(float(rec["remaining_balance"]) - amount, 2))
                new_status = 'Written Off' if new_rem <= 0.01 else (rec.get("status") or "Partial")
                cur.execute("""
                    UPDATE accounts_receivables
                    SET remaining_balance = %s, status = %s, notes = COALESCE(notes, '') || ' | Written Off Rs. ' || %s
                    WHERE id = %s;
                """, (new_rem, new_status, str(amount), rec_id))
                conn.commit()

            return res
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 11. ACCOUNTS PAYABLE EXTENSIONS (DEBIT NOTES, SUPPLIER ADVANCES)
    # -------------------------------------------------------------------------
    @classmethod
    def post_supplier_debit_note(cls, note_data, conn=None):
        """
        Supplier Debit Note (Purchase Return / Vendor Allowance):
          Dr Accounts Payable (2010) [Total Debit Note Amount]
          Cr COGS / Raw Materials Purchases (5010) [Base Amount]
          Cr GST Input Credit (2040) [Tax Reversal Amount, if applicable]
        Atomically reduces supplier remaining payable in accounts_payables and registers debit note.
        """
        d = note_data
        amount = round(float(d.get("amount") or 0.0), 2)
        tax_amount = round(float(d.get("tax_amount") or 0.0), 2)
        base_amount = max(0.0, round(amount - tax_amount, 2)) if tax_amount > 0 else amount
        supp_name = str(d.get("supplier_name") or "Supplier").strip()
        pay_id = d.get("payable_id")
        reason = d.get("reason") or "Supplier debit note / return"
        user = d.get("user") or "admin"
        date_str = str(d.get("date") or datetime.date.today().isoformat())

        if amount <= 0:
            raise ValueError("Debit note amount must be greater than zero.")

        ap_acc = cls.get_mapped_account("accounts_payable", default_code="2010", conn=conn)
        inv_acc = cls.get_mapped_account("inventory_raw", default_code="1050", conn=conn)
        gst_in_acc = cls.get_mapped_account("input_gst", default_code="2040", conn=conn)

        lines = [
            {
                "account_id": ap_acc["id"],
                "debit": amount,
                "credit": 0.0,
                "description": f"Debit Note reduction to {supp_name}",
                "party_type": "supplier",
                "party_name": supp_name
            },
            {
                "account_id": inv_acc["id"],
                "debit": 0.0,
                "credit": base_amount,
                "description": f"Inventory return reduction: {reason}",
                "party_type": "supplier",
                "party_name": supp_name
            }
        ]
        if tax_amount > 0:
            lines.append({
                "account_id": gst_in_acc["id"],
                "debit": 0.0,
                "credit": tax_amount,
                "description": f"Input GST ITC Reversal on Debit Note",
                "party_type": "supplier",
                "party_name": supp_name,
                "tax_code": "GST_INPUT"
            })

        dn_no = d.get("debit_note_no") or generate_unique_number("DN", "accounts_debit_notes", "debit_note_no")
        entry = DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "inventory",
                "source_entity": "debit_note",
                "source_id": dn_no,
                "reference_no": dn_no,
                "narration": f"Supplier Debit Note #{dn_no} for {supp_name}: {reason}",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )
        entry_id = entry["entry_id"]

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            if pay_id:
                cur.execute("SELECT id, total_amount, paid_amount, remaining_balance FROM accounts_payables WHERE id = %s;", (pay_id,))
                pay = cur.fetchone()
                if pay:
                    new_rem = max(0.0, round(float(pay["remaining_balance"]) - amount, 2))
                    new_status = "Paid" if new_rem <= 0.01 else "Partial"
                    cur.execute("""
                        UPDATE accounts_payables
                        SET remaining_balance = %s, status = %s
                        WHERE id = %s;
                    """, (new_rem, new_status, pay["id"]))

            cur.execute("""
                INSERT INTO accounts_debit_notes
                    (debit_note_no, payable_id, supplier_name, note_date, amount, tax_amount, reason, journal_entry_id, created_by)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (dn_no, pay_id, supp_name, date_str, amount, tax_amount, reason, entry_id, user))
            conn.commit()
            return {"status": "success", "debit_note_no": dn_no, "entry_id": entry_id, "journal_entry_id": entry_id, "amount": amount, "entry_number": entry.get("entry_number")}
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def post_supplier_advance(cls, advance_data, conn=None):
        """
        Supplier Advance Paid (Prepaid Vendor Deposit Asset):
          Dr Supplier Advances (1070 Current Asset)
          Cr Cash / Bank / UPI (Liquid Asset)
        """
        d = advance_data
        amount = round(float(d.get("amount") or 0.0), 2)
        if amount <= 0:
            raise ValueError("Advance amount must be greater than zero.")

        supp_name = str(d.get("supplier_name") or "Supplier").strip()
        p_mode = str(d.get("payment_method") or "Bank Transfer").strip()
        date_str = str(d.get("date") or datetime.date.today().isoformat())
        ref_no = d.get("reference_no") or f"ADV-SUPP-{int(datetime.datetime.now().timestamp())}"
        user = d.get("user") or "admin"

        adv_acc = ChartOfAccountsEngine.get_account_by_code("1070", conn=conn) or cls.get_mapped_account("supplier_advances", default_code="1070", conn=conn)
        liquid_acc = cls.resolve_liquid_account(p_mode, conn=conn)

        lines = [
            {
                "account_id": adv_acc["id"],
                "debit": amount,
                "credit": 0.0,
                "description": f"Supplier Advance to {supp_name}",
                "party_type": "supplier",
                "party_name": supp_name
            },
            {
                "account_id": liquid_acc["id"],
                "debit": 0.0,
                "credit": amount,
                "description": f"Disbursement via {p_mode}",
                "party_type": "supplier",
                "party_name": supp_name
            }
        ]

        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "inventory",
                "source_entity": "supplier_advance",
                "reference_no": ref_no,
                "narration": f"Supplier Advance: {supp_name} (Rs. {amount:,.2f}) via {p_mode}",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    # -------------------------------------------------------------------------
    # 12. CASH & BANK ACCOUNTING (CONTRA TRANSFERS & BANK CHARGES)
    # -------------------------------------------------------------------------
    @classmethod
    def post_contra_transfer(cls, transfer_data_or_from, to_code=None, amount=None, payment_date=None, narration=None, reference_no=None, user="admin", conn=None):
        """
        Pure Contra Double-Entry Transfer between Cash and Bank accounts:
          Dr Target Liquid Account (Cash / Bank / UPI)
          Cr Source Liquid Account (Cash / Bank / UPI)
        Strictly prohibits income or expense classifications.
        """
        if isinstance(transfer_data_or_from, dict):
            d = transfer_data_or_from
            from_code = str(d.get("from_account_code") or d.get("source_code") or "1020")
            to_code = str(d.get("to_account_code") or d.get("target_code") or "1010")
            val = round(float(d.get("amount") or 0.0), 2)
            payment_date = d.get("date") or payment_date
            narration = d.get("narration") or narration
            reference_no = d.get("reference_no") or reference_no
            user = d.get("user") or user
        else:
            from_code = str(transfer_data_or_from)
            to_code = str(to_code)
            val = round(float(amount or 0.0), 2)

        if val <= 0:
            raise ValueError("Transfer amount must be greater than zero.")
        if from_code == to_code:
            raise ValueError("Source account and target account cannot be identical in a contra transfer.")

        from_acc = ChartOfAccountsEngine.get_account_by_code(from_code, conn=conn)
        to_acc = ChartOfAccountsEngine.get_account_by_code(to_code, conn=conn)
        if not from_acc or not to_acc:
            raise ValueError(f"Could not resolve accounts for contra transfer ({from_code} -> {to_code}).")

        # Invariant: Must both be liquid Asset accounts
        if from_acc["major_type"] != "Asset" or to_acc["major_type"] != "Asset":
            raise ValueError("Contra transfers are strictly restricted to Liquid Asset accounts (Cash, Bank, UPI).")

        default_narration = f"Contra Transfer: {from_acc['name']} to {to_acc['name']}"
        lines = [
            {
                "account_id": to_acc["id"],
                "debit": val,
                "credit": 0.0,
                "description": f"Funds received from {from_acc['name']}"
            },
            {
                "account_id": from_acc["id"],
                "debit": 0.0,
                "credit": val,
                "description": f"Funds transferred to {to_acc['name']}"
            }
        ]

        ref = reference_no or f"CONTRA-{int(datetime.datetime.now().timestamp())}"
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": payment_date or datetime.date.today().isoformat(),
                "source_module": "banking",
                "source_entity": "contra_transfer",
                "reference_no": ref,
                "narration": narration or default_narration,
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    @classmethod
    def post_bank_charges(cls, charges_data_or_code, amount=None, date=None, narration=None, reference_no=None, user="admin", conn=None):
        """
        Bank charges / maintenance fees:
          Dr Bank Charges & Fees (7010 Financial Cost)
          Cr Bank Account (1020 Liquid Asset)
        """
        if isinstance(charges_data_or_code, dict):
            d = charges_data_or_code
            bank_code = str(d.get("bank_account_code") or "1020")
            val = round(float(d.get("amount") or 0.0), 2)
            date = d.get("date") or date
            narration = d.get("narration") or narration
            reference_no = d.get("reference_no") or reference_no
            user = d.get("user") or user
        else:
            bank_code = str(charges_data_or_code)
            val = round(float(amount or 0.0), 2)

        if val <= 0:
            raise ValueError("Bank charges amount must be greater than zero.")

        bank_acc = ChartOfAccountsEngine.get_account_by_code(bank_code, conn=conn) or cls.get_mapped_account("bank_default", default_code="1020", conn=conn)
        charges_acc = ChartOfAccountsEngine.get_account_by_code("7010", conn=conn) or cls.get_mapped_account("bank_charges", default_code="7010", conn=conn)

        lines = [
            {"account_id": charges_acc["id"], "debit": val, "credit": 0.0, "description": "Bank service & transaction charges"},
            {"account_id": bank_acc["id"], "debit": 0.0, "credit": val, "description": f"Deducted from {bank_acc['name']}"}
        ]

        ref = reference_no or f"BNK-CHG-{int(datetime.datetime.now().timestamp())}"
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date or datetime.date.today().isoformat(),
                "source_module": "banking",
                "source_entity": "bank_charges",
                "reference_no": ref,
                "narration": narration or f"Bank maintenance / processing charges on {bank_acc['name']}",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    # -------------------------------------------------------------------------
    # 13. FIXED ASSETS: PERIODIC DEPRECIATION & DISPOSAL
    # -------------------------------------------------------------------------
    @classmethod
    def post_periodic_depreciation(cls, asset_data_or_id=None, depreciation_date=None, amount=None, user="admin", asset_id=None, depreciation_amount=None, conn=None):
        """
        Posts periodic depreciation with accounting period validation and duplicate prevention:
          Dr Depreciation Expense (6090)
          Cr Accumulated Depreciation (1160)
        Updates accounts_fixed_assets and inserts audit log in accounts_asset_depreciation_log.
        """
        if asset_id is not None and asset_data_or_id is None:
            asset_data_or_id = asset_id
        if depreciation_amount is not None and amount is None:
            amount = depreciation_amount

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True

        cur = conn.cursor(dictionary=True)
        try:
            target_id = asset_data_or_id["id"] if isinstance(asset_data_or_id, dict) else asset_data_or_id
            asset_id = target_id
            cur.execute("SELECT * FROM accounts_fixed_assets WHERE id = %s;", (target_id,))
            asset = cur.fetchone()
            if not asset:
                raise ValueError(f"Fixed asset #{asset_id} not found.")
            if asset.get("status") == "Disposed":
                raise ValueError(f"Cannot depreciate asset '{asset['asset_name']}' because it is marked as Disposed.")

            depr_date = str(depreciation_date or datetime.date.today().isoformat()).split("T")[0]

            # 1. Period Closed / Locked Check
            PeriodControlEngine.validate_transaction_date(depr_date, conn=conn)

            # 2. Duplicate Check for same asset in same month / period
            month_prefix = depr_date[:7]
            cur.execute("""
                SELECT id FROM accounts_asset_depreciation_log
                WHERE asset_id = %s AND depreciation_date LIKE %s;
            """, (asset_id, f"{month_prefix}%"))
            if cur.fetchone():
                raise ValueError(f"Asset '{asset['asset_name']}' has already been depreciated for {month_prefix}.")

            # 3. Calculate Depreciation
            curr_val = round(float(asset["current_value"]), 2)
            if curr_val <= 0:
                raise ValueError(f"Asset '{asset['asset_name']}' is already fully depreciated (Net Book Value is zero).")

            if amount is not None:
                depr_val = round(float(amount), 2)
            else:
                rate = float(asset.get("depreciation_rate") or 10.0)
                cost = float(asset.get("purchase_value") or curr_val)
                depr_val = round((cost * (rate / 100.0)) / 12.0, 2)
                if depr_val <= 0:
                    depr_val = round(cost / (float(asset.get("useful_life_years") or 5) * 12.0), 2)

            depr_val = min(depr_val, curr_val)
            if depr_val <= 0:
                raise ValueError("Calculated depreciation amount is zero.")

            # 4. Post Journal Entry
            depr_exp_acc = cls.get_mapped_account("depreciation_expense", default_code="6090", conn=conn)
            accum_depr_acc = cls.get_mapped_account("accumulated_depreciation", default_code="1160", conn=conn)

            lines = [
                {
                    "account_id": depr_exp_acc["id"],
                    "debit": depr_val,
                    "credit": 0.0,
                    "description": f"Periodic Depreciation: {asset['asset_name']} ({asset['asset_code']})"
                },
                {
                    "account_id": accum_depr_acc["id"],
                    "debit": 0.0,
                    "credit": depr_val,
                    "description": f"Accumulated Depreciation on {asset['asset_code']}"
                }
            ]

            entry = DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_date": depr_date,
                    "source_module": "asset",
                    "source_entity": "depreciation",
                    "source_id": str(asset_id),
                    "reference_no": f"DEP-{asset['asset_code']}-{month_prefix}",
                    "narration": f"Periodic depreciation write-off for {asset['asset_name']} [{asset['asset_code']}] for {month_prefix}",
                    "status": "POSTED"
                },
                lines_data=lines,
                user=user,
                external_conn=conn
            )
            entry_id = entry["entry_id"]

            # 5. Update Asset Register & Insert Depreciation Log
            new_accum = round(float(asset["accumulated_depreciation"]) + depr_val, 2)
            new_val = max(0.0, round(curr_val - depr_val, 2))
            cur.execute("""
                UPDATE accounts_fixed_assets
                SET accumulated_depreciation = %s, current_value = %s
                WHERE id = %s;
            """, (new_accum, new_val, asset_id))

            cur.execute("""
                INSERT INTO accounts_asset_depreciation_log
                    (asset_id, depreciation_date, depreciation_amount, book_value_before, book_value_after, journal_entry_id, created_by)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s);
            """, (asset_id, depr_date, depr_val, curr_val, new_val, entry_id, user))
            conn.commit()

            return {
                "status": "success",
                "asset_id": asset_id,
                "asset_name": asset["asset_name"],
                "depreciation_amount": depr_val,
                "new_book_value": new_val,
                "new_net_book_value": new_val,
                "new_accumulated_depreciation": new_accum,
                "accumulated_depreciation": new_accum,
                "journal_entry_id": entry_id
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    @classmethod
    def post_asset_disposal(cls, disposal_data, conn=None):
        """
        Fixed Asset Disposal / Sale:
          Dr Cash / Bank (Proceeds, if > 0)
          Dr Accumulated Depreciation (1160) (Entire accumulated depreciation)
          Dr Loss on Asset Disposal (6170) [If proceeds < Net Book Value]
          Cr Gain on Asset Disposal (4030) [If proceeds > Net Book Value]
          Cr Fixed Asset Account (1110-1155) [Original Purchase Cost]
        """
        d = disposal_data
        asset_id = d.get("asset_id")
        proceeds = max(0.0, round(float(d.get("disposal_proceeds") or d.get("sale_proceeds") or d.get("proceeds") or 0.0), 2))
        p_mode = str(d.get("payment_method") or "Bank Transfer").strip()
        disp_date = str(d.get("disposal_date") or datetime.date.today().isoformat()).split("T")[0]
        notes = d.get("notes") or "Asset disposal / sale"
        user = d.get("user") or "admin"

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM accounts_fixed_assets WHERE id = %s;", (asset_id,))
            asset = cur.fetchone()
            if not asset:
                raise ValueError(f"Fixed asset #{asset_id} not found.")
            if asset.get("status") == "Disposed":
                raise ValueError(f"Asset '{asset['asset_name']}' is already disposed.")

            cost = round(float(asset["purchase_value"]), 2)
            accum = round(float(asset["accumulated_depreciation"]), 2)
            nbv = max(0.0, round(cost - accum, 2))
            gain_loss = round(proceeds - nbv, 2)

            # Resolve Fixed Asset COA Account
            cat = str(asset.get("category") or "").lower()
            if "machinery" in cat or "plant" in cat:
                target_code = "1130"
            elif "vehicle" in cat:
                target_code = "1150"
            elif "building" in cat or "premises" in cat:
                target_code = "1120"
            elif "land" in cat:
                target_code = "1110"
            elif "furniture" in cat:
                target_code = "1145"
            else:
                target_code = "1140"

            asset_acc = ChartOfAccountsEngine.get_account_by_code(target_code, conn=conn) or cls.get_mapped_account("fixed_assets", default_code="1140", conn=conn)
            accum_acc = cls.get_mapped_account("accumulated_depreciation", default_code="1160", conn=conn)
            liquid_acc = cls.resolve_liquid_account(p_mode, conn=conn)

            lines = []
            if proceeds > 0:
                lines.append({
                    "account_id": liquid_acc["id"],
                    "debit": proceeds,
                    "credit": 0.0,
                    "description": f"Sale proceeds from {asset['asset_name']} via {p_mode}"
                })
            if accum > 0:
                lines.append({
                    "account_id": accum_acc["id"],
                    "debit": accum,
                    "credit": 0.0,
                    "description": f"Derecognition of accumulated depreciation: {asset['asset_name']}"
                })
            if gain_loss < 0:
                # Loss on disposal -> OpEx / Misc Expense 6170
                loss_amt = round(abs(gain_loss), 2)
                loss_acc = ChartOfAccountsEngine.get_account_by_code("6170", conn=conn)
                lines.append({
                    "account_id": loss_acc["id"],
                    "debit": loss_amt,
                    "credit": 0.0,
                    "description": f"Loss on disposal of {asset['asset_name']}"
                })
            elif gain_loss > 0:
                # Gain on disposal -> Other Income 4030
                gain_amt = round(gain_loss, 2)
                gain_acc = ChartOfAccountsEngine.get_account_by_code("4030", conn=conn)
                lines.append({
                    "account_id": gain_acc["id"],
                    "debit": 0.0,
                    "credit": gain_amt,
                    "description": f"Gain on disposal of {asset['asset_name']}"
                })

            # Credit original asset cost
            lines.append({
                "account_id": asset_acc["id"],
                "debit": 0.0,
                "credit": cost,
                "description": f"Derecognition of fixed asset: {asset['asset_name']} ({asset['asset_code']})"
            })

            entry = DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_date": disp_date,
                    "source_module": "asset",
                    "source_entity": "asset_disposal",
                    "source_id": str(asset_id),
                    "reference_no": f"DISP-{asset['asset_code']}",
                    "narration": f"Asset Disposal: {asset['asset_name']} [{asset['asset_code']}] (Proceeds: Rs. {proceeds:,.2f}, Gain/Loss: Rs. {gain_loss:,.2f})",
                    "status": "POSTED"
                },
                lines_data=lines,
                user=user,
                external_conn=conn
            )
            entry_id = entry["entry_id"]

            cur.execute("""
                UPDATE accounts_fixed_assets
                SET status = 'Disposed', current_value = 0.0, disposal_date = %s,
                    disposal_proceeds = %s, disposal_gain_loss = %s, disposal_journal_id = %s,
                    notes = COALESCE(notes, '') || ' | ' || %s
                WHERE id = %s;
            """, (disp_date, proceeds, gain_loss, entry_id, notes, asset_id))
            conn.commit()

            return {
                "status": "success",
                "asset_id": asset_id,
                "sale_proceeds": proceeds,
                "gain_loss": gain_loss,
                "journal_entry_id": entry_id
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 14. LOAN ACCOUNTING (SPLIT PRINCIPAL & INTEREST INSTALLMENT REPAYMENT)
    # -------------------------------------------------------------------------
    @classmethod
    def post_loan_repayment_installment(cls, rep_data, conn=None):
        """
        Splits Loan Installment into Principal Repayment and Interest Expense:
          Dr Loan Liability (2110 / 2120) [Principal Part]
          Dr Loan Interest Expense (7020) [Interest Part]
          Cr Bank / Cash (1020 / 1010) [Total Repaid]
        Atomically updates accounts_liabilities outstanding balance and registers installment in accounts_liability_payments.
        """
        d = rep_data
        lia_id = d.get("liability_id")
        p_val = max(0.0, round(float(d.get("principal_part") or d.get("principal_amount") or 0.0), 2))
        i_val = max(0.0, round(float(d.get("interest_part") or d.get("interest_amount") or 0.0), 2))
        total_repaid = round(p_val + i_val, 2)
        p_mode = str(d.get("payment_method") or "Bank Transfer").strip()
        date_str = str(d.get("payment_date") or d.get("date") or datetime.date.today().isoformat()).split("T")[0]
        ref = d.get("reference_no") or f"LOAN-INST-{int(datetime.datetime.now().timestamp())}"
        notes = d.get("notes") or "Loan installment repayment"
        user = d.get("user") or "admin"

        if total_repaid <= 0:
            raise ValueError("Total installment amount (Principal + Interest) must be greater than zero.")

        should_close = False
        if conn is None:
            conn = get_db_connection()
            should_close = True
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM accounts_liabilities WHERE id = %s;", (lia_id,))
            loan = cur.fetchone()
            if not loan:
                raise ValueError(f"Loan record #{lia_id} not found.")

            target_code = "2110" if "bank" in str(loan.get("liability_type") or "").lower() else "2120"
            loan_acc = ChartOfAccountsEngine.get_account_by_code(target_code, conn=conn) or cls.get_mapped_account("bank_loan", default_code="2110", conn=conn)
            interest_acc = ChartOfAccountsEngine.get_account_by_code("7020", conn=conn) or cls.get_mapped_account("loan_interest", default_code="7020", conn=conn)
            liquid_acc = cls.resolve_liquid_account(p_mode, conn=conn)

            lines = []
            if p_val > 0:
                lines.append({
                    "account_id": loan_acc["id"],
                    "debit": p_val,
                    "credit": 0.0,
                    "description": f"Principal Repayment: {loan['title']} ({loan['liability_code']})"
                })
            if i_val > 0:
                lines.append({
                    "account_id": interest_acc["id"],
                    "debit": i_val,
                    "credit": 0.0,
                    "description": f"Loan Interest Charge: {loan['title']}"
                })
            lines.append({
                "account_id": liquid_acc["id"],
                "debit": 0.0,
                "credit": total_repaid,
                "description": f"Installment disbursement via {p_mode}"
            })

            entry = DoubleEntryEngine.post_journal_entry(
                entry_data={
                    "entry_date": date_str,
                    "source_module": "liability",
                    "source_entity": "loan_installment",
                    "source_id": str(lia_id),
                    "reference_no": ref,
                    "narration": f"Loan Installment: {loan['title']} (Principal: Rs. {p_val:,.2f}, Interest: Rs. {i_val:,.2f}) via {p_mode}",
                    "status": "POSTED"
                },
                lines_data=lines,
                user=user,
                external_conn=conn
            )
            entry_id = entry["entry_id"]

            new_rem = max(0.0, round(float(loan["outstanding_balance"]) - p_val, 2))
            new_status = "Settled" if new_rem <= 0.01 else "Active"
            cur.execute("""
                UPDATE accounts_liabilities
                SET outstanding_balance = %s, status = %s
                WHERE id = %s;
            """, (new_rem, new_status, lia_id))

            cur.execute("""
                INSERT INTO accounts_liability_payments
                    (liability_id, payment_date, principal_amount, interest_amount, total_amount, payment_method, reference_no, journal_entry_id, notes, created_by)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (lia_id, date_str, p_val, i_val, total_repaid, p_mode, ref, entry_id, notes, user))
            conn.commit()

            return {
                "status": "success",
                "liability_id": lia_id,
                "principal_paid": p_val,
                "interest_paid": i_val,
                "total_paid": total_repaid,
                "remaining_balance": new_rem,
                "loan_status": new_status,
                "journal_entry_id": entry_id
            }
        finally:
            cur.close()
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 15. INVENTORY MOVEMENTS & COGS ADJUSTMENTS
    # -------------------------------------------------------------------------
    @classmethod
    def post_inventory_adjustment(cls, adj_data, conn=None):
        """
        Stock Damage / Wastage / Spoilage / Physical Discrepancy Adjustment:
          Wastage / Damage:
            Dr Other Direct Production Costs / COGS (5060) or Misc Operating Expense (6170)
            Cr Inventory Stock (1050)
          Surplus / Found Stock:
            Dr Inventory Stock (1050)
            Cr Other Operating Income (4030)
        """
        d = adj_data
        qty_change = float(d.get("qty_change") or 0.0)
        unit_cost = float(d.get("unit_cost") or 0.0)
        adj_type = str(d.get("adjustment_type") or ("damage" if qty_change < 0 else "surplus")).lower()
        total_val = round(abs(qty_change) * unit_cost, 2)
        p_code = str(d.get("product_code") or "ITEM").strip()
        reason = d.get("reason") or f"Inventory stock adjustment ({adj_type})"
        date_str = str(d.get("date") or datetime.date.today().isoformat()).split("T")[0]
        user = d.get("user") or "admin"

        if total_val <= 0:
            raise ValueError("Adjustment total value must be greater than zero.")

        inv_acc = ChartOfAccountsEngine.get_account_by_code("1050", conn=conn) or cls.get_mapped_account("inventory_raw", default_code="1050", conn=conn)

        if qty_change < 0 or adj_type in ("damage", "spoilage", "writeoff", "shortage"):
            # Stock Reduction
            cogs_loss_acc = ChartOfAccountsEngine.get_account_by_code("5060", conn=conn) or cls.get_mapped_account("cogs", default_code="5010", conn=conn)
            lines = [
                {
                    "account_id": cogs_loss_acc["id"],
                    "debit": total_val,
                    "credit": 0.0,
                    "description": f"Stock Loss / Spoilage: {p_code} (Qty: {abs(qty_change)})"
                },
                {
                    "account_id": inv_acc["id"],
                    "debit": 0.0,
                    "credit": total_val,
                    "description": f"Inventory write-down: {p_code}"
                }
            ]
        else:
            # Stock Addition (Surplus)
            income_acc = ChartOfAccountsEngine.get_account_by_code("4030", conn=conn) or cls.get_mapped_account("other_income", default_code="4030", conn=conn)
            lines = [
                {
                    "account_id": inv_acc["id"],
                    "debit": total_val,
                    "credit": 0.0,
                    "description": f"Stock Surplus addition: {p_code} (Qty: {qty_change})"
                },
                {
                    "account_id": income_acc["id"],
                    "debit": 0.0,
                    "credit": total_val,
                    "description": f"Inventory adjustment surplus gain: {p_code}"
                }
            ]

        ref = f"ADJ-STK-{p_code}-{int(datetime.datetime.now().timestamp())}"
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "inventory",
                "source_entity": "stock_adjustment",
                "source_id": p_code,
                "reference_no": ref,
                "narration": f"Inventory Adjustment: {p_code} [{adj_type}] - {reason} (Qty: {qty_change}, Value: Rs. {total_val:,.2f})",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    # -------------------------------------------------------------------------
    # 17. CASH & BANK: CONTRA TRANSFERS & BANK CHARGES
    # -------------------------------------------------------------------------
    @classmethod
    def post_contra_transfer(cls, transfer_data, conn=None):
        """
        Bank / Cash Contra Transfer:
          Dr Destination Account (1010 Cash or 1020 Bank)
          Cr Source Account (1020 Bank or 1010 Cash)
        Guarantees ZERO effect on Revenue or Expenses (P&L unaffected).
        """
        d = transfer_data
        amount = round(float(d.get("amount") or 0.0), 2)
        from_mode = str(d.get("from_account") or d.get("source") or "Bank").strip().title()
        to_mode = str(d.get("to_account") or d.get("destination") or "Cash").strip().title()
        ref_no = d.get("reference_no") or f"CONTRA-{int(datetime.datetime.now().timestamp())}"
        date_str = str(d.get("date") or datetime.date.today().isoformat())
        narration = d.get("narration") or f"Transfer from {from_mode} to {to_mode}"
        user = d.get("user") or "admin"

        if amount <= 0:
            raise ValueError("Transfer amount must be greater than zero.")
        if from_mode == to_mode:
            raise ValueError("Source and destination accounts must be different.")

        from_acc = cls.resolve_liquid_account(from_mode, conn=conn)
        to_acc = cls.resolve_liquid_account(to_mode, conn=conn)

        lines = [
            {"account_id": to_acc["id"], "debit": amount, "credit": 0.0, "description": f"Transfer in from {from_mode}"},
            {"account_id": from_acc["id"], "debit": 0.0, "credit": amount, "description": f"Transfer out to {to_mode}"},
        ]
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "banking",
                "source_entity": "contra_transfer",
                "source_id": ref_no,
                "reference_no": ref_no,
                "narration": narration,
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    @classmethod
    def post_bank_charges(cls, charge_data, conn=None):
        """
        Bank Service Charges & Transaction Fees:
          Dr Bank Charges & Processing Fees (7010)
          Cr Bank Operating Account (1020)
        """
        d = charge_data
        amount = round(float(d.get("amount") or 0.0), 2)
        ref_no = d.get("reference_no") or f"BNK-CHG-{int(datetime.datetime.now().timestamp())}"
        date_str = str(d.get("date") or datetime.date.today().isoformat())
        description = d.get("description") or "Monthly bank service charges"
        user = d.get("user") or "admin"

        if amount <= 0:
            raise ValueError("Bank charge amount must be greater than zero.")

        chg_acc = ChartOfAccountsEngine.get_account_by_code("7010", conn=conn) or cls.get_mapped_account("bank_charges", default_code="7010", conn=conn)
        bank_acc = cls.resolve_liquid_account("Bank", conn=conn)

        lines = [
            {"account_id": chg_acc["id"], "debit": amount, "credit": 0.0, "description": description},
            {"account_id": bank_acc["id"], "debit": 0.0, "credit": amount, "description": f"Bank charge debited: {description}"},
        ]
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "banking",
                "source_entity": "bank_charge",
                "source_id": ref_no,
                "reference_no": ref_no,
                "narration": f"Bank Charges: {description}",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    # -------------------------------------------------------------------------
    # 18. STATUTORY TAX: TDS DEDUCTION & REMITTANCE
    # -------------------------------------------------------------------------
    @classmethod
    def post_expense_with_tds(cls, expense_data, conn=None):
        """
        Records an operating expense with TDS deducted at source:
          Dr Expense Account (e.g., 6040 Audit/Legal/Consulting) [Gross Amount]
          Cr TDS Payable (2050) [TDS Amount]
          Cr Bank Operating Account (1020) [Net Amount Paid]
        """
        d = expense_data
        gross = round(float(d.get("gross_amount") or d.get("amount") or 0.0), 2)
        tds_rate = float(d.get("tds_rate") or (10.0 if d.get("tds_amount") is None else 0.0))
        tds_amount = round(float(d.get("tds_amount") or (gross * (tds_rate / 100.0))), 2)
        net_paid = round(gross - tds_amount, 2)
        p_mode = str(d.get("payment_method") or "Bank Transfer").strip()
        date_str = str(d.get("date") or datetime.date.today().isoformat())
        category = d.get("category") or "Professional Fees"
        vendor = d.get("vendor_name") or d.get("payee") or "Consultant"
        section = d.get("tds_section") or "194J"
        user = d.get("user") or "admin"

        if gross <= 0:
            raise ValueError("Gross expense amount must be greater than zero.")
        if tds_amount < 0 or tds_amount >= gross:
            raise ValueError("TDS amount must be non-negative and less than gross expense.")

        exp_acc = cls.get_mapped_account("legal_audit_fees", default_code="6040", conn=conn)
        tds_acc = cls.get_mapped_account("tds_payable", default_code="2050", conn=conn)
        bank_acc = cls.resolve_liquid_account(p_mode, conn=conn)

        lines = [
            {
                "account_id": exp_acc["id"],
                "debit": gross,
                "credit": 0.0,
                "description": f"{category}: {vendor} (Gross under Sec {section})",
                "party_name": vendor
            },
            {
                "account_id": tds_acc["id"],
                "debit": 0.0,
                "credit": tds_amount,
                "description": f"TDS @{tds_rate}% under Sec {section} on {vendor}",
                "tax_code": f"TDS_{section}",
                "tax_rate": tds_rate,
                "party_name": vendor
            },
            {
                "account_id": bank_acc["id"],
                "debit": 0.0,
                "credit": net_paid,
                "description": f"Net payment via {p_mode} after TDS deduction",
                "party_name": vendor
            }
        ]

        ref = d.get("reference_no") or f"TDS-EXP-{int(datetime.datetime.now().timestamp())}"
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "tax",
                "source_entity": "tds_deduction",
                "source_id": ref,
                "reference_no": ref,
                "narration": f"Expense with TDS: {vendor} - {category} (Gross: Rs. {gross:,.2f}, TDS: Rs. {tds_amount:,.2f})",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    @classmethod
    def post_tds_remittance(cls, remittance_data, conn=None):
        """
        Remittance of TDS liability to Government Treasury:
          Dr TDS Payable (2050)
          Cr Bank Operating Account (1020)
        """
        d = remittance_data
        amount = round(float(d.get("amount") or 0.0), 2)
        p_mode = str(d.get("payment_method") or "Bank Transfer").strip()
        challan_no = d.get("challan_no") or f"CHALLAN-{int(datetime.datetime.now().timestamp())}"
        date_str = str(d.get("date") or datetime.date.today().isoformat())
        user = d.get("user") or "admin"

        if amount <= 0:
            raise ValueError("TDS remittance amount must be greater than zero.")

        tds_acc = cls.get_mapped_account("tds_payable", default_code="2050", conn=conn)
        bank_acc = cls.resolve_liquid_account(p_mode, conn=conn)

        lines = [
            {"account_id": tds_acc["id"], "debit": amount, "credit": 0.0, "description": f"TDS Remittance to Govt (Challan: {challan_no})"},
            {"account_id": bank_acc["id"], "debit": 0.0, "credit": amount, "description": f"TDS Payment via {p_mode}"},
        ]
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "tax",
                "source_entity": "tds_payment",
                "source_id": challan_no,
                "reference_no": challan_no,
                "narration": f"TDS Payment to Government: Challan #{challan_no} for Rs. {amount:,.2f}",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    # -------------------------------------------------------------------------
    # 19. EQUITY: CAPITAL INTRODUCTION & OWNER DRAWINGS
    # -------------------------------------------------------------------------
    @classmethod
    def post_capital_introduction(cls, capital_data, conn=None):
        """
        Owner Capital Contribution / Introduction:
          Dr Cash / Bank (Liquid Asset)
          Cr Owner's Capital Account (3010 Equity)
        Increases Owner's Equity without affecting Revenue/P&L.
        """
        d = capital_data
        amount = round(float(d.get("amount") or 0.0), 2)
        p_mode = str(d.get("payment_method") or "Bank Transfer").strip()
        date_str = str(d.get("date") or datetime.date.today().isoformat())
        owner = d.get("owner_name") or "Owner"
        notes = d.get("notes") or "Owner capital contribution"
        user = d.get("user") or "admin"

        if amount <= 0:
            raise ValueError("Capital contribution amount must be greater than zero.")

        cap_acc = ChartOfAccountsEngine.get_account_by_code("3010", conn=conn) or cls.get_mapped_account("owner_capital", default_code="3010", conn=conn)
        liquid_acc = cls.resolve_liquid_account(p_mode, conn=conn)

        lines = [
            {"account_id": liquid_acc["id"], "debit": amount, "credit": 0.0, "description": f"Capital introduced via {p_mode}"},
            {"account_id": cap_acc["id"], "debit": 0.0, "credit": amount, "description": f"Owner Capital Introduced: {owner}"},
        ]
        ref = d.get("reference_no") or f"CAP-{int(datetime.datetime.now().timestamp())}"
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "manual",
                "source_entity": "capital_introduction",
                "source_id": ref,
                "reference_no": ref,
                "narration": f"Capital Contribution by {owner}: Rs. {amount:,.2f} via {p_mode}",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )

    @classmethod
    def post_owner_drawings(cls, drawings_data, conn=None):
        """
        Owner Drawings / Withdrawals:
          Dr Owner's Drawings Account (3040 Equity Contra)
          Cr Cash / Bank (Liquid Asset)
        Reduces Owner's Equity directly; strictly excluded from operating expenses / P&L.
        """
        d = drawings_data
        amount = round(float(d.get("amount") or 0.0), 2)
        p_mode = str(d.get("payment_method") or "Cash").strip()
        date_str = str(d.get("date") or datetime.date.today().isoformat())
        owner = d.get("owner_name") or "Owner"
        notes = d.get("notes") or "Owner personal withdrawal"
        user = d.get("user") or "admin"

        if amount <= 0:
            raise ValueError("Drawings amount must be greater than zero.")

        draw_acc = ChartOfAccountsEngine.get_account_by_code("3040", conn=conn) or cls.get_mapped_account("drawings", default_code="3040", conn=conn)
        liquid_acc = cls.resolve_liquid_account(p_mode, conn=conn)

        lines = [
            {"account_id": draw_acc["id"], "debit": amount, "credit": 0.0, "description": f"Owner Drawings: {owner}"},
            {"account_id": liquid_acc["id"], "debit": 0.0, "credit": amount, "description": f"Disbursement via {p_mode} for drawings"},
        ]
        ref = d.get("reference_no") or f"DRAW-{int(datetime.datetime.now().timestamp())}"
        return DoubleEntryEngine.post_journal_entry(
            entry_data={
                "entry_date": date_str,
                "source_module": "manual",
                "source_entity": "owner_drawings",
                "source_id": ref,
                "reference_no": ref,
                "narration": f"Owner Drawings by {owner}: Rs. {amount:,.2f} via {p_mode}",
                "status": "POSTED"
            },
            lines_data=lines,
            user=user,
            external_conn=conn
        )





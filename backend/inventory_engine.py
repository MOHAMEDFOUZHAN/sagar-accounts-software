"""
Inventory Costing & Movement Engine.
Provides audited, product-level inventory valuation, FIFO batch allocation,
COGS calculation, sales return restoration, and negative inventory detection.
Operates with JAI Agency SQLite DB as the operational source of truth.
"""

import logging
from decimal import Decimal
from typing import Dict, List, Any, Optional, Tuple
from backend.db import get_jai_agency_db_connection

logger = logging.getLogger(__name__)


class NegativeInventoryError(Exception):
    """Raised when a transaction causes negative stock under strict stock enforcement."""
    def __init__(self, product_code: str, product_name: str, requested_qty: float, available_qty: float, bill_id: Any = None):
        self.product_code = product_code
        self.product_name = product_name
        self.requested_qty = requested_qty
        self.available_qty = available_qty
        self.bill_id = bill_id
        self.deficit_qty = round(max(0.0, requested_qty - available_qty), 4)
        msg = (
            f"Negative Inventory Detected for Product '{product_name}' (Code: {product_code}). "
            f"Requested: {requested_qty:g}, Available before sale: {available_qty:g}, Deficit: {self.deficit_qty:g} units. "
            f"Transaction Bill ID: {bill_id}. Posting prevented under strict inventory control rules."
        )
        super().__init__(msg)


class InventoryCostingEngine:
    """
    Computes exact product-level inventory movement and Cost of Goods Sold (COGS).
    Strictly adheres to FIFO (First-In, First-Out) costing based on physical inward batches.
    """

    COSTING_METHOD = "FIFO (Batch Queue)"
    ALLOW_NEGATIVE_INVENTORY = True
    STRICT_STOCK_CHECK = False

    @classmethod
    def get_inventory_and_cogs_breakdown(cls, jai_conn=None) -> Dict[str, Any]:
        """
        Processes all JAI Agency inventory inward batches, sales items, and customer returns
        to compute exact product-by-product movements, actual COGS, and closing stock valuation.
        """
        should_close = False
        if jai_conn is None:
            jai_conn = get_jai_agency_db_connection()
            should_close = True

        if not jai_conn:
            logger.error("Could not connect to Jai Agency DB.")
            return {
                "products": [],
                "bill_cogs": {},
                "return_cogs": {},
                "negative_stock_warnings": [],
                "totals": {
                    "opening_value": 0.0,
                    "purchases": 0.0,
                    "actual_cogs": 0.0,
                    "closing_inventory": 0.0,
                    "is_reconciled": True,
                    "difference": 0.0,
                },
            }

        try:
            cur = jai_conn.cursor()

            # 1. Master catalog
            cur.execute("""
                SELECT code, name, category, price, last_cost, unit, reorder_level
                FROM products
                ORDER BY code ASC;
            """)
            products = {str(r["code"]): dict(r) for r in cur.fetchall()}

            # 2. Inward purchase batches (storage)
            cur.execute("""
                SELECT id, batch_id, product_code, product_name, qty, cost, arrival_date, invoice_no, supplier_id
                FROM storage
                WHERE invoice_no != 'MANUAL-STORAGE'
                  AND invoice_no NOT LIKE 'RET-%'
                  AND invoice_no NOT LIKE 'CANCEL-%'
                ORDER BY arrival_date ASC, id ASC;
            """)
            batches = [dict(r) for r in cur.fetchall()]

            # 3. Active sales bills and line items
            cur.execute("""
                SELECT si.id, si.bill_id, si.product_code, si.product_name, si.qty, si.price,
                       sl.date as sale_date, sl.status
                FROM sale_items si
                JOIN sales_log sl ON si.bill_id = sl.id
                WHERE sl.status = 'ACTIVE'
                ORDER BY sl.date ASC, si.id ASC;
            """)
            sale_items = [dict(r) for r in cur.fetchall()]

            # 4. Customer returns
            cur.execute("""
                SELECT id, bill_id, product_code, product_name, qty, refund_amount, date as return_date
                FROM returns_log
                ORDER BY date ASC, id ASC;
            """)
            returns = [dict(r) for r in cur.fetchall()]

            # Build FIFO batch queues per product
            product_queues: Dict[str, List[Dict[str, Any]]] = {}
            product_purchases: Dict[str, Dict[str, float]] = {}

            for b in batches:
                p_code = str(b["product_code"])
                qty = round(float(b.get("qty") or 0.0), 4)
                cost = round(float(b.get("cost") or 0.0), 2)

                if p_code not in product_queues:
                    product_queues[p_code] = []
                    product_purchases[p_code] = {"qty": 0.0, "cost_total": 0.0}

                product_queues[p_code].append({
                    "batch_id": b.get("batch_id") or f"B-{b['id']}",
                    "invoice_no": b.get("invoice_no"),
                    "arrival_date": b.get("arrival_date"),
                    "unit_cost": cost,
                    "original_qty": qty,
                    "qty_remaining": qty,
                })
                product_purchases[p_code]["qty"] = round(product_purchases[p_code]["qty"] + qty, 4)
                product_purchases[p_code]["cost_total"] = round(product_purchases[p_code]["cost_total"] + (qty * cost), 2)

            # Allocate sales to FIFO batches
            bill_cogs: Dict[int, float] = {}
            bill_item_cogs: Dict[int, List[Dict[str, Any]]] = {}
            product_sales: Dict[str, Dict[str, float]] = {}
            negative_stock_warnings: List[Dict[str, Any]] = []

            for si in sale_items:
                b_id = int(si["bill_id"])
                p_code = str(si["product_code"])
                qty_needed = round(float(si.get("qty") or 0.0), 4)
                p_name = si.get("product_name") or products.get(p_code, {}).get("name") or p_code

                if p_code not in product_sales:
                    product_sales[p_code] = {"qty_sold": 0.0, "cogs_total": 0.0}
                if b_id not in bill_cogs:
                    bill_cogs[b_id] = 0.0
                    bill_item_cogs[b_id] = []

                queue = product_queues.get(p_code, [])
                remaining_to_allocate = qty_needed
                item_cogs = 0.0
                allocated_batches = []

                # Drain FIFO batches
                for batch in queue:
                    if remaining_to_allocate <= 0:
                        break
                    if batch["qty_remaining"] > 0:
                        take_qty = min(batch["qty_remaining"], remaining_to_allocate)
                        batch["qty_remaining"] = round(batch["qty_remaining"] - take_qty, 4)
                        remaining_to_allocate = round(remaining_to_allocate - take_qty, 4)
                        cost_portion = round(take_qty * batch["unit_cost"], 2)
                        item_cogs = round(item_cogs + cost_portion, 2)
                        allocated_batches.append({
                            "batch_id": batch["batch_id"],
                            "qty": take_qty,
                            "unit_cost": batch["unit_cost"],
                            "cost": cost_portion,
                        })

                # If sale exceeds available batches -> Negative Inventory deficit
                if remaining_to_allocate > 0:
                    avail_before = round(qty_needed - remaining_to_allocate, 4)
                    if cls.STRICT_STOCK_CHECK:
                        raise NegativeInventoryError(
                            product_code=p_code,
                            product_name=p_name,
                            requested_qty=qty_needed,
                            available_qty=avail_before,
                            bill_id=b_id,
                        )

                    p_info = products.get(p_code, {})
                    fallback_cost = round(float(p_info.get("last_cost") or 0.0), 2)
                    deficit_cogs = round(remaining_to_allocate * fallback_cost, 2)
                    item_cogs = round(item_cogs + deficit_cogs, 2)

                    negative_stock_warnings.append({
                        "product_code": p_code,
                        "product_name": p_name,
                        "bill_id": b_id,
                        "available_qty": avail_before,
                        "required_qty": qty_needed,
                        "deficit_qty": remaining_to_allocate,
                        "fallback_unit_cost": fallback_cost,
                        "deficit_cogs": deficit_cogs,
                        "action_taken": f"Allowed under business rules (ALLOW_NEGATIVE_INVENTORY=True). Deficit costed using master last_cost (Rs. {fallback_cost:.2f}).",
                        "explanation": (
                            f"Sale of {qty_needed:g} units for '{p_name}' (Bill #{b_id}) exceeded available inward batches. "
                            f"Stock had {avail_before:g} units available; {remaining_to_allocate:g} units deficit was costed "
                            f"at master catalog last_cost (Rs. {fallback_cost:.2f})."
                        ),
                    })

                bill_cogs[b_id] = round(bill_cogs[b_id] + item_cogs, 2)
                bill_item_cogs[b_id].append({
                    "item_id": si["id"],
                    "product_code": p_code,
                    "product_name": p_name,
                    "qty": qty_needed,
                    "cogs": item_cogs,
                    "allocated_batches": allocated_batches,
                })
                product_sales[p_code]["qty_sold"] = round(product_sales[p_code]["qty_sold"] + qty_needed, 4)
                product_sales[p_code]["cogs_total"] = round(product_sales[p_code]["cogs_total"] + item_cogs, 2)

            # Process customer returns (restore to inventory queue & reverse COGS)
            return_cogs: Dict[int, float] = {}
            product_returns: Dict[str, Dict[str, float]] = {}

            for ret in returns:
                r_id = int(ret["id"])
                p_code = str(ret["product_code"])
                qty_ret = round(float(ret.get("qty") or 0.0), 4)
                p_name = ret.get("product_name") or products.get(p_code, {}).get("name") or p_code

                if p_code not in product_returns:
                    product_returns[p_code] = {"qty_ret": 0.0, "cogs_reversed": 0.0}

                # Determine unit cost for returned item (from earliest batch or product last_cost)
                queue = product_queues.get(p_code, [])
                if queue:
                    unit_cost = queue[0]["unit_cost"]
                    queue[0]["qty_remaining"] = round(queue[0]["qty_remaining"] + qty_ret, 4)
                else:
                    unit_cost = round(float(products.get(p_code, {}).get("last_cost") or 0.0), 2)

                cogs_rev = round(qty_ret * unit_cost, 2)
                return_cogs[r_id] = cogs_rev
                product_returns[p_code]["qty_ret"] = round(product_returns[p_code]["qty_ret"] + qty_ret, 4)
                product_returns[p_code]["cogs_reversed"] = round(product_returns[p_code]["cogs_reversed"] + cogs_rev, 2)

            # Build product-by-product reconciliation table
            product_reports = []
            grand_purchases = 0.0
            grand_actual_cogs = 0.0
            grand_closing_inventory = 0.0

            for code, p in products.items():
                purch = product_purchases.get(code, {"qty": 0.0, "cost_total": 0.0})
                sales = product_sales.get(code, {"qty_sold": 0.0, "cogs_total": 0.0})
                rets = product_returns.get(code, {"qty_ret": 0.0, "cogs_reversed": 0.0})

                purch_qty = purch["qty"]
                purch_cost = purch["cost_total"]
                sold_qty = sales["qty_sold"]
                ret_qty = rets["qty_ret"]
                net_sold_qty = round(sold_qty - ret_qty, 4)
                actual_cogs = round(sales["cogs_total"] - rets["cogs_reversed"], 2)
                closing_qty = round(purch_qty - net_sold_qty, 4)

                queue = product_queues.get(code, [])
                if closing_qty >= 0:
                    closing_val = round(sum(b["qty_remaining"] * b["unit_cost"] for b in queue), 2)
                else:
                    closing_val = round(closing_qty * float(p.get("last_cost") or 0.0), 2)

                unit_cost = float(p.get("last_cost") or (queue[0]["unit_cost"] if queue else 0.0))

                grand_purchases = round(grand_purchases + purch_cost, 2)
                grand_actual_cogs = round(grand_actual_cogs + actual_cogs, 2)
                grand_closing_inventory = round(grand_closing_inventory + closing_val, 2)

                is_negative = (closing_qty < 0)

                product_reports.append({
                    "product_id": code,
                    "product_code": code,
                    "product_name": p["name"],
                    "category": p.get("category") or "General",
                    "opening_qty": 0.0,
                    "opening_value": 0.0,
                    "purchased_qty": purch_qty,
                    "purchase_cost": purch_cost,
                    "sold_qty": sold_qty,
                    "returned_qty": ret_qty,
                    "net_sold_qty": net_sold_qty,
                    "adjustments_qty": 0.0,
                    "closing_qty": closing_qty,
                    "closing_value": closing_val,
                    "unit_cost": unit_cost,
                    "costing_method": cls.COSTING_METHOD,
                    "actual_cogs": actual_cogs,
                    "is_negative": is_negative,
                })

            diff = round(abs(grand_purchases - (grand_actual_cogs + grand_closing_inventory)), 2)
            is_reconciled = (diff < 0.05)

            return {
                "products": product_reports,
                "bill_cogs": bill_cogs,
                "bill_item_cogs": bill_item_cogs,
                "return_cogs": return_cogs,
                "negative_stock_warnings": negative_stock_warnings,
                "totals": {
                    "opening_value": 0.0,
                    "purchases": grand_purchases,
                    "actual_cogs": grand_actual_cogs,
                    "closing_inventory": grand_closing_inventory,
                    "is_reconciled": is_reconciled,
                    "difference": diff,
                },
            }

        finally:
            if should_close and jai_conn:
                jai_conn.close()

    @classmethod
    def get_bill_cogs(cls, bill_id: int, jai_conn=None) -> Tuple[float, List[Dict[str, Any]]]:
        """Returns the total COGS and line item allocations for a specific sales bill."""
        res = cls.get_inventory_and_cogs_breakdown(jai_conn=jai_conn)
        b_id = int(bill_id)
        cogs = res["bill_cogs"].get(b_id, 0.0)
        items = res["bill_item_cogs"].get(b_id, [])
        return round(float(cogs), 2), items

    @classmethod
    def get_return_cogs(cls, return_id: int, jai_conn=None) -> float:
        """Returns the COGS reversal amount for a specific customer return."""
        res = cls.get_inventory_and_cogs_breakdown(jai_conn=jai_conn)
        r_id = int(return_id)
        return round(float(res["return_cogs"].get(r_id, 0.0)), 2)

    @classmethod
    def get_live_closing_valuation(cls, jai_conn=None) -> float:
        """Returns the true closing inventory valuation from unconsumed batches in storage."""
        res = cls.get_inventory_and_cogs_breakdown(jai_conn=jai_conn)
        return round(float(res["totals"]["closing_inventory"]), 2)

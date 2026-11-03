"""Business validation and routing.

Validation answers "is this document *right*?" (independent of whether extraction was right):
arithmetic, dates, and matching against ERP master data. For supplier invoices and carrier bills
of lading that is a three-way match against the purchase order; for inbound customer POs it is a
check against the catalogue, list prices and case packs.

Routing then decides: auto-post, or send to the human review queue with the reasons attached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .extract import Extraction
from .schemas import REQUIRED_FIELDS

PRICE_TOLERANCE = 0.02  # 2% unit price variance before a human looks at it
MONEY_TOLERANCE = 0.011


@dataclass
class MasterData:
    catalog: dict[str, dict]  # sku -> {description, price, pack, weight_kg}
    erp_pos: dict[str, dict]  # po_number -> {supplier, currency, lines: {sku: [qty, price]}}
    seen_documents: set[tuple[str, str, str]] = field(default_factory=set)  # (doc_type, sender, number)

    @classmethod
    def from_world_json(cls, data: dict) -> MasterData:
        return cls(catalog=data["catalog"], erp_pos=data["erp_pos"])


@dataclass
class Finding:
    code: str
    message: str
    severity: str = "error"  # error -> must review; warn -> informational


def validate(ex: Extraction, md: MasterData) -> list[Finding]:
    d = ex.doc
    out: list[Finding] = []
    for fld in REQUIRED_FIELDS[d.doc_type]:
        if getattr(d, fld) in (None, ""):
            out.append(Finding("missing_field", f"required field {fld} not found"))
    if not d.lines:
        out.append(Finding("no_lines", "no line items extracted"))

    if d.doc_type in ("invoice", "purchase_order"):
        for i, li in enumerate(d.lines, 1):
            if (
                li.unit_price is not None
                and li.amount is not None
                and abs(li.qty * li.unit_price - li.amount) > MONEY_TOLERANCE + 0.0005 * abs(li.amount)
            ):
                out.append(Finding("line_math", f"line {i}: {li.qty} x {li.unit_price} != {li.amount}"))
        line_sum = round(sum(li.amount or 0 for li in d.lines), 2)
        if d.doc_type == "invoice":
            if d.subtotal is not None and abs(line_sum - d.subtotal) > MONEY_TOLERANCE:
                out.append(Finding("total_mismatch", f"lines sum to {line_sum}, subtotal says {d.subtotal}"))
            if (
                None not in (d.subtotal, d.tax, d.total)
                and abs(d.subtotal + d.tax - d.total) > MONEY_TOLERANCE
            ):
                out.append(
                    Finding("total_mismatch", f"subtotal {d.subtotal} + tax {d.tax} != total {d.total}")
                )
        elif d.total is not None and abs(line_sum - d.total) > MONEY_TOLERANCE:
            out.append(Finding("total_mismatch", f"lines sum to {line_sum}, total says {d.total}"))

    if d.doc_type == "invoice" and d.doc_date and d.due_date and d.due_date < d.doc_date:
        out.append(Finding("due_before_issue", f"due {d.due_date} is before invoice date {d.doc_date}"))

    if d.doc_number and d.sender:
        key = (d.doc_type, d.sender, d.doc_number)
        if d.doc_type == "invoice" and key in md.seen_documents:
            out.append(Finding("duplicate_invoice", f"{d.sender} invoice {d.doc_number} already received"))

    if d.doc_type in ("invoice", "bill_of_lading"):
        out += _three_way(d, md)
    else:
        out += _sales_order_checks(d, md)
    return out


def _three_way(d, md: MasterData) -> list[Finding]:
    out: list[Finding] = []
    if not d.po_number:
        return out
    po = md.erp_pos.get(d.po_number)
    if po is None:
        return [Finding("unknown_po", f"PO {d.po_number} not found in ERP")]
    if d.doc_type == "invoice" and d.sender and po["supplier"] != d.sender:
        out.append(Finding("supplier_mismatch", f"PO {d.po_number} was issued to {po['supplier']}"))
    for i, li in enumerate(d.lines, 1):
        ordered = po["lines"].get(li.sku)
        if ordered is None:
            out.append(Finding("sku_not_on_po", f"line {i}: {li.sku} not on PO {d.po_number}"))
            continue
        qty, price = ordered
        if li.qty > qty + 1e-9:
            out.append(Finding("qty_exceeds_po", f"line {i}: {li.sku} qty {li.qty} > ordered {qty}"))
        if (
            d.doc_type == "invoice"
            and li.unit_price is not None
            and abs(li.unit_price - price) > PRICE_TOLERANCE * price + 0.005
        ):
            out.append(Finding("price_variance", f"line {i}: {li.sku} price {li.unit_price} vs PO {price}"))
        if d.doc_type == "bill_of_lading" and li.cartons is not None:
            pack = md.catalog.get(li.sku, {}).get("pack")
            if pack and li.cartons != math.ceil(li.qty / pack):
                out.append(
                    Finding(
                        "carton_mismatch", f"line {i}: {li.cartons} cartons for {li.qty} units at {pack}/case"
                    )
                )
    return out


def _sales_order_checks(d, md: MasterData) -> list[Finding]:
    out: list[Finding] = []
    for i, li in enumerate(d.lines, 1):
        item = md.catalog.get(li.sku)
        if item is None:
            out.append(Finding("unknown_sku", f"line {i}: SKU {li.sku} not in catalogue"))
            continue
        if (
            li.unit_price is not None
            and abs(li.unit_price - item["price"]) > PRICE_TOLERANCE * item["price"] + 0.005
        ):
            out.append(
                Finding(
                    "price_mismatch_vs_list", f"line {i}: {li.sku} at {li.unit_price}, list {item['price']}"
                )
            )
        if item["pack"] > 1 and li.qty % item["pack"]:
            out.append(
                Finding(
                    "qty_not_case_multiple", f"line {i}: {li.qty} not a multiple of case pack {item['pack']}"
                )
            )
    return out


@dataclass
class Decision:
    route: str  # "auto_approve" | "review"
    reasons: list[str]
    min_confidence: float


def route(ex: Extraction, findings: list[Finding], threshold: float = 0.8) -> Decision:
    required = REQUIRED_FIELDS[ex.doc.doc_type]
    confs = (
        [ex.confidence.get(f, 0.0) for f in required]
        + [ex.confidence.get("doc_type", 0.0)]
        + list(ex.line_confidence)
    )
    min_c = min(confs) if confs else 0.0
    reasons = [f.code for f in findings if f.severity == "error"]
    low = [f for f in required if ex.confidence.get(f, 0.0) < threshold]
    if low:
        reasons.append("low_confidence:" + ",".join(low))
    if any(c < threshold for c in ex.line_confidence):
        reasons.append("low_confidence:lines")
    if ex.confidence.get("doc_type", 0) < 0.6:
        reasons.append("low_confidence:doc_type")
    if ex.warnings:
        reasons += ["warning:" + w for w in ex.warnings]
    return Decision("review" if reasons else "auto_approve", sorted(set(reasons)), min_c)

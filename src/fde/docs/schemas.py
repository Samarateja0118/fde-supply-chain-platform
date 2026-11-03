"""Canonical document schemas. Extraction output, ground truth and the review UI all use these."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

DocType = Literal["invoice", "bill_of_lading", "purchase_order"]


class LineItem(BaseModel):
    sku: str
    description: str | None = None
    qty: float
    unit_price: float | None = None  # absent on bills of lading
    amount: float | None = None
    cartons: int | None = None  # bills of lading only


class Document(BaseModel):
    doc_type: DocType
    sender: str | None = None
    doc_number: str | None = None  # invoice no. / BOL no. / PO no.
    doc_date: date | None = None
    due_date: date | None = None  # invoices
    po_number: str | None = None  # the purchase order this invoice / BOL refers to (PO: same as doc_number)
    currency: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    total_weight_kg: float | None = None  # BOL
    carrier: str | None = None  # BOL
    lines: list[LineItem] = Field(default_factory=list)


HEADER_FIELDS: dict[str, tuple[str, ...]] = {
    "invoice": (
        "sender",
        "doc_number",
        "doc_date",
        "due_date",
        "po_number",
        "currency",
        "subtotal",
        "tax",
        "total",
    ),
    "bill_of_lading": ("sender", "doc_number", "doc_date", "po_number", "carrier", "total_weight_kg"),
    "purchase_order": ("sender", "doc_number", "doc_date", "currency", "total"),
}

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "invoice": ("doc_number", "doc_date", "po_number", "total"),
    "bill_of_lading": ("doc_number", "doc_date", "po_number"),
    "purchase_order": ("doc_number", "doc_date"),
}

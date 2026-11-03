"""Synthetic but realistic inbound documents with ground truth.

Real customers will not hand over their AP inbox for a portfolio project, so documents are generated
from the real Online Retail catalogue (SKUs, descriptions, prices, case packs) and rendered as text
PDFs (reportlab) or plain-text email bodies in several sender-specific layouts: different labels and
languages, number locales (1,234.56 vs 1.234,56), date formats, column orders and header layouts.

A share of documents carries a deliberate business problem (price variance, unknown PO, totals that
do not add up, ...). Those are recorded in ``truth["issues"]`` so the validator's recall can be scored.

Layouts live in ``styles.py`` (development set, used while writing the extractor) and
``holdout_styles.py`` (written after the extractor was frozen; never used for tuning).
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .styles import Style

# --------------------------------------------------------------------------- master data


@dataclass
class CatalogItem:
    sku: str
    description: str
    price: float
    pack: int
    weight_kg: float


@dataclass
class ErpPO:
    """A purchase order our customer issued to a supplier (lives in the ERP; invoices/BOLs match it)."""

    po_number: str
    supplier: str
    currency: str
    lines: dict[str, tuple[float, float]]  # sku -> (qty ordered, unit price)


@dataclass
class World:
    catalog: dict[str, CatalogItem]
    erp_pos: dict[str, ErpPO] = field(default_factory=dict)
    issued_invoices: set[tuple[str, str]] = field(default_factory=set)

    def to_json(self) -> dict:
        return {
            "catalog": {k: vars(v) for k, v in self.catalog.items()},
            "erp_pos": {
                k: {
                    "po_number": v.po_number,
                    "supplier": v.supplier,
                    "currency": v.currency,
                    "lines": {s: list(q) for s, q in v.lines.items()},
                }
                for k, v in self.erp_pos.items()
            },
        }


def catalog_from_sales(clean, n: int = 400, seed: int = 3) -> dict[str, CatalogItem]:
    """Build the catalogue from the real order history (top SKUs by line count)."""
    rng = random.Random(seed)
    top = clean["sku"].value_counts().index[:n]
    sub = clean[clean["sku"].isin(top)]
    desc = sub.groupby("sku")["description"].agg(
        lambda s: s.dropna().mode().iloc[0] if s.notna().any() else "ITEM"
    )
    price = sub.groupby("sku")["unit_price"].median()
    pack = sub.groupby("sku")["qty"].agg(lambda q: int(q.value_counts().idxmax()))
    out = {}
    for sku in top:
        out[sku] = CatalogItem(
            sku,
            str(desc[sku]).strip().title(),
            round(float(price[sku]), 2),
            max(1, min(48, int(pack[sku]))),
            round(rng.uniform(0.05, 1.5), 2),
        )
    return out


# --------------------------------------------------------------------------- formatting helpers


def fmt_number(x: float, locale: str, decimals: int = 2) -> str:
    s = f"{x:,.{decimals}f}"
    if locale == "eu":
        s = s.replace(",", "_").replace(".", ",").replace("_", ".")
    elif locale == "ch":
        s = s.replace(",", "'")
    return s


def fmt_money(x: float, style: Style) -> str:
    return style.money_fmt.format(
        n=fmt_number(x, style.number_locale), sym=style.currency_symbol, code=style.currency
    )


def fmt_qty(x: float, style: Style) -> str:
    return fmt_number(x, style.number_locale, 0) if x >= 1000 else str(int(x))


def ocr_noise(text: str, rng: random.Random, rate: float) -> str:
    """Character confusions typical of a scanned page's OCR text layer."""
    swaps = {"0": "O", "O": "0", "1": "l", "l": "1", "5": "S", "S": "5", "8": "B", ",": ".", "I": "1"}
    out = []
    for ch in text:
        if ch in swaps and rng.random() < rate:
            out.append(swaps[ch])
        else:
            out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------- document content


def _pick_lines(world: World, rng: random.Random, k: int) -> list[CatalogItem]:
    return rng.sample(list(world.catalog.values()), k)


def make_purchase_order(world: World, style: Style, rng: random.Random, n: int, issue: str | None) -> dict:
    """Inbound PO from a retail buyer (sales-order intake)."""
    items = _pick_lines(world, rng, rng.randint(2, 9))
    lines = []
    for it in items:
        qty = it.pack * rng.randint(1, 12)
        price = it.price
        lines.append(
            {
                "sku": it.sku,
                "description": it.description,
                "qty": qty,
                "unit_price": price,
                "amount": round(qty * price, 2),
            }
        )
    issues = []
    if issue == "unknown_sku":
        lines[0]["sku"] = f"{rng.randint(10000, 99999)}Z"
        issues.append("unknown_sku")
    elif issue == "price_mismatch_vs_list":
        lines[0]["unit_price"] = round(lines[0]["unit_price"] * rng.choice([0.8, 0.85, 1.2]), 2)
        lines[0]["amount"] = round(lines[0]["qty"] * lines[0]["unit_price"], 2)
        issues.append("price_mismatch_vs_list")
    elif issue == "qty_not_case_multiple":
        it = world.catalog[lines[0]["sku"]]
        if it.pack > 1:
            lines[0]["qty"] += 1
            lines[0]["amount"] = round(lines[0]["qty"] * lines[0]["unit_price"], 2)
            issues.append("qty_not_case_multiple")
    total = round(sum(li["amount"] for li in lines), 2)
    if issue == "total_mismatch":
        total = round(total + rng.choice([10, 100, 0.9]) * rng.choice([1, -1]), 2)
        issues.append("total_mismatch")
    d = date(2026, 1, 5) + timedelta(days=rng.randint(0, 300))
    return {
        "doc_type": "purchase_order",
        "sender": style.sender,
        "doc_number": style.number_pattern.format(n=n, r=rng.randint(100, 999)),
        "doc_date": d.isoformat(),
        "due_date": None,
        "po_number": None,
        "currency": style.currency,
        "subtotal": None,
        "tax": None,
        "total": total,
        "total_weight_kg": None,
        "carrier": None,
        "lines": lines,
        "issues": issues,
    }


def _new_erp_po(world: World, rng: random.Random, supplier: str, currency: str) -> ErpPO:
    items = _pick_lines(world, rng, rng.randint(2, 8))
    po = ErpPO(
        po_number=f"PO-{rng.randint(400000, 499999)}",
        supplier=supplier,
        currency=currency,
        lines={
            it.sku: (float(it.pack * rng.randint(2, 20)), round(it.price * 0.55, 2)) for it in items
        },  # cost ~55% of list
    )
    world.erp_pos[po.po_number] = po
    return po


def make_invoice(world: World, style: Style, rng: random.Random, n: int, issue: str | None) -> dict:
    po = _new_erp_po(world, rng, style.sender, style.currency)
    lines = []
    for sku, (qty, price) in po.lines.items():
        it = world.catalog[sku]
        lines.append(
            {
                "sku": sku,
                "description": it.description,
                "qty": qty,
                "unit_price": price,
                "amount": round(qty * price, 2),
            }
        )
    issues = []
    po_number = po.po_number
    if issue == "unknown_po":
        po_number = f"PO-{rng.randint(500000, 599999)}"
        issues.append("unknown_po")
    elif issue == "price_variance":
        lines[0]["unit_price"] = round(lines[0]["unit_price"] * rng.choice([1.06, 1.1, 1.25]), 2)
        lines[0]["amount"] = round(lines[0]["qty"] * lines[0]["unit_price"], 2)
        issues.append("price_variance")
    elif issue == "qty_exceeds_po":
        lines[0]["qty"] += world.catalog[lines[0]["sku"]].pack * rng.randint(1, 3)
        lines[0]["amount"] = round(lines[0]["qty"] * lines[0]["unit_price"], 2)
        issues.append("qty_exceeds_po")
    subtotal = round(sum(li["amount"] for li in lines), 2)
    tax = round(subtotal * style.tax_rate, 2)
    total = round(subtotal + tax, 2)
    if issue == "total_mismatch":
        total = round(total + rng.choice([1, 10, 100]), 2)
        issues.append("total_mismatch")
    d = date(2026, 1, 5) + timedelta(days=rng.randint(0, 300))
    due = d + timedelta(days=style.payment_days)
    if issue == "due_before_issue":
        due = d - timedelta(days=rng.randint(1, 20))
        issues.append("due_before_issue")
    number = style.number_pattern.format(n=n, r=rng.randint(100, 999))
    if issue == "duplicate_invoice" and world.issued_invoices:
        prev = sorted(x for x in world.issued_invoices if x[0] == style.sender)
        if prev:
            number = rng.choice(prev)[1]
            issues.append("duplicate_invoice")
    world.issued_invoices.add((style.sender, number))
    return {
        "doc_type": "invoice",
        "sender": style.sender,
        "doc_number": number,
        "doc_date": d.isoformat(),
        "due_date": due.isoformat(),
        "po_number": po_number,
        "currency": style.currency,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
        "total_weight_kg": None,
        "carrier": None,
        "lines": lines,
        "issues": issues,
    }


def make_bol(world: World, style: Style, rng: random.Random, n: int, issue: str | None) -> dict:
    supplier = rng.choice(["Brightwell Supply Co.", "Kessler Haushaltswaren GmbH", "Pacific Rim Trading LLC"])
    po = _new_erp_po(world, rng, supplier, "GBP")
    lines = []
    weight = 0.0
    for sku, (qty, _) in po.lines.items():
        it = world.catalog[sku]
        cartons = math.ceil(qty / it.pack)
        weight += qty * it.weight_kg
        lines.append(
            {
                "sku": sku,
                "description": it.description,
                "qty": qty,
                "unit_price": None,
                "amount": None,
                "cartons": cartons,
            }
        )
    issues = []
    po_number = po.po_number
    if issue == "unknown_po":
        po_number = f"PO-{rng.randint(500000, 599999)}"
        issues.append("unknown_po")
    elif issue == "qty_exceeds_po":
        lines[0]["qty"] += world.catalog[lines[0]["sku"]].pack * rng.randint(1, 4)
        lines[0]["cartons"] = math.ceil(lines[0]["qty"] / world.catalog[lines[0]["sku"]].pack)
        issues.append("qty_exceeds_po")
    elif issue == "carton_mismatch":
        lines[0]["cartons"] += rng.randint(2, 5)
        issues.append("carton_mismatch")
    d = date(2026, 1, 5) + timedelta(days=rng.randint(0, 300))
    return {
        "doc_type": "bill_of_lading",
        "sender": style.sender,
        "doc_number": style.number_pattern.format(n=n, r=rng.randint(100, 999)),
        "doc_date": d.isoformat(),
        "due_date": None,
        "po_number": po_number,
        "currency": None,
        "subtotal": None,
        "tax": None,
        "total": None,
        "total_weight_kg": round(weight, 1),
        "carrier": style.sender,
        "lines": lines,
        "issues": issues,
    }


MAKERS = {"invoice": make_invoice, "bill_of_lading": make_bol, "purchase_order": make_purchase_order}
ISSUES = {
    "invoice": [
        "unknown_po",
        "price_variance",
        "qty_exceeds_po",
        "total_mismatch",
        "due_before_issue",
        "duplicate_invoice",
    ],
    "bill_of_lading": ["unknown_po", "qty_exceeds_po", "carton_mismatch"],
    "purchase_order": ["unknown_sku", "price_mismatch_vs_list", "qty_not_case_multiple", "total_mismatch"],
}


# --------------------------------------------------------------------------- generation


def generate(
    world: World, styles: list[Style], n_docs: int, out_dir: Path, seed: int, issue_rate: float = 0.3
) -> list[dict]:
    from .render import render  # local import keeps reportlab out of the extractor's import path

    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i in range(n_docs):
        style = styles[i % len(styles)]
        issue = rng.choice(ISSUES[style.doc_type]) if rng.random() < issue_rate else None
        truth = MAKERS[style.doc_type](world, style, rng, 10000 + i, issue)
        fmt = "txt" if rng.random() < style.email_share else "pdf"
        path = out_dir / f"{i:04d}_{style.key}.{fmt}"
        render(truth, style, path, rng)
        (out_dir / f"{i:04d}_{style.key}.truth.json").write_text(json.dumps(truth, indent=1))
        manifest.append(
            {
                "file": path.name,
                "style": style.key,
                "format": fmt,
                "doc_type": style.doc_type,
                "issues": truth["issues"],
            }
        )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest

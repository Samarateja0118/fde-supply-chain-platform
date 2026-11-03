"""Render a ground-truth document dict in a sender's style, to a text PDF or a plain-text email."""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

from .generator import fmt_money, fmt_number, fmt_qty, ocr_noise
from .styles import Style

CUSTOMER = ["Harbor & Pine Home Goods Ltd", "Dock Road Distribution Centre", "Northampton NN4 7BW"]
WIDTH = {"pos": 5, "sku": 13, "description": 34, "qty": 9, "cartons": 8, "unit_price": 13, "amount": 14}

Row = list[tuple[int, str]]  # (column in characters, text)


def _sku(sku: str, style: Style) -> str:
    if style.sku_fmt == "hyphen" and len(sku) > 5:
        return sku[:5] + "-" + sku[5:]
    if style.sku_fmt == "lower":
        return sku.lower()
    return sku


def _date(iso: str | None, style: Style) -> str:
    return date.fromisoformat(iso).strftime(style.date_fmt) if iso else ""


def _header_values(truth: dict, style: Style) -> list[tuple[str, str]]:
    vals = []
    for fld in ("doc_number", "doc_date", "due_date", "po_number", "total_weight_kg"):
        if fld not in style.labels or truth.get(fld) is None:
            continue
        v = truth[fld]
        if fld in ("doc_date", "due_date"):
            v = _date(v, style)
        elif fld == "total_weight_kg":
            v = fmt_number(v, style.number_locale, 1)
        vals.append((style.labels[fld], str(v)))
    return vals


def layout(truth: dict, style: Style) -> list[Row]:
    rows: list[Row] = [[(0, style.sender.upper()), (58, style.title)]]
    rows += [[(0, a)] for a in style.address]
    rows.append([])

    party_label = {"invoice": "Bill To:", "bill_of_lading": "Consignee:", "purchase_order": "Supplier:"}[
        style.doc_type
    ]
    party = [[(0, party_label)]] + [[(0, c)] for c in CUSTOMER]
    if style.doc_type == "purchase_order":
        party = [[(0, party_label)]] + [[(0, CUSTOMER[0])], [(0, "Deliver to: " + style.address[0])]]

    hv = _header_values(truth, style)
    if style.header_mode == "inline":
        for i in range(0, len(hv), 2):
            row = [(0, f"{hv[i][0]} {hv[i][1]}")]
            if i + 1 < len(hv):
                row.append((44, f"{hv[i + 1][0]} {hv[i + 1][1]}"))
            rows.append(row)
        rows.append([])
        rows += party
    elif style.header_mode == "stacked":
        rows.append([(i * 26, lab) for i, (lab, _) in enumerate(hv)])
        rows.append([(i * 26, val) for i, (_, val) in enumerate(hv)])
        rows.append([])
        rows += party
    else:  # box: party block on the left, label/value pairs on the right of the same rows
        n = max(len(party), len(hv))
        for i in range(n):
            row = list(party[i]) if i < len(party) else []
            if i < len(hv):
                row += [(48, hv[i][0]), (70, hv[i][1])]
            rows.append(row)
    if style.doc_type == "bill_of_lading":
        rows.append(
            [(0, "Carrier: " + style.sender), (44, "Shipper: " + truth.get("shipper", "Supplier warehouse"))]
        )
    rows.append([])

    # line-item table
    x = 0
    pos = {}
    for fld, _ in style.columns:
        pos[fld] = x
        x += WIDTH[fld]
    rows.append([(pos[f], h) for f, h in style.columns])
    for i, li in enumerate(truth["lines"], 1):
        cells = []
        for fld, _ in style.columns:
            if fld == "pos":
                v = str(i)
            elif fld == "qty":
                v = fmt_qty(li["qty"], style)
            elif fld in ("unit_price", "amount"):
                v = fmt_number(li[fld], style.number_locale)
            elif fld == "cartons":
                v = str(li["cartons"])
            elif fld == "description":
                v = li["description"][:32]
            else:
                v = _sku(li[fld], style)
            x = pos[fld]
            if style.right_align_numbers and fld in ("qty", "cartons", "unit_price", "amount"):
                x = pos[fld] + WIDTH[fld] - 2 - len(v)
            cells.append((x, v))
        rows.append(cells)
    rows.append([])

    if style.doc_type == "invoice":
        for fld in ("subtotal", "tax", "total"):
            rows.append([(50, style.labels[fld]), (70, fmt_money(truth[fld], style))])
    elif style.doc_type == "purchase_order":
        rows.append([(50, style.labels["total"]), (70, fmt_money(truth["total"], style))])
    else:
        rows.append([(0, f"Total packages: {sum(li['cartons'] for li in truth['lines'])}")])
    rows.append([])
    footer = (
        style.footer
        or {
            "invoice": [
                f"Payment terms: {style.payment_days} days net. Please quote the invoice number with payment."
            ],
            "bill_of_lading": ["Received in apparent good order and condition. Signature: ____________"],
            "purchase_order": ["Please confirm receipt of this order and advise delivery date."],
        }[style.doc_type]
    )
    rows += [[(0, f)] for f in footer]
    return rows


def to_text(rows: list[Row]) -> str:
    lines = []
    for row in rows:
        s = ""
        for col, text in sorted(row):
            if len(s) < col:
                s += " " * (col - len(s))
            elif s:
                s += " "
            s += text
        lines.append(s.rstrip())
    return "\n".join(lines) + "\n"


def to_pdf(rows: list[Row], path: Path, noise: float, rng: random.Random) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(path.stem)
    y = A4[1] - 50
    for row in rows:
        for col, text in row:
            c.setFont("Helvetica", 9)
            c.drawString(36 + col * 6.2, y, ocr_noise(text, rng, noise) if noise else text)
        y -= 13
    c.save()


def render(truth: dict, style: Style, path: Path, rng: random.Random) -> None:
    rows = layout(truth, style)
    if path.suffix == ".txt":
        greeting = "Hello,\n\nPlease find our document below.\n\n"
        path.write_text(greeting + to_text(rows) + "\nKind regards\n" + style.sender + "\n", encoding="utf-8")
    else:
        to_pdf(rows, path, style.noise_rate, rng)

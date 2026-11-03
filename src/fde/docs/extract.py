"""Rule-based, layout-aware extraction (no LLM; no API key is available in this environment).

Pipeline: read words with x positions (pdfplumber for PDFs, character offsets for text) ->
group words into lines and "chunks" (runs separated by wide gaps) -> classify the document type ->
find header fields via a multilingual label vocabulary in three arrangements (inline "Label: value",
label then value in the next chunk, label above value) -> locate the line-item table by its header
row and assign each row's words to columns by horizontal overlap -> read totals below the table.

Every field carries a confidence that depends on *how* it was found, so the router can send weak
extractions to a person instead of guessing. The ``Extractor`` protocol is the seam where an LLM or
a vendor OCR/IDP service would plug in; the validator and review queue do not care which one ran.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .normalize import (
    date_order_evidence,
    detect_currency,
    fix_ocr_digits,
    looks_like_sku,
    normalize_sku,
    parse_date,
    parse_number,
)
from .schemas import Document, LineItem

CHAR_PT = 6.0  # text documents: one character ~ 6pt, so PDF and text share tolerances
GAP_PT = 6.0  # a horizontal gap wider than this separates chunks


@dataclass
class Word:
    x0: float
    x1: float
    text: str


@dataclass
class Line:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def chunks(self) -> list[Word]:
        out: list[Word] = []
        for w in self.words:
            if out and w.x0 - out[-1].x1 <= GAP_PT:
                out[-1] = Word(out[-1].x0, w.x1, out[-1].text + " " + w.text)
            else:
                out.append(Word(w.x0, w.x1, w.text))
        return out


@dataclass
class Extraction:
    doc: Document
    confidence: dict[str, float] = field(default_factory=dict)  # header field -> confidence
    line_confidence: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    text: str = ""


class Extractor(Protocol):
    def extract(self, path: Path) -> Extraction: ...


# --------------------------------------------------------------------------- reading


def read_lines(path: Path) -> list[Line]:
    if path.suffix.lower() == ".pdf":
        import pdfplumber

        lines: list[Line] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
                rows: dict[int, list[Word]] = {}
                for w in words:
                    rows.setdefault(round(w["top"] / 3), []).append(Word(w["x0"], w["x1"], w["text"]))
                prev = None
                for key in sorted(rows):
                    if prev is not None and key - prev > 6:  # vertical gap -> keep a blank line
                        lines.append(Line([]))
                    lines.append(Line(sorted(rows[key], key=lambda w: w.x0)))
                    prev = key
        return lines
    text = path.read_text(encoding="utf-8", errors="replace")
    return lines_from_text(text)


def lines_from_text(text: str) -> list[Line]:
    out = []
    for raw in text.splitlines():
        words = [
            Word(m.start() * CHAR_PT, m.end() * CHAR_PT, m.group())
            for m in re.finditer(r"\S+", raw.expandtabs(4))
        ]
        out.append(Line(words))
    return out


# --------------------------------------------------------------------------- vocabulary


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if not unicodedata.combining(c)).replace("°", "o").replace("º", "o")


DOC_KEYWORDS = {
    "invoice": [
        "invoice",
        "rechnung",
        "factura",
        "facture",
        "factuur",
        "tax invoice",
        "amount due",
        "vat",
        "mwst",
        "iva",
        "btw",
        "remit",
    ],
    "bill_of_lading": [
        "bill of lading",
        "b/l",
        "vrachtbrief",
        "consignee",
        "carrier",
        "gross weight",
        "packages",
        "ctns",
        "shipment",
        "albaran",
        "cmr",
        "waybill",
    ],
    "purchase_order": [
        "purchase order",
        "bon de commande",
        "pedido de compra",
        "bestellung",
        "order date",
        "deliver to",
        "orden de compra",
        "please confirm receipt",
    ],
}

ORDER_REF = [
    "your po ref",
    "customer po",
    "your po",
    "po ref",
    "po number",
    "po no",
    "po #",
    "p.o. number",
    "p.o. no",
    "p.o. #",
    "purchase order no",
    "purchase order",
    "order reference",
    "order ref",
    "ihre bestellnummer",
    "bestellnummer",
    "bestell-nr",
    "su pedido",
    "pedido",
    "n° pedido",
    "uw bestelling",
    "uw order",
    "votre commande",
    "n° de commande",
    "no de commande",
    "order number",
    "order no",
    "reference commande",
]

HEADER_ALIASES = {
    "doc_number": [
        "invoice no",
        "invoice number",
        "invoice #",
        "inv no",
        "inv #",
        "rechnungsnummer",
        "rechnung nr",
        "rechnungs-nr",
        "factura n",
        "factura no",
        "numero de factura",
        "n° factura",
        "facture n",
        "factuurnummer",
        "b/l no",
        "b/l number",
        "bl no",
        "document number",
        "document no",
        "waybill no",
        "consignment no",
        "albaran n",
        "albaran no",
        "delivery note no",
    ],
    "doc_date": [
        "invoice date",
        "rechnungsdatum",
        "fecha de factura",
        "fecha factura",
        "date de facture",
        "factuurdatum",
        "ship date",
        "date of shipment",
        "shipping date",
        "fecha de envio",
        "fecha",
        "datum",
        "order date",
        "date de commande",
        "tax point",
        "date issued",
        "issue date",
        "date",
    ],
    "due_date": [
        "due date",
        "payment due",
        "fallig am",
        "falligkeit",
        "vencimiento",
        "fecha de vencimiento",
        "echeance",
        "date d'echeance",
        "vervaldatum",
        "pay by",
    ],
    "po_number": ORDER_REF,
    "total_weight_kg": [
        "gross weight (kg)",
        "gross weight",
        "total gross weight",
        "total weight",
        "weight (kg)",
        "peso bruto",
        "bruttogewicht",
        "brutogewicht",
        "poids brut",
    ],
}
TOTAL_ALIASES = {
    "subtotal": [
        "subtotal",
        "sub total",
        "sub-total",
        "zwischensumme",
        "nettobetrag",
        "base imponible",
        "subtotaal",
        "total ht",
        "net total",
        "net amount",
    ],
    "tax": ["vat", "sales tax", "tax", "mwst", "ust", "iva", "btw", "tva"],
    "total": [
        "total due",
        "amount due",
        "gesamtbetrag",
        "total factura",
        "total a pagar",
        "te betalen",
        "total ttc",
        "invoice total",
        "grand total",
        "balance due",
        "order total",
        "total eur",
        "total",
        "importe total",
    ],
}
COLUMN_ALIASES = {
    "pos": ["pos", "pos.", "line", "ln", "#", "no.", "lin."],
    "sku": [
        "item code",
        "sku",
        "artikel-nr.",
        "artikel-nr",
        "artikelnummer",
        "artikel",
        "article",
        "art.",
        "art. no",
        "product code",
        "ref.",
        "ref",
        "réf.",
        "réf",
        "code",
        "item",
        "part no",
        "part number",
        "referencia",
        "ref. articulo",
        "codigo",
        "artikelcode",
    ],
    "description": [
        "description",
        "bezeichnung",
        "désignation",
        "designation",
        "product",
        "goods description",
        "omschrijving",
        "descripción",
        "descripcion",
        "item description",
        "goods",
        "concepto",
        "artículo",
    ],
    "qty": [
        "qty",
        "quantity",
        "menge",
        "units",
        "qté",
        "qte",
        "cantidad",
        "cant.",
        "aantal",
        "quantité",
        "pcs",
        "stück",
        "uds",
        "unidades",
    ],
    "cartons": ["ctns", "cartons", "packages", "colli", "bultos", "pkgs", "cases", "cajas", "pakketten"],
    "unit_price": [
        "unit price",
        "price each",
        "price",
        "einzelpreis",
        "prix unitaire",
        "precio unitario",
        "precio",
        "rate",
        "unit cost",
        "p.u.",
        "prijs",
        "stukprijs",
    ],
    "amount": [
        "line total",
        "total",
        "amount",
        "betrag",
        "montant",
        "importe",
        "ext. price",
        "extended",
        "net amount",
        "gesamt",
        "bedrag",
        "total línea",
        "total linea",
    ],
}
NUMERIC_COLS = {"pos", "qty", "cartons", "unit_price", "amount"}


def classify(lines: list[Line]) -> tuple[str, float]:
    head = fold(" ".join(line.text for line in lines[:8]))
    body = fold(" ".join(line.text for line in lines))
    scores = {}
    for dt, kws in DOC_KEYWORDS.items():
        scores[dt] = sum(3 * head.count(fold(k)) + body.count(fold(k)) for k in kws)
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    margin = (ranked[0] - ranked[1]) / max(ranked[0], 1)
    return best, 0.5 + 0.5 * margin


# --------------------------------------------------------------------------- value parsing


ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/.]*\d[A-Za-z0-9\-/]*|\d[A-Za-z0-9\-/.]*")


def parse_id(text: str) -> str | None:
    text = text.strip(" :#.-\t")
    m = ID_RE.search(text)
    if not m:
        return None
    tok = m.group().rstrip(".")
    pm = re.match(r"([A-Z]{1,4}[-/]?)(.*)$", tok)
    if pm and pm.group(2):
        return pm.group(1) + fix_ocr_digits(pm.group(2))
    return fix_ocr_digits(tok)


def _value(fld: str, text: str, ctx: dict):
    t = text.strip(" :#\t")
    if not t:
        return None
    if fld in ("doc_number", "po_number"):
        return parse_id(t)
    if fld in ("doc_date", "due_date"):
        ev = date_order_evidence(t)
        if ev:
            ctx.setdefault("order_evidence", set()).add(ev)
        d, amb = parse_date(t, ctx.get("prefer", "dmy"))
        if d and amb and not ctx.get("profiled"):
            ctx.setdefault("ambiguous", set()).add(fld)
        return d
    if fld in ("total_weight_kg", "subtotal", "tax", "total"):
        # take the last number-looking run (labels like "VAT @ 20%" contain numbers themselves)
        cands = re.findall(r"-?[\d.,'OolSB]*\d[\d.,'OolSB]*-?", t)
        for c in reversed(cands):
            v = parse_number(c, ctx.get("locale"))
            if v is not None:
                return v
    return None


OCR_CLASS = {
    "o": "[o0]",
    "l": "[l1i]",
    "i": "[i1l]",
    "s": "[s5]",
    "b": "[b8]",
    "e": "[e]",
    "g": "[g9]",
    "z": "[z2]",
}


def _alias_pattern(alias: str) -> re.Pattern:
    """Alias regex tolerant to OCR confusions in the label itself ("T0tal", "lnvoice")."""
    parts = [OCR_CLASS.get(c, re.escape(c)) for c in fold(alias)]
    return re.compile("".join(parts) + r"(?![a-z])")


_PATTERNS: dict[str, re.Pattern] = {}


def _match_alias(chunk: str, aliases: list[str]) -> str | None:
    f = fold(chunk)
    for a in sorted(aliases, key=len, reverse=True):
        pat = _PATTERNS.get(a)
        if pat is None:
            pat = _PATTERNS[a] = _alias_pattern(a)
        if pat.match(f):
            return a
    return None


# --------------------------------------------------------------------------- extractor


class RuleBasedExtractor:
    def __init__(self, known_senders: list[str] | None = None, date_profiles: dict[str, str] | None = None):
        self.known_senders = known_senders or []
        # sender -> "dmy" | "mdy", learned from that sender's unambiguous dates (e.g. 25/03/2026).
        # Lets 05/03/2026 from a known sender go straight through instead of to a reviewer.
        self.date_profiles = date_profiles if date_profiles is not None else {}

    def extract(self, path: Path) -> Extraction:
        return self.extract_lines(read_lines(path))

    def extract_text(self, text: str) -> Extraction:
        return self.extract_lines(lines_from_text(text))

    def extract_lines(self, lines: list[Line]) -> Extraction:
        doc_type, type_conf = classify(lines)
        full = "\n".join(line.text for line in lines)
        currency = detect_currency(full)
        ctx = {"locale": self._locale(full), "prefer": "mdy" if currency == "USD" else "dmy"}
        doc = Document(doc_type=doc_type, currency=currency)
        conf: dict[str, float] = {"doc_type": type_conf, "currency": 0.9 if currency else 0.0}
        warnings: list[str] = []

        doc.sender, conf["sender"] = self._sender(lines)
        if conf["sender"] >= 0.8 and doc.sender in self.date_profiles:
            ctx["prefer"], ctx["profiled"] = self.date_profiles[doc.sender], True

        table_start, table_end, columns = self._find_table(lines)
        header_zone = lines[:table_start] if table_start is not None else lines
        found = self._header_fields(header_zone, doc_type, ctx)
        for fld, (val, c) in found.items():
            setattr(doc, fld, val)
            conf[fld] = c

        # dd/mm vs mm/dd: if ambiguous, pick the reading under which due date follows doc date
        if ctx.get("ambiguous"):
            self._resolve_dates(doc, ctx, conf, warnings)

        ev = ctx.get("order_evidence", set())
        if len(ev) == 1 and conf["sender"] >= 0.8:
            self.date_profiles.setdefault(doc.sender, next(iter(ev)))

        line_conf: list[float] = []
        if columns:
            doc.lines, line_conf = self._parse_rows(lines[table_start + 1 : table_end], columns, ctx)
        else:
            warnings.append("no line-item table header recognised")

        if doc_type in ("invoice", "purchase_order"):
            tail = lines[table_end:] if table_end is not None else lines
            totals = self._totals(tail, ctx)
            if doc_type == "purchase_order":
                # POs carry one figure; "Total HT"/"Net total" style labels land in subtotal.
                pick = totals.get("total") or totals.get("subtotal")
                totals = {"total": pick} if pick else {}
            for fld, (val, c) in totals.items():
                setattr(doc, fld, val)
                conf[fld] = c
        if doc_type == "bill_of_lading":
            doc.carrier = doc.sender
            conf["carrier"] = conf.get("sender", 0.0)
            doc.currency = None
        return Extraction(doc, conf, line_conf, warnings, full)

    # -- pieces

    @staticmethod
    def _locale(text: str) -> str:
        eu = len(re.findall(r"\d\.\d{3},\d{2}\b|\b\d+,\d{2}\b", text))
        en = len(re.findall(r"\d,\d{3}\.\d{2}\b|\b\d+\.\d{2}\b", text))
        return "eu" if eu > en else "en"

    def _sender(self, lines: list[Line]) -> tuple[str | None, float]:
        top = [c.text for line in lines[:12] for c in line.chunks()]
        best, score = None, 0.0
        for name in self.known_senders:
            for chunk in top:
                r = difflib.SequenceMatcher(None, fold(name), fold(chunk)).ratio()
                if r > score:
                    best, score = name, r
        if best and score >= 0.8:
            return best, 0.95 if score > 0.95 else 0.8
        first = next(
            (
                line.chunks()[0].text
                for line in lines
                if line.words and not fold(line.text).startswith(("hello", "hi", "dear", "please"))
            ),
            None,
        )
        return first, 0.4

    def _header_fields(self, lines: list[Line], doc_type: str, ctx: dict) -> dict[str, tuple[object, float]]:
        aliases: list[tuple[str, str]] = []
        for fld, al in HEADER_ALIASES.items():
            target = fld
            if fld == "po_number" and doc_type == "purchase_order":
                target = "doc_number"
            if doc_type != "invoice" and fld == "due_date":
                continue
            if doc_type != "bill_of_lading" and fld == "total_weight_kg":
                continue
            aliases += [(a, target) for a in al]
        if doc_type == "purchase_order":
            aliases = [
                (a, f) for a, f in aliases if not (f == "doc_number" and a in HEADER_ALIASES["doc_number"])
            ]
        aliases.sort(key=lambda x: len(x[0]), reverse=True)

        found: dict[str, tuple[object, float]] = {}
        for i, line in enumerate(lines):
            chunks = line.chunks()
            for j, ch in enumerate(chunks):
                hit = next(((a, f) for a, f in aliases if _match_alias(ch.text, [a])), None)
                if not hit:
                    continue
                alias, fld = hit
                if fld in found:
                    continue
                rest = ch.text[len(alias) :]
                # 1) same chunk: "Invoice No: BW10000"
                v = _value(fld, self._strip_label_tail(rest), ctx)
                if v is not None:
                    found[fld] = (v, 0.95)
                    continue
                # 2) next chunk on the same line: label column + value column
                if j + 1 < len(chunks) and _match_alias(chunks[j + 1].text, [a for a, _ in aliases]) is None:
                    v = _value(fld, chunks[j + 1].text, ctx)
                    if v is not None:
                        found[fld] = (v, 0.9)
                        continue
                # 3) value stacked under the label
                for k in range(i + 1, min(i + 3, len(lines))):
                    below = [c for c in lines[k].chunks() if abs(c.x0 - ch.x0) <= 12]
                    if below:
                        v = _value(fld, below[0].text, ctx)
                        if v is not None:
                            found[fld] = (v, 0.85)
                        break
        # fallback: an order reference that looks like our ERP PO format anywhere in the header
        if doc_type != "purchase_order" and "po_number" not in found:
            m = re.search(r"\bP[O0]-?\s?[\dOl]{6}\b", "\n".join(line.text for line in lines))
            if m:
                found["po_number"] = (parse_id(m.group().replace(" ", "")), 0.6)
        return found

    @staticmethod
    def _strip_label_tail(rest: str) -> str:
        # "(kg): 318.3" -> "318.3"; "Nr.: X" -> "X"
        return re.sub(r"^\s*(\([^)]*\))?\s*(nr\.?|no\.?|n°|#)?\s*[:#.]?\s*", "", rest, flags=re.I)

    def _resolve_dates(self, doc: Document, ctx: dict, conf: dict, warnings: list[str]) -> None:
        if doc.doc_date and doc.due_date:
            from datetime import date as _d

            def swap(d):
                try:
                    return _d(d.year, d.day, d.month)
                except ValueError:
                    return None

            options = [(doc.doc_date, doc.due_date)]
            sd, sdue = swap(doc.doc_date), swap(doc.due_date)
            if sd and sdue:
                options.append((sd, sdue))
            ok = [o for o in options if 0 <= (o[1] - o[0]).days <= 120]
            if len(ok) == 1:
                doc.doc_date, doc.due_date = ok[0]
                return
        for fld in ctx["ambiguous"]:
            conf[fld] = min(conf.get(fld, 1.0), 0.6)
        warnings.append("ambiguous day/month order in " + ", ".join(sorted(ctx["ambiguous"])))

    def _find_table(self, lines: list[Line]):
        for i, line in enumerate(lines):
            cols = self._header_columns(line)
            if len(cols) >= 3 and ("qty" in {c[0] for c in cols}):
                end = i + 1
                while end < len(lines):
                    t = lines[end].text
                    if (
                        not lines[end].words
                        or _match_alias(t, sum(TOTAL_ALIASES.values(), []))
                        or fold(t).startswith("total")
                    ):
                        break
                    end += 1
                return i, end, cols
        return None, None, None

    @staticmethod
    def _header_columns(line: Line) -> list[tuple[str, float, float]]:
        cols: list[tuple[str, float, float]] = []
        used: set[str] = set()
        words = line.words
        i = 0
        while i < len(words):
            matched = False
            for span in (3, 2, 1):
                if i + span > len(words):
                    continue
                if span > 1 and any(words[k + 1].x0 - words[k].x1 > GAP_PT for k in range(i, i + span - 1)):
                    continue
                phrase = fold(" ".join(w.text for w in words[i : i + span])).rstrip(":")
                for col, al in COLUMN_ALIASES.items():
                    if col in used:
                        continue
                    if phrase in {fold(a) for a in al}:
                        cols.append((col, words[i].x0, words[i + span - 1].x1))
                        used.add(col)
                        i += span
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                i += 1
        # A column owns the horizontal band up to the next column's header, so right-aligned values
        # under a short header ("Rate      0.91") are not pulled into the neighbouring column.
        cols.sort(key=lambda c: c[1])
        return [
            (c[0], c[1], (cols[i + 1][1] - 1) if i + 1 < len(cols) else c[2] + 80) for i, c in enumerate(cols)
        ]

    def _parse_rows(self, rows: list[Line], columns, ctx) -> tuple[list[LineItem], list[float]]:
        numeric = [c for c in columns if c[0] in NUMERIC_COLS]
        first_numeric_x = min((c[1] for c in numeric), default=1e9)
        has_sku_col = any(c[0] == "sku" for c in columns)
        sku_col = next((c for c in columns if c[0] == "sku"), None)
        items, confs = [], []
        for row in rows:
            if not row.words:
                continue
            vals: dict[str, tuple[float, float]] = {}  # col -> (value, distance)
            desc, sku, sku_dist, sku_raw = [], None, 1e9, ""
            cands: list[tuple[Word, float]] = []
            for w in row.words:
                num = (
                    parse_number(w.text, ctx["locale"])
                    if re.fullmatch(r"[\d.,'OolSB\-]+", w.text) and re.search(r"\d", w.text)
                    else None
                )
                is_sku = looks_like_sku(w.text) and (num is None or len(re.sub(r"\D", "", w.text)) == 5)
                if is_sku and has_sku_col:
                    d = _dist(w, sku_col)
                    if d < sku_dist:
                        if sku is not None:
                            desc.append(sku_raw)
                        sku, sku_dist, sku_raw = normalize_sku(w.text), d, w.text
                        continue
                if num is not None and w.x1 >= first_numeric_x - GAP_PT:
                    cands.append((w, num))
                    continue
                desc.append(w.text)
            # One value per column: assign the closest (word, column) pairs first; ties go to the
            # word nearest the column's left edge. A second number that lands in an already-taken
            # band then falls through to the nearest free column instead of being dropped.
            pairs = sorted(
                (_dist(w, c), abs(w.x0 - c[1]), i, c[0]) for i, (w, _) in enumerate(cands) for c in numeric
            )
            used_words: set[int] = set()
            for d, _, i, col in pairs:
                if i in used_words or col in vals:
                    continue
                vals[col] = (cands[i][1], d)
                used_words.add(i)
            desc += [cands[i][0].text for i in range(len(cands)) if i not in used_words]
            if sku is None and "qty" not in vals:
                continue
            qty = vals.get("qty", (None,))[0]
            price = vals.get("unit_price", (None,))[0]
            amount = vals.get("amount", (None,))[0]
            cartons = vals.get("cartons", (None,))[0]
            c = 0.9
            expected = {col[0] for col in numeric} - {"pos"}
            if qty is None or sku is None:
                c = 0.4
            elif expected - set(vals):
                c = 0.5  # a column the table promises is empty on this row: never auto-approve
            elif price is not None and amount is not None:
                c = 0.97 if abs(qty * price - amount) <= 0.011 + 0.0005 * amount else 0.55
            items.append(
                LineItem(
                    sku=sku or "",
                    description=" ".join(desc) or None,
                    qty=qty or 0.0,
                    unit_price=price,
                    amount=amount,
                    cartons=int(cartons) if cartons is not None else None,
                )
            )
            confs.append(c)
        return items, confs

    def _totals(self, lines: list[Line], ctx) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for line in lines:
            chunks = line.chunks()
            for j, ch in enumerate(chunks):
                for fld, al in TOTAL_ALIASES.items():
                    if fld in out:
                        continue
                    a = _match_alias(ch.text, al)
                    if not a:
                        continue
                    if fld == "tax" and _match_alias(ch.text, TOTAL_ALIASES["total"]):
                        continue
                    rest = ch.text[len(a) :]
                    rest = re.sub(r"^[^:]*\d+\s?%", "", rest)  # "VAT @ 20%" -> ""
                    v = _value(fld, rest, ctx)
                    c = 0.95
                    if v is None and j + 1 < len(chunks):
                        v, c = _value(fld, chunks[j + 1].text, ctx), 0.9
                    if v is not None:
                        out[fld] = (v, c)
                    break
        return out


def _dist(w: Word, col) -> float:
    _, x0, x1 = col
    if w.x1 < x0:
        return x0 - w.x1
    if w.x0 > x1:
        return w.x0 - x1
    return 0.0

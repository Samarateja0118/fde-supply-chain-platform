"""Value parsers for numbers, money, dates and identifiers in messy multi-locale documents."""

from __future__ import annotations

import re
from datetime import date

OCR_DIGIT = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"})
CURRENCY_TOKENS = {
    "£": "GBP",
    "€": "EUR",
    "$": "USD",
    "GBP": "GBP",
    "EUR": "EUR",
    "USD": "USD",
    "EURO": "EUR",
}

MONTHS = {
    # en
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # de / nl / fr / es (common forms)
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5, "juni": 6, "juli": 7, "oktober": 10, "dezember": 12, "dez": 12, "okt": 10,
    "januari": 1, "februari": 2, "maart": 3, "mei": 5, "augustus": 8,
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}  # fmt: skip


def fix_ocr_digits(token: str) -> str:
    """Undo OCR letter-for-digit confusions, but only in tokens that are mostly digits already."""
    digits = sum(c.isdigit() for c in token)
    letters = sum(c.isalpha() for c in token)
    if digits and digits >= letters:
        return token.translate(OCR_DIGIT)
    return token


def parse_number(text: str, locale_hint: str | None = None) -> float | None:
    """Parse '1,234.56', '1.234,56', '1 234,56', "1'234.56", '318,3' -> float. None if not a number."""
    s = text.strip()
    for sym in ("£", "€", "$", "GBP", "EUR", "USD"):
        s = s.replace(sym, "")
    s = fix_ocr_digits(s.strip().replace(" ", " "))
    s = s.replace(" ", "").replace("'", "")
    neg = s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-()")
    if not s or not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s:
        tail = s.split(",")[-1]
        if s.count(",") > 1:
            dec = ""
        elif len(tail) != 3:
            dec = ","
        else:
            dec = "," if locale_hint == "eu" else ""
    elif "." in s:
        tail = s.split(".")[-1]
        dec = "" if s.count(".") > 1 or (len(tail) == 3 and locale_hint == "eu") else "."
    else:
        dec = ""
    if dec == ",":
        s = s.replace(".", "").replace(",", ".")
    elif dec == ".":
        s = s.replace(",", "")
    else:
        s = s.replace(",", "").replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def detect_currency(text: str) -> str | None:
    counts: dict[str, int] = {}
    for tok, code in CURRENCY_TOKENS.items():
        n = text.count(tok) if not tok.isalpha() else len(re.findall(rf"\b{tok}\b", text))
        if n:
            counts[code] = counts.get(code, 0) + n
    return max(counts, key=counts.get) if counts else None


DATE_NUMERIC = re.compile(r"\b(\d{1,4})([./\-])(\d{1,2})\2(\d{2,4})\b")
DATE_WORDY = re.compile(
    r"\b(\d{1,2})\.?\s+([A-Za-zéûäè]{3,10})\.?,?\s+(\d{4})\b|\b([A-Za-zéûäè]{3,10})\.?\s+(\d{1,2}),?\s+(\d{4})\b"
)


def parse_date(text: str, prefer: str = "dmy") -> tuple[date | None, bool]:
    """Return (date, ambiguous). ``prefer`` resolves 03/04/2026 style ambiguity ('dmy' or 'mdy')."""
    t = fix_ocr_digits(text.strip()) if re.search(r"\d", text) else text
    m = DATE_NUMERIC.search(t)
    if m:
        a, sep, b, c = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            if len(a) == 4:  # ISO-ish yyyy-mm-dd
                return date(int(a), int(b), int(c)), False
            y = int(c) + (2000 if len(c) == 2 else 0)
            x, z = int(a), int(b)
            if x > 12:
                return date(y, z, x), False
            if z > 12:
                return date(y, x, z), False
            if x == z:
                return date(y, x, z), False
            if sep == ".":  # dotted dates are day-first everywhere they are used
                return date(y, z, x), False
            return (date(y, z, x), True) if prefer == "dmy" else (date(y, x, z), True)
        except ValueError:
            return None, False
    m = DATE_WORDY.search(text)
    if m:
        try:
            if m.group(1):
                d, mon, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
            else:
                mon, d, y = m.group(4).lower(), int(m.group(5)), int(m.group(6))
            month = MONTHS.get(mon) or MONTHS.get(mon[:3])
            if month:
                return date(y, month, d), False
        except ValueError:
            return None, False
    return None, False


def date_order_evidence(text: str) -> str | None:
    """'dmy' / 'mdy' when a numeric date can only be read one way (e.g. 25/03 or 03/25)."""
    m = DATE_NUMERIC.search(fix_ocr_digits(text.strip()))
    if not m or len(m.group(1)) == 4:
        return None
    x, z = int(m.group(1)), int(m.group(3))
    if x > 12 >= z:
        return "dmy"
    if z > 12 >= x:
        return "mdy"
    return None


SKU_RE = re.compile(r"^[0-9OolISB]{5}[A-Za-z]{0,2}$")


def normalize_sku(token: str) -> str:
    t = re.sub(r"[\s\-./]", "", token).upper()
    head, tail = t[:5], t[5:]
    return head.translate(OCR_DIGIT) + tail


def looks_like_sku(token: str) -> bool:
    t = re.sub(r"[\s\-./]", "", token)
    return bool(SKU_RE.match(t)) and sum(c.isdigit() for c in t[:5]) >= 3

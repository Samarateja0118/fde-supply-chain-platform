from datetime import date

import pytest

from fde.docs.normalize import date_order_evidence, looks_like_sku, normalize_sku, parse_date, parse_number


@pytest.mark.parametrize(
    "text,hint,expected",
    [
        ("1,234.56", None, 1234.56),
        ("1.234,56", "eu", 1234.56),
        ("318,3", None, 318.3),
        ("USD 318.90", None, 318.9),
        ("1.234", "eu", 1234.0),
        ("1,234", None, 1234.0),
        ("12O,5O", None, 120.5),
        ("£7.00", None, 7.0),
        ("abc", None, None),
    ],
)
def test_parse_number(text, hint, expected):
    assert parse_number(text, hint) == expected


def test_parse_date_formats_and_ambiguity():
    assert parse_date("19.01.2026") == (date(2026, 1, 19), False)
    assert parse_date("February 25, 2026") == (date(2026, 2, 25), False)
    assert parse_date("5 mars 2026") == (date(2026, 3, 5), False)
    assert parse_date("03/04/2026", "dmy") == (date(2026, 4, 3), True)
    assert parse_date("03/04/2026", "mdy") == (date(2026, 3, 4), True)
    assert parse_date("12/25/2026") == (date(2026, 12, 25), False)
    assert date_order_evidence("25/03/2026") == "dmy" and date_order_evidence("03/25/2026") == "mdy"


def test_sku_normalisation_handles_ocr_and_formatting():
    assert normalize_sku("85123-a") == "85123A"
    assert normalize_sku("B4978") == "84978"
    assert looks_like_sku("85099B") and not looks_like_sku("PO-123456")

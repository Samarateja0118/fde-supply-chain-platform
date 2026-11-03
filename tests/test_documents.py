import json

from fde.docs.benchmark import run
from fde.docs.extract import RuleBasedExtractor
from fde.docs.holdout_styles import HOLDOUT_STYLES
from fde.docs.schemas import Document, LineItem
from fde.docs.service import VENDOR_MASTER
from fde.docs.styles import DEV_STYLES
from fde.docs.validate import MasterData, route, validate


def test_dev_layouts_extract_exactly_and_issues_are_caught(catalog, tmp_path):
    res = run(catalog, DEV_STYLES, VENDOR_MASTER, 70, tmp_path / "dev", seed=11)
    assert res["extraction"]["document_exact_match"] == 1.0
    assert res["issue_recall_total"] == 1.0
    assert res["auto_approved_with_error"].startswith("0/")


def test_holdout_layouts_never_auto_approve_a_wrong_document(catalog, tmp_path):
    res = run(catalog, HOLDOUT_STYLES, VENDOR_MASTER, 60, tmp_path / "ho", seed=12)
    assert res["auto_approved_with_error"].startswith("0/")
    assert res["extraction"]["header_field_accuracy"] > 0.9


def test_text_email_invoice_end_to_end():
    text = """Hello,

BRIGHTWELL SUPPLY CO.                                     TAX INVOICE
Invoice No: BW777                           Invoice Date: 25/03/2026
Due Date: 24/04/2026                        Your PO Ref: PO-400001

Item Code    Description                       Qty      Unit Price   Line Total
85123A       White Hanging Heart               12       2.55         30.60

                                                  Subtotal            £30.60
                                                  VAT @ 20%           £6.12
                                                  Total Due           £36.72
"""
    ex = RuleBasedExtractor(VENDOR_MASTER).extract_text(text)
    d = ex.doc
    assert (d.doc_type, d.sender, d.doc_number, d.po_number, d.total) == (
        "invoice",
        "Brightwell Supply Co.",
        "BW777",
        "PO-400001",
        36.72,
    )
    assert str(d.doc_date) == "2026-03-25" and d.lines[0].sku == "85123A" and d.lines[0].amount == 30.60
    md = MasterData(
        catalog={"85123A": {"price": 2.55, "pack": 6}},
        erp_pos={
            "PO-400001": {
                "supplier": "Brightwell Supply Co.",
                "currency": "GBP",
                "lines": {"85123A": [12, 2.55]},
            }
        },
    )
    assert validate(ex, md) == []
    assert route(ex, []).route == "auto_approve"
    md.erp_pos["PO-400001"]["lines"]["85123A"] = [12, 2.20]
    assert {f.code for f in validate(ex, md)} == {"price_variance"}


def test_line_missing_a_promised_column_goes_to_review():
    text = """PURCHASE ORDER
PO Number: X1                 Order Date: 25/03/2026
Product Code   Product          Qty    Price Each    Total
22423          Cake Stand       4                    51.00
"""
    ex = RuleBasedExtractor().extract_text(text)
    assert ex.doc.lines and ex.doc.lines[0].unit_price is None
    assert route(ex, []).route == "review"


def test_duplicate_invoice_detected():
    doc = Document(
        doc_type="invoice",
        sender="S",
        doc_number="1",
        po_number="P",
        total=1.0,
        lines=[LineItem(sku="1", qty=1, unit_price=1, amount=1)],
    )
    from fde.docs.extract import Extraction

    md = MasterData(
        catalog={},
        erp_pos={"P": {"supplier": "S", "currency": "GBP", "lines": {"1": [1, 1]}}},
        seen_documents={("invoice", "S", "1")},
    )
    assert "duplicate_invoice" in {f.code for f in validate(Extraction(doc), md)}
    json.dumps(doc.model_dump(mode="json"))

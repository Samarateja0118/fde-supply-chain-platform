"""Sender layouts used while developing the extractor (the "dev" set).

Each style is one sender's house format. They differ the way real inbound documents do: label
wording and language, number and date locale, column order, header arrangement, and whether the
document arrives as a PDF or pasted into an email.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Style:
    key: str
    sender: str
    doc_type: str
    title: str
    address: list[str]
    labels: dict[str, str]  # field -> label text
    columns: list[tuple[str, str]]  # (field, header text) in printed order
    header_mode: str = (
        "inline"  # inline "Label: value" | stacked (label line, value below) | box (label col + value col)
    )
    currency: str = "GBP"
    currency_symbol: str = "£"
    money_fmt: str = "{sym}{n}"
    number_locale: str = "en"
    date_fmt: str = "%d/%m/%Y"
    tax_rate: float = 0.20
    payment_days: int = 30
    number_pattern: str = "INV-{n}"
    email_share: float = 0.0  # share of documents delivered as plain-text email bodies
    noise_rate: float = 0.0  # OCR-style character confusion rate (simulated scanned PDFs)
    footer: list[str] = field(default_factory=list)
    sku_fmt: str = "plain"  # plain "85123A" | hyphen "85123-A" | lower "85123a"
    right_align_numbers: bool = False


DEV_STYLES: list[Style] = [
    Style(
        key="sup_uk",
        sender="Brightwell Supply Co.",
        doc_type="invoice",
        title="TAX INVOICE",
        address=["Unit 4, Canal Side Estate", "Leicester LE3 5TT", "VAT Reg GB 123 4567 89"],
        labels={
            "doc_number": "Invoice No:",
            "doc_date": "Invoice Date:",
            "due_date": "Due Date:",
            "po_number": "Your PO Ref:",
            "subtotal": "Subtotal",
            "tax": "VAT @ 20%",
            "total": "Total Due",
        },
        columns=[
            ("sku", "Item Code"),
            ("description", "Description"),
            ("qty", "Qty"),
            ("unit_price", "Unit Price"),
            ("amount", "Line Total"),
        ],
        header_mode="inline",
        number_pattern="BW{n}",
        email_share=0.2,
    ),
    Style(
        key="sup_de",
        sender="Kessler Haushaltswaren GmbH",
        doc_type="invoice",
        title="RECHNUNG",
        address=["Industriestraße 12", "50667 Köln", "USt-IdNr. DE123456789"],
        labels={
            "doc_number": "Rechnungsnummer",
            "doc_date": "Rechnungsdatum",
            "due_date": "Fällig am",
            "po_number": "Ihre Bestellnummer",
            "subtotal": "Zwischensumme",
            "tax": "MwSt. 19%",
            "total": "Gesamtbetrag",
        },
        columns=[
            ("pos", "Pos."),
            ("sku", "Artikel-Nr."),
            ("description", "Bezeichnung"),
            ("qty", "Menge"),
            ("unit_price", "Einzelpreis"),
            ("amount", "Betrag"),
        ],
        header_mode="stacked",
        currency="EUR",
        currency_symbol="€",
        money_fmt="{n} {sym}",
        number_locale="eu",
        date_fmt="%d.%m.%Y",
        tax_rate=0.19,
        payment_days=14,
        number_pattern="RE-2026-{n}",
    ),
    Style(
        key="sup_us",
        sender="Pacific Rim Trading LLC",
        doc_type="invoice",
        title="INVOICE",
        address=["2200 Harbor Blvd, Suite 300", "Long Beach, CA 90813"],
        labels={
            "doc_number": "Invoice #",
            "doc_date": "Date",
            "due_date": "Payment Due",
            "po_number": "Customer PO",
            "subtotal": "Sub Total",
            "tax": "Sales Tax",
            "total": "Amount Due",
        },
        columns=[
            ("description", "Description"),
            ("sku", "SKU"),
            ("qty", "Units"),
            ("unit_price", "Price"),
            ("amount", "Ext. Price"),
        ],
        header_mode="box",
        currency="USD",
        currency_symbol="$",
        money_fmt="{code} {n}",
        date_fmt="%B %d, %Y",
        tax_rate=0.0,
        payment_days=45,
        number_pattern="{n}-{r}",
        email_share=0.3,
    ),
    Style(
        key="car_fastfreight",
        sender="FastFreight Logistics Ltd",
        doc_type="bill_of_lading",
        title="BILL OF LADING",
        address=["Depot 7, M1 Junction 21", "Leicester LE19 1WP"],
        labels={
            "doc_number": "B/L No:",
            "doc_date": "Ship Date:",
            "po_number": "Customer PO:",
            "total_weight_kg": "Gross Weight (kg):",
        },
        columns=[("sku", "Item"), ("description", "Description"), ("cartons", "Ctns"), ("qty", "Units")],
        header_mode="inline",
        number_pattern="FF{n}",
    ),
    Style(
        key="car_nordsee",
        sender="Nordsee Spedition BV",
        doc_type="bill_of_lading",
        title="VRACHTBRIEF / BILL OF LADING",
        address=["Waalhaven Z.z. 19", "3089 JH Rotterdam"],
        labels={
            "doc_number": "Document number",
            "doc_date": "Date of shipment",
            "po_number": "Order reference",
            "total_weight_kg": "Total gross weight",
        },
        columns=[
            ("qty", "Quantity"),
            ("cartons", "Packages"),
            ("sku", "Article"),
            ("description", "Goods description"),
        ],
        header_mode="box",
        number_locale="eu",
        date_fmt="%d-%m-%Y",
        number_pattern="NS/{r}/{n}",
        email_share=0.1,
    ),
    Style(
        key="buy_cornerstore",
        sender="Cornerstone Gift Shops",
        doc_type="purchase_order",
        title="PURCHASE ORDER",
        address=["14 High Street", "Bath BA1 5AW"],
        labels={"doc_number": "PO Number:", "doc_date": "Order Date:", "total": "Order Total:"},
        columns=[
            ("sku", "Product Code"),
            ("description", "Product"),
            ("qty", "Qty"),
            ("unit_price", "Price Each"),
            ("amount", "Total"),
        ],
        header_mode="inline",
        number_pattern="CGS-{n}",
        email_share=0.5,
    ),
    Style(
        key="buy_maison",
        sender="Maison Lumière SARL",
        doc_type="purchase_order",
        title="BON DE COMMANDE",
        address=["8 rue de la Paix", "75002 Paris"],
        labels={"doc_number": "N° de commande", "doc_date": "Date", "total": "Total HT"},
        columns=[
            ("sku", "Réf."),
            ("description", "Désignation"),
            ("qty", "Qté"),
            ("unit_price", "Prix unitaire"),
            ("amount", "Montant"),
        ],
        header_mode="stacked",
        currency="EUR",
        currency_symbol="€",
        money_fmt="{n} {sym}",
        number_locale="eu",
        date_fmt="%d/%m/%Y",
        number_pattern="BC{n}",
    ),
]

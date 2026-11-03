"""Held-out sender layouts. Written AFTER the extractor was frozen (see git history: the extractor
commit precedes this file) and never used to tune it. They stand in for "the customer's next five
suppliers" that show up after go-live.

New things here that the dev set never showed the extractor: Spanish documents, right-aligned
numeric columns, hyphenated and lower-case SKUs, a simulated scanned PDF with OCR character
confusions, US month-first dates on a purchase order, and label wording outside the dev vocabulary.
"""

from __future__ import annotations

from .styles import Style

HOLDOUT_STYLES: list[Style] = [
    Style(
        key="sup_es",
        sender="Cerámicas del Levante S.L.",
        doc_type="invoice",
        title="FACTURA",
        address=["Polígono Industrial Fuente del Jarro", "46988 Paterna (Valencia)", "CIF B12345678"],
        labels={
            "doc_number": "Factura nº",
            "doc_date": "Fecha",
            "due_date": "Vencimiento",
            "po_number": "Su pedido",
            "subtotal": "Base imponible",
            "tax": "IVA 21%",
            "total": "Total factura",
        },
        columns=[
            ("sku", "Referencia"),
            ("description", "Descripción"),
            ("qty", "Cantidad"),
            ("unit_price", "Precio"),
            ("amount", "Importe"),
        ],
        header_mode="box",
        currency="EUR",
        currency_symbol="€",
        money_fmt="{n} {sym}",
        number_locale="eu",
        date_fmt="%d/%m/%Y",
        tax_rate=0.21,
        payment_days=60,
        number_pattern="FV26/{n}",
        sku_fmt="lower",
        right_align_numbers=True,
    ),
    Style(
        key="sup_scan",
        sender="Thornbury Packaging Ltd",
        doc_type="invoice",
        title="SALES INVOICE",
        address=["Mill Lane Works", "Thornbury, Bristol BS35 2AA"],
        labels={
            "doc_number": "Invoice Number",
            "doc_date": "Tax Point",
            "due_date": "Payment Due By",
            "po_number": "Order No.",
            "subtotal": "Net Total",
            "tax": "VAT 20%",
            "total": "Invoice Total",
        },
        columns=[
            ("pos", "Line"),
            ("sku", "Part No"),
            ("description", "Item Description"),
            ("qty", "Quantity"),
            ("unit_price", "Rate"),
            ("amount", "Net Amount"),
        ],
        header_mode="inline",
        number_pattern="TP-{n}",
        noise_rate=0.02,
        right_align_numbers=True,
        sku_fmt="hyphen",
    ),
    Style(
        key="car_iberia",
        sender="Transportes Iberia Norte S.A.",
        doc_type="bill_of_lading",
        title="CMR / ALBARÁN DE TRANSPORTE",
        address=["Calle del Puerto 22", "48980 Santurtzi (Bizkaia)"],
        labels={
            "doc_number": "Albarán nº",
            "doc_date": "Fecha de envío",
            "po_number": "Su pedido",
            "total_weight_kg": "Peso bruto (kg)",
        },
        columns=[
            ("cartons", "Bultos"),
            ("sku", "Referencia"),
            ("description", "Descripción"),
            ("qty", "Unidades"),
        ],
        header_mode="stacked",
        number_locale="eu",
        date_fmt="%d/%m/%Y",
        number_pattern="ALB-{n}",
        right_align_numbers=True,
    ),
    Style(
        key="buy_juniper",
        sender="Juniper & Vine Boutiques Inc.",
        doc_type="purchase_order",
        title="PURCHASE ORDER",
        address=["410 Pearl Street", "Boulder, CO 80302"],
        labels={"doc_number": "P.O. Number:", "doc_date": "Date Issued:", "total": "Total:"},
        columns=[
            ("sku", "Item #"),
            ("description", "Description"),
            ("qty", "Qty Ordered"),
            ("unit_price", "Unit Cost"),
            ("amount", "Extended"),
        ],
        header_mode="inline",
        currency="USD",
        currency_symbol="$",
        money_fmt="{sym}{n}",
        date_fmt="%m/%d/%Y",
        tax_rate=0.0,
        number_pattern="JV{n}",
        email_share=0.4,
    ),
]

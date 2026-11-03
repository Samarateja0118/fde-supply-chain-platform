"""Produce the customer's "as delivered" exports from the clean public data.

This is the part a forward-deployed engineer meets on day one: the customer cannot give you an API
to their sales history, but they can drop files on an SFTP. Everything odd about these files is a
thing that shows up in real legacy exports:

sales_orders_YYYY_MM.csv   (ERP report export)
  * Windows-1252 encoding, semicolon delimiter, decimal commas, day-first dates "01.12.2010 08:26"
  * a 3-line report preamble before the header and a TOTAL trailer row with the row count
  * header names that differ from anything canonical ("Invoice Nbr", "Qty Shipped", ...)
  * one month re-exported with a 400-row page duplicated (export pagination bug)
  * free-text descriptions containing semicolons and curly quotes (hence quoting + cp1252)

inventory_YYYYMMDD.dat     (mainframe fixed-width extract, EBCDIC cp037)
  * fixed 68-byte records with no newlines (RECFM=FB); HDR / DTL / TRL records, implied 2-decimal unit cost (PIC 9(5)V99), trailing-sign quantities
  * trailer carries a record count and a hash total of on-hand units for reconciliation

The REST side (products and purchase orders) is served by ``fde.mock_erp``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import random
from pathlib import Path

import pandas as pd

SALES_COLUMNS = [
    "Invoice Nbr",
    "Item",
    "Item Desc",
    "Qty Shipped",
    "Inv Date",
    "Unit Price (GBP)",
    "Cust #",
    "Ship Country",
]

FIXED_WIDTH = [  # (field, start, length) for DTL records, 1-based like the copybook
    ("rec_type", 1, 3),
    ("sku", 4, 8),
    ("description", 12, 30),
    ("warehouse", 42, 3),
    ("on_hand", 45, 9),  # 8 digits + trailing sign (" " or "-")
    ("unit_cost", 54, 7),  # PIC 9(5)V99, implied decimal point
    ("last_count", 61, 8),  # YYYYMMDD
]
RECORD_LEN = 68


def write_sales_exports(lines: pd.DataFrame, out_dir: Path, seed: int = 5) -> list[Path]:
    """lines: the canonical schema from ``fde.forecast.prepare.load_raw_online_retail``."""
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    month = lines["order_ts"].dt.to_period("M")
    dup_month = sorted(month.unique())[6]
    for m in sorted(month.unique()):
        part = lines[month == m]
        buf = io.StringIO()
        buf.write("HARBOR & PINE ERP 7.2 - SALES LINE REPORT\r\n")
        buf.write(
            f"Period: {m.strftime('%m/%Y')};Run by: BATCH;Printed: {(m.end_time + pd.Timedelta(days=1)).strftime('%d.%m.%Y')} 02:00\r\n"
        )
        buf.write("\r\n")
        w = csv.writer(buf, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        w.writerow(SALES_COLUMNS)
        rows = []
        for r in part.itertuples(index=False):
            desc = "" if pd.isna(r.description) else str(r.description)
            if rng.random() < 0.002:
                desc = desc.replace("'", "’") + "; SPECIAL"
            rows.append(
                [
                    r.order_id,
                    r.sku,
                    desc,
                    str(int(r.qty)),
                    r.order_ts.strftime("%d.%m.%Y %H:%M"),
                    f"{r.unit_price:.2f}".replace(".", ","),
                    "" if pd.isna(r.customer_id) else str(r.customer_id),
                    r.country,
                ]
            )
        if m == dup_month:
            start = len(rows) // 2
            rows = rows[: start + 400] + rows[start : start + 400] + rows[start + 400 :]
        w.writerows(rows)
        w.writerow(["TOTAL ROWS", str(len(rows)), "", "", "", "", "", ""])
        path = out_dir / f"sales_orders_{m.strftime('%Y_%m')}.csv"
        path.write_bytes(buf.getvalue().encode("cp1252", errors="replace"))
        paths.append(path)
    return paths


def write_inventory_extract(snapshot: pd.DataFrame, path: Path, as_of: str) -> Path:
    """snapshot columns: sku, description, warehouse, on_hand, unit_cost."""
    recs = [f"HDR{'INVSNAP':<8}{as_of}".ljust(RECORD_LEN)]
    total = 0
    for r in snapshot.itertuples(index=False):
        qty = int(r.on_hand)
        total += qty
        qty_field = f"{abs(qty):08d}{'-' if qty < 0 else ' '}"
        cost_field = f"{round(r.unit_cost * 100):07d}"
        desc = str(r.description)[:30]
        recs.append(f"DTL{r.sku:<8}{desc:<30}{r.warehouse:<3}{qty_field}{cost_field}{as_of}")
    recs.append(f"TRL{len(recs) - 1:09d}{total:+015d}".ljust(RECORD_LEN))
    assert all(len(r) == RECORD_LEN for r in recs)
    path.parent.mkdir(parents=True, exist_ok=True)
    # RECFM=FB: fixed-length records, no line terminators at all.
    path.write_bytes("".join(recs).encode("cp037"))
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

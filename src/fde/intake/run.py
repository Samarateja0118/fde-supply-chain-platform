"""Run every configured source for a customer into the database, idempotently.

Re-running is safe: a file whose SHA-256 was already loaded successfully is skipped; a changed file
with the same name replaces that file's rows; REST records upsert on their key.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pandas as pd
from sqlalchemy import delete, insert, select

from .. import db
from .adapters import fetch_all, load_mapping, read_fixed_width, read_sales_csv
from .exports import sha256


def _already_loaded(engine, digest: str) -> bool:
    with engine.connect() as c:
        return (
            c.execute(
                select(db.load_runs.c.id).where(
                    db.load_runs.c.sha256 == digest, db.load_runs.c.status == "loaded"
                )
            ).first()
            is not None
        )


def _record(engine, source: str, file: str | None, digest: str | None, status: str, report: dict) -> None:
    with engine.begin() as c:
        c.execute(
            insert(db.load_runs).values(source=source, file=file, sha256=digest, status=status, report=report)
        )


def run_intake(
    drop_dir: Path,
    engine,
    erp: httpx.Client | None,
    customer: str = "harbor_pine",
    quarantine_dir: Path | None = None,
) -> dict:
    cfg = load_mapping(customer)["sources"]
    quarantine_dir = quarantine_dir or drop_dir / "quarantine"
    summary: dict[str, list[dict] | dict] = {"sales_orders": [], "inventory": [], "rest": {}}

    for path in sorted(drop_dir.glob(cfg["sales_orders"]["glob"])):
        digest = sha256(path)
        if _already_loaded(engine, digest):
            summary["sales_orders"].append({"file": path.name, "status": "skipped (already loaded)"})
            continue
        df, quarantine, rep = read_sales_csv(path, cfg["sales_orders"])
        with engine.begin() as c:
            c.execute(delete(db.sales_lines).where(db.sales_lines.c.source_file == path.name))
            if len(df):
                rows = df.astype(object).where(df.notna(), None).to_dict("records")
                for i in range(0, len(rows), 5000):
                    c.execute(insert(db.sales_lines), rows[i : i + 5000])
        if quarantine:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            (quarantine_dir / f"{path.stem}.rejects.json").write_text(json.dumps(quarantine, default=str))
        status = "loaded" if rep.control_total_ok is not False else "loaded_with_control_break"
        _record(engine, "sales_orders", path.name, digest, status, rep.as_dict())
        summary["sales_orders"].append(rep.as_dict() | {"status": status})

    for path in sorted(drop_dir.glob(cfg["inventory"]["glob"])):
        digest = sha256(path)
        if _already_loaded(engine, digest):
            summary["inventory"].append({"file": path.name, "status": "skipped (already loaded)"})
            continue
        rows, quarantine, rep = read_fixed_width(path, cfg["inventory"])
        db.upsert(engine, db.inventory, rows, ["sku", "warehouse"])
        status = "loaded" if rep.control_total_ok else "loaded_with_control_break"
        _record(engine, "inventory", path.name, digest, status, rep.as_dict())
        summary["inventory"].append(rep.as_dict() | {"status": status})

    if erp is not None:
        for source, table, keys in (
            ("products", db.products, ["sku"]),
            ("purchase_orders", db.purchase_orders, ["po_number"]),
        ):
            items, stats, rep = fetch_all(erp, cfg[source], checkpoint=drop_dir / f".{source}.cursor")
            cols = {c.name for c in table.columns}
            rows = [{k: v for k, v in it.items() if k in cols} for it in items]
            db.upsert(engine, table, rows, keys)
            report = rep.as_dict() | {
                "requests": stats.requests,
                "pages": stats.pages,
                "retries": stats.retries,
                "schema_variants": stats.schema_variants,
            }
            _record(engine, source, None, None, "loaded", report)
            summary["rest"][source] = report
    return summary


def canonical_sales(engine) -> pd.DataFrame:
    with engine.connect() as c:
        df = pd.read_sql(select(db.sales_lines), c)
    df["order_ts"] = pd.to_datetime(df["order_ts"])
    df["customer_id"] = df["customer_id"].astype("string")
    return df.drop(columns=["id", "source_file"])

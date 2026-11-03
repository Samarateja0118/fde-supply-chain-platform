"""Storage. SQLite by default (tests, laptop), Postgres in Docker Compose (DATABASE_URL)."""

from __future__ import annotations

import os

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

sales_lines = Table(
    "sales_lines",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_file", String(200), index=True, nullable=False),
    Column("order_id", String(20), nullable=False),
    Column("sku", String(20), index=True, nullable=False),
    Column("description", Text),
    Column("qty", Float, nullable=False),
    Column("order_ts", DateTime, nullable=False),
    Column("unit_price", Float, nullable=False),
    Column("customer_id", String(20)),
    Column("country", String(60)),
)

products = Table(
    "products",
    metadata,
    Column("sku", String(20), primary_key=True),
    Column("description", Text),
    Column("list_price", Float),
    Column("unit_cost", Float),
    Column("case_pack", Integer),
    Column("weight_kg", Float),
    Column("updated_at", DateTime, server_default=func.now()),
)

purchase_orders = Table(
    "purchase_orders",
    metadata,
    Column("po_number", String(30), primary_key=True),
    Column("supplier", String(120)),
    Column("currency", String(3)),
    Column("lines", JSON),  # {sku: [qty, unit_price]}
)

inventory = Table(
    "inventory_snapshot",
    metadata,
    Column("sku", String(20), primary_key=True),
    Column("warehouse", String(3), primary_key=True),
    Column("description", Text),
    Column("on_hand", Integer),
    Column("unit_cost", Float),
    Column("as_of", String(8)),
)

load_runs = Table(
    "load_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String(40), nullable=False),
    Column("file", String(200)),
    Column("sha256", String(64), index=True),
    Column("status", String(20)),
    Column("report", JSON),
    Column("created_at", DateTime, server_default=func.now()),
)

documents = Table(
    "documents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("filename", String(200)),
    Column("sha256", String(64), index=True),
    Column("doc_type", String(20)),
    Column("sender", String(120)),
    Column("doc_number", String(60)),
    Column("status", String(20), index=True),  # auto_approved | needs_review | approved | rejected
    Column("extracted", JSON),
    Column("confidence", JSON),
    Column("findings", JSON),
    Column("reasons", JSON),
    Column("final", JSON),  # extracted + reviewer corrections
    Column("reviewer", String(80)),
    Column("created_at", DateTime, server_default=func.now()),
    Column("reviewed_at", DateTime),
)

corrections = Table(
    "corrections",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("document_id", Integer, index=True),
    Column("sender", String(120)),
    Column("field", String(40)),
    Column("extracted_value", Text),
    Column("corrected_value", Text),
    Column("created_at", DateTime, server_default=func.now()),
)

sender_profiles = Table(
    "sender_profiles",
    metadata,
    Column("sender", String(120), primary_key=True),
    Column("date_order", String(3)),
)


def get_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL", "sqlite:///fde.db")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, future=True)
    metadata.create_all(engine)
    return engine


def upsert(engine: Engine, table: Table, rows: list[dict], keys: list[str]) -> int:
    if not rows:
        return 0
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(table)
    update = {
        c.name: stmt.excluded[c.name] for c in table.columns if c.name not in keys and c.name in rows[0]
    }
    stmt = (
        stmt.on_conflict_do_update(index_elements=keys, set_=update)
        if update
        else stmt.on_conflict_do_nothing(index_elements=keys)
    )
    with engine.begin() as conn:
        for i in range(0, len(rows), 1000):
            conn.execute(stmt, rows[i : i + 1000])
    return len(rows)

"""A stand-in for the customer's ERP REST API, with the failure modes their real one had.

Faults (all seeded, so a run is reproducible):
* 429 Too Many Requests with a Retry-After header, at ``FAULT_429_RATE``
* 500/503 at ``FAULT_5XX_RATE``
* schema drift: from page ``DRIFT_AFTER_PAGE`` on, fields come back camelCased (unit_cost -> unitCost)

Run: ``uvicorn fde.mock_erp.server:app --port 8081`` with ``ERP_WORLD=path/to/world.json``.
"""

from __future__ import annotations

import base64
import json
import os
import random
import threading
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

CAMEL = {
    "unit_cost": "unitCost",
    "list_price": "listPrice",
    "case_pack": "casePack",
    "weight_kg": "weightKg",
    "po_number": "poNumber",
}


class Faults:
    def __init__(self, rate_429: float, rate_5xx: float, drift_after_page: int, seed: int):
        self.rate_429, self.rate_5xx, self.drift_after_page = rate_429, rate_5xx, drift_after_page
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.counts = {"requests": 0, "429": 0, "5xx": 0}

    def roll(self) -> int | None:
        with self.lock:
            self.counts["requests"] += 1
            r = self.rng.random()
            if r < self.rate_429:
                self.counts["429"] += 1
                return 429
            if r < self.rate_429 + self.rate_5xx:
                self.counts["5xx"] += 1
                return self.rng.choice([500, 503])
            return None


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def _offset(cursor: str | None) -> int:
    return json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"] if cursor else 0


def create_app(world: dict, faults: Faults | None = None) -> FastAPI:
    faults = faults or Faults(0.0, 0.0, 10**9, 0)
    app = FastAPI(title="Harbor & Pine ERP (mock)")
    products = [
        {
            "sku": sku,
            "description": it["description"],
            "list_price": it["price"],
            "unit_cost": round(it["price"] * 0.55, 2),
            "case_pack": it["pack"],
            "weight_kg": it["weight_kg"],
        }
        for sku, it in sorted(world["catalog"].items())
    ]
    pos = [
        {"po_number": k, "supplier": v["supplier"], "currency": v["currency"], "lines": v["lines"]}
        for k, v in sorted(world["erp_pos"].items())
    ]

    def page(items: list[dict], cursor: str | None, limit: int):
        code = faults.roll()
        if code == 429:
            return JSONResponse({"error": "rate limited"}, status_code=429, headers={"Retry-After": "0.05"})
        if code:
            return JSONResponse({"error": "upstream unavailable"}, status_code=code)
        off = _offset(cursor)
        chunk = items[off : off + limit]
        if off // max(limit, 1) >= faults.drift_after_page:
            chunk = [{CAMEL.get(k, k): v for k, v in it.items()} for it in chunk]
        nxt = _cursor(off + limit) if off + limit < len(items) else None
        return {"items": chunk, "next_cursor": nxt, "total": len(items)}

    @app.get("/health")
    def health():
        return {"ok": True, **faults.counts}

    @app.get("/api/v1/products")
    def list_products(cursor: str | None = None, limit: int = Query(100, le=500)):
        return page(products, cursor, limit)

    @app.get("/api/v1/purchase-orders")
    def list_pos(cursor: str | None = None, limit: int = Query(50, le=500)):
        return page(pos, cursor, limit)

    app.state.faults = faults
    return app


def _from_env() -> FastAPI:
    path = Path(os.environ.get("ERP_WORLD", "data/customer_drop/erp_world.json"))
    world = json.loads(path.read_text()) if path.exists() else {"catalog": {}, "erp_pos": {}}
    faults = Faults(
        float(os.environ.get("FAULT_429_RATE", "0.15")),
        float(os.environ.get("FAULT_5XX_RATE", "0.10")),
        int(os.environ.get("DRIFT_AFTER_PAGE", "3")),
        int(os.environ.get("FAULT_SEED", "42")),
    )
    return create_app(world, faults)


app = _from_env() if os.environ.get("FDE_MOCK_ERP_AUTOLOAD", "1") == "1" else None

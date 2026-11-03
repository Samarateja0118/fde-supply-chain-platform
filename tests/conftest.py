import random

import numpy as np
import pandas as pd
import pytest

from fde.docs.generator import CatalogItem


@pytest.fixture(scope="session")
def catalog():
    rng = random.Random(0)
    words = ["Heart", "Lantern", "Ceramic", "Vintage", "Cake", "Stand", "Bag", "Sign", "Candle", "Box"]
    out = {}
    for i in range(80):
        sku = f"{20000 + i * 37}" + (rng.choice(["", "", "A", "B"]))
        desc = " ".join(rng.sample(words, 3)).title()
        out[sku] = CatalogItem(
            sku,
            desc,
            round(rng.uniform(0.3, 9.0), 2),
            rng.choice([1, 4, 6, 12, 24]),
            round(rng.uniform(0.1, 1.0), 2),
        )
    return out


@pytest.fixture(scope="session")
def sales_lines():
    """Canonical sales lines shaped like the Online Retail data (small, synthetic)."""
    rng = np.random.default_rng(1)
    rows = []
    ts = pd.Timestamp("2010-12-01 08:00")
    for inv in range(3000):
        t = ts + pd.Timedelta(minutes=int(inv * 170))
        cust = str(12000 + rng.integers(0, 300))
        for _ in range(rng.integers(1, 6)):
            sku = f"{21000 + int(rng.integers(0, 60))}"
            rows.append(
                {
                    "order_id": str(536000 + inv),
                    "sku": sku,
                    "description": f"ITEM {sku}",
                    "qty": float(rng.integers(1, 25)),
                    "order_ts": t,
                    "unit_price": float(round(rng.uniform(0.5, 5), 2)),
                    "customer_id": cust,
                    "country": "United Kingdom",
                }
            )
    rows.append(
        {
            **rows[0],
            "order_id": "C539999",
            "qty": -rows[0]["qty"],
            "order_ts": rows[0]["order_ts"] + pd.Timedelta(hours=1),
        }
    )
    rows.append({**rows[1], "sku": "POST", "qty": 1.0})
    df = pd.DataFrame(rows)
    df["customer_id"] = df["customer_id"].astype("string")
    return df

"""Build an allocation scenario from the real order history plus an explicit, made-up DC network.

Real (from the data): which SKUs matter, each SKU's case pack (the most common order quantity),
how each SKU's demand splits across countries, unit prices, and next week's forecast.
Made up (and labelled as such in the README): three DCs, lane distances/costs, stock levels.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .allocate import Network

REGION_OF = {
    "United Kingdom": "UK",
    "EIRE": "Ireland",
    "Channel Islands": "UK",
    "Germany": "DACH",
    "Austria": "DACH",
    "Switzerland": "DACH",
    "France": "France",
    "Belgium": "Benelux",
    "Netherlands": "Benelux",
    "Spain": "Iberia",
    "Portugal": "Iberia",
    "Norway": "Nordics",
    "Sweden": "Nordics",
    "Denmark": "Nordics",
    "Finland": "Nordics",
    "Iceland": "Nordics",
    "Italy": "South-East EU",
    "Greece": "South-East EU",
    "Cyprus": "South-East EU",
    "Malta": "South-East EU",
    "Poland": "South-East EU",
    "Czech Republic": "South-East EU",
    "Lithuania": "South-East EU",
}
REGIONS = [
    "UK",
    "Ireland",
    "DACH",
    "France",
    "Benelux",
    "Iberia",
    "Nordics",
    "South-East EU",
    "Rest of World",
]
DCS = ["DC-Midlands (UK)", "DC-Rotterdam (NL)", "DC-Valencia (ES)"]
# Approximate road/ferry km from each DC to each region's demand centre (illustrative).
KM = np.array(
    [
        [150, 450, 1100, 750, 600, 1900, 1400, 2000, 3000],
        [600, 1000, 500, 500, 100, 1700, 900, 1500, 2800],
        [1900, 2200, 1700, 1100, 1600, 300, 2900, 1800, 3200],
    ],
    dtype=float,
)


def case_packs(clean: pd.DataFrame, skus: list[str]) -> np.ndarray:
    sub = clean[clean["sku"].isin(skus)]
    mode = sub.groupby("sku")["qty"].agg(lambda q: q.value_counts().idxmax())
    return np.clip(mode.reindex(skus).fillna(1).to_numpy(dtype=float), 1, 48)


def region_split(clean: pd.DataFrame, skus: list[str], since: pd.Timestamp) -> np.ndarray:
    sub = clean[(clean["sku"].isin(skus)) & (clean["order_ts"] >= since)]
    reg = sub["country"].map(REGION_OF).fillna("Rest of World")
    tab = sub.assign(region=reg).pivot_table(index="region", columns="sku", values="qty", aggfunc="sum")
    tab = tab.reindex(index=REGIONS, columns=skus).fillna(0.0)
    share = tab / tab.sum(axis=0).replace(0, 1)
    return share.to_numpy()


def build(
    clean: pd.DataFrame,
    next_week_fc: pd.Series,
    unit_price: pd.Series,
    n_skus: int = 60,
    stock_ratio: float = 0.85,
    seed: int = 11,
) -> Network:
    rng = np.random.default_rng(seed)
    skus = list((next_week_fc * unit_price).sort_values(ascending=False).index[:n_skus])
    since = clean["order_ts"].max() - pd.Timedelta(weeks=13)
    split = region_split(clean, skus, since)  # [R, S]
    demand = np.round(split * next_week_fc[skus].to_numpy()[None, :])
    pack = case_packs(clean, skus)
    total = demand.sum(axis=0)
    # Stock is scarce on purpose (85% of demand) and skewed towards the UK DC, with per-SKU noise.
    base = np.array([0.6, 0.25, 0.15])[:, None]
    noise = rng.dirichlet([6, 3, 2], size=len(skus)).T
    mix = 0.5 * base + 0.5 * noise
    stock = np.floor(mix * total[None, :] * stock_ratio)
    unit_cost = 0.05 + 0.0004 * KM
    fixed = 150 + 0.9 * KM
    priority = np.ones(len(REGIONS))
    priority[REGIONS.index("UK")] = 1.5  # key accounts
    margin = 0.4 * unit_price[skus].to_numpy()
    penalty = priority[:, None] * margin[None, :]
    return Network(DCS, REGIONS, skus, unit_cost, fixed, penalty, stock, demand, pack)

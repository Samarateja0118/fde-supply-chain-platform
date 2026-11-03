"""Turn canonical sales lines into a clean weekly SKU demand matrix.

Input columns (canonical schema produced by the intake adapters):
    order_id, sku, description, qty, order_ts, unit_price, customer_id, country

Cleaning decisions, each one a real problem in the UCI Online Retail data:
* Non-product codes (POST, DOT, M, BANK CHARGES, AMAZONFEE, gift vouchers) are fees, not demand.
* Zero/negative price lines are adjustments or samples.
* Cancellations ("C" invoices, negative qty) are matched one-for-one against the original order line
  (same customer, SKU and absolute quantity). Both sides are removed. This deletes the two
  fat-finger orders of 80,995 and 74,215 units that were cancelled minutes later; without it they
  dominate every error metric.
* Unmatched negative lines (returns of items bought before the data window) are dropped.
"""

from __future__ import annotations

import pandas as pd

PRODUCT_CODE = r"^\d{5}[A-Za-z]{0,2}$"


def clean_sales(lines: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    df = lines.copy()
    report: dict[str, int] = {"input_lines": len(df)}

    is_product = df["sku"].astype(str).str.match(PRODUCT_CODE)
    report["dropped_non_product"] = int((~is_product).sum())
    df = df[is_product]

    bad_price = df["unit_price"] <= 0
    report["dropped_nonpositive_price"] = int(bad_price.sum())
    df = df[~bad_price]

    df = df.assign(abs_qty=df["qty"].abs(), cust=df["customer_id"].fillna("?"))
    neg = df[df["qty"] < 0].sort_values("order_ts")
    pos = df[df["qty"] > 0].sort_values("order_ts")
    key = ["cust", "sku", "abs_qty"]
    # Pair the k-th cancellation of a (customer, sku, qty) with the k-th original order line.
    neg = neg.assign(k=neg.groupby(key).cumcount())
    pos = pos.assign(k=pos.groupby(key).cumcount())
    matched = pos.reset_index().merge(neg[key + ["k", "order_ts"]], on=key + ["k"], suffixes=("", "_c"))
    matched = matched[matched["order_ts"] <= matched["order_ts_c"]]
    report["cancellations_matched"] = len(matched)
    report["cancellations_unmatched"] = len(neg) - len(matched)
    df = pos.drop(index=matched["index"])

    report["clean_lines"] = len(df)
    return df.drop(columns=["abs_qty", "cust", "k"]), report


def weekly_matrix(
    clean: pd.DataFrame, n_skus: int = 500, min_history_weeks: int = 40
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (demand, price): weeks x SKUs, Monday-Sunday weeks labelled by the Sunday.

    The first, partial week of the dataset is dropped. SKUs are the top ``n_skus`` by revenue among
    those with at least ``min_history_weeks`` weeks between their first sale and the end of the data,
    so every SKU has enough history for the backtest.
    """
    week = clean["order_ts"].dt.to_period("W-SUN").dt.end_time.dt.normalize()
    df = clean.assign(week=week, revenue=clean["qty"] * clean["unit_price"])
    observed = sorted(df["week"].unique())
    # Full calendar: the Christmas shutdown week has no rows at all but is real zero demand.
    weeks = list(pd.date_range(observed[1], observed[-1], freq="7D"))
    df = df[df["week"].isin(weeks)]

    first = df.groupby("sku")["week"].min()
    span = (pd.Timestamp(weeks[-1]) - first).dt.days // 7 + 1
    eligible = span[span >= min_history_weeks].index
    rev = df[df["sku"].isin(eligible)].groupby("sku")["revenue"].sum().sort_values(ascending=False)
    skus = list(rev.index[:n_skus])

    sub = df[df["sku"].isin(skus)]
    demand = (
        sub.pivot_table(index="week", columns="sku", values="qty", aggfunc="sum").reindex(weeks).fillna(0.0)
    )
    qty = sub.groupby("sku")["qty"].sum()
    price = (sub.groupby("sku")["revenue"].sum() / qty).rename("unit_price").to_frame()
    return demand[skus], price.loc[skus]


def load_raw_online_retail(path: str) -> pd.DataFrame:
    """Read the original UCI Online Retail CSV into the canonical schema (benchmark path)."""
    raw = pd.read_csv(path, dtype={"StockCode": str, "InvoiceNo": str, "CustomerID": "string"})
    return pd.DataFrame(
        {
            "order_id": raw["InvoiceNo"],
            "sku": raw["StockCode"].str.upper(),
            "description": raw["Description"],
            "qty": raw["Quantity"].astype(float),
            "order_ts": pd.to_datetime(raw["InvoiceDate"], format="%m/%d/%Y %H:%M"),
            "unit_price": raw["UnitPrice"].astype(float),
            "customer_id": raw["CustomerID"].astype("string"),
            "country": raw["Country"],
        }
    )

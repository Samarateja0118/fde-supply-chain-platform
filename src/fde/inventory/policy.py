"""Periodic-review order-up-to policy and a backorder simulation to score it.

Timing (weekly buckets): review at the end of week t, the order arrives at the start of week t+2.
So the order must protect weeks t+1 and t+2 (lead time 1 + review period 1):

    S_t = F(t+1) + F(t+2) + z * RMSE_i

RMSE_i is SKU i's root-mean-square error of the 2-week cumulative forecast over a calibration block of
origins that precedes the evaluation block. Variants that were tried and lost on this data (kept in
``scripts/`` notes, not in the main path): bias-corrected SS (mean + z*std of residuals), SS scaled
with the forecast level, and ABC-differentiated z.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

Z_SWEEP = tuple(float(z) for z in np.linspace(0.25, 4.0, 16))


@dataclass
class PolicyResult:
    name: str
    z: float
    fill_rate: float  # units served from stock / units demanded
    cycle_service: float  # share of SKU-weeks without a stockout
    avg_inventory_units: float
    avg_inventory_value: float
    orders: int


def rmse_2w(fc: np.ndarray, act: np.ndarray) -> np.ndarray:
    """fc/act: [n_origins, 2, n_skus] -> per-SKU RMSE of the 2-week cumulative forecast."""
    r = act.sum(axis=1) - fc.sum(axis=1)
    return np.sqrt((r**2).mean(axis=0))


def order_up_to(fc: np.ndarray, rmse: np.ndarray, z: float | np.ndarray) -> np.ndarray:
    return np.maximum(fc.sum(axis=1) + z * rmse, 0.0)


def simulate(order_up_to: np.ndarray, demand: np.ndarray, unit_price: np.ndarray) -> dict:
    """order_up_to: [n_reviews, n_skus] target S at each review; demand: [n_reviews, n_skus] demand of
    the week following each review. Backorders carry over. Starts with on-hand = S_0, nothing on order.
    """
    n, k = demand.shape
    on_hand = order_up_to[0].copy()
    pipeline = np.zeros((n + 2, k))  # pipeline[w] arrives at the start of week w
    served = np.zeros(k)
    stockout_weeks = 0
    inv_units = np.zeros(n)
    inv_value = np.zeros(n)
    orders = 0
    for w in range(n):
        # Review at end of week w-1 (i.e. before week w's demand): position = on hand + on order.
        position = on_hand + pipeline[w:].sum(axis=0)
        q = np.maximum(order_up_to[w] - position, 0.0)
        q = np.ceil(q)
        orders += int((q > 0).sum())
        pipeline[w + 1] += q  # arrives at start of the week after next review period
        on_hand = on_hand + pipeline[w]
        d = demand[w]
        served += np.minimum(np.maximum(on_hand, 0.0), d)
        stockout_weeks += int((on_hand < d).sum())
        on_hand = on_hand - d
        pos = np.maximum(on_hand, 0.0)
        inv_units[w] = pos.sum()
        inv_value[w] = (pos * unit_price).sum()
    total = demand.sum()
    return {
        "fill_rate": float(served.sum() / total) if total else 1.0,
        "cycle_service": 1 - stockout_weeks / (n * k),
        "avg_inventory_units": float(inv_units.mean()),
        "avg_inventory_value": float(inv_value.mean()),
        "orders": orders,
    }


def frontier(bt, calib, unit_price: np.ndarray, model: str, zs=Z_SWEEP) -> list[PolicyResult]:
    """Fill rate vs average inventory value for one forecaster across a sweep of z."""
    demand = bt[model].actuals[:, 0, :]
    sd = rmse_2w(calib[model].forecasts, calib[model].actuals)
    return [
        PolicyResult(model, float(z), **simulate(order_up_to(bt[model].forecasts, sd, z), demand, unit_price))
        for z in zs
    ]


def cover_rule(bt, unit_price: np.ndarray, weeks=(2, 3, 4, 5)) -> list[PolicyResult]:
    """Rule of thumb many customers run today: order up to N weeks of trailing-4-week average demand."""
    demand = bt["ma4"].actuals[:, 0, :]
    return [
        PolicyResult(
            f"cover_{w}w", float("nan"), **simulate(bt["ma4"].forecasts[:, 0, :] * w, demand, unit_price)
        )
        for w in weeks
    ]


def inventory_at_fill(points: list[PolicyResult], target: float) -> float | None:
    pts = sorted(points, key=lambda r: r.fill_rate)
    fr = [r.fill_rate for r in pts]
    if fr[-1] < target or fr[0] > target:
        return None
    return float(np.interp(target, fr, [r.avg_inventory_value for r in pts]))


def tune_z(calib_prev, calib, unit_price: np.ndarray, model: str, target: float) -> float:
    """Pick the smallest z that reaches ``target`` fill rate when simulated on the calibration block,
    with RMSE taken from the block before it. This is what you could actually do in production."""
    sd = rmse_2w(calib_prev[model].forecasts, calib_prev[model].actuals)
    demand = calib[model].actuals[:, 0, :]
    for z in np.arange(0.0, 5.01, 0.05):
        if simulate(order_up_to(calib[model].forecasts, sd, z), demand, unit_price)["fill_rate"] >= target:
            return float(round(z, 2))
    return 5.0


def z_for(service: float) -> float:
    return float(norm.ppf(service))

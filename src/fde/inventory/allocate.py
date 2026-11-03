"""Allocate scarce DC stock to regional demand: a small mixed-integer program solved with HiGHS
(via scipy.optimize.milp), compared against the greedy "nearest DC first" rule.

Decision variables
    c[w,r,s]  integer   cases of SKU s shipped from DC w to region r
    y[w,r]    binary    lane w->r is used this week (pays a fixed truck/linehaul cost)
    u[r,s]    continuous  units of demand left unmet (penalised at lost margin x region priority)

Objective  min  sum unit_cost[w,r] * pack[s] * c  +  fixed[w,r] * y  +  penalty[r,s] * u
Subject to
    sum_r pack[s] * c[w,r,s] <= stock[w,s]                       (DC stock)
    sum_w pack[s] * c[w,r,s] + u[r,s] >= demand[r,s]             (demand cover; overshoot by < 1 case allowed)
    sum_w pack[s] * c[w,r,s] <= demand[r,s] + pack[s] - 1        (do not ship a full extra case)
    pack[s] * c[w,r,s] <= M[w,r,s] * y[w,r]                      (no shipment on an unused lane)
    sum_s u[r,s] <= (1 - floor) * sum_s demand[r,s]              (contractual service floor per region)

Without the service floor the optimum simply abandons small regions whose lost margin is below a
lane's fixed cost. That is "optimal" and also how you lose a customer, so the floor is a parameter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix


@dataclass
class Network:
    dcs: list[str]
    regions: list[str]
    skus: list[str]
    unit_cost: np.ndarray  # [W, R] cost per unit shipped
    fixed_cost: np.ndarray  # [W, R] cost per lane used
    penalty: np.ndarray  # [R, S] cost per unit unmet
    stock: np.ndarray  # [W, S] units on hand
    demand: np.ndarray  # [R, S] units forecast
    pack: np.ndarray  # [S] units per case


@dataclass
class Plan:
    method: str
    shipped: np.ndarray  # [W, R, S] units
    lanes_used: int
    transport_cost: float
    fixed_cost: float
    unmet_cost: float
    unmet_units: float
    seconds: float
    mip_gap: float | None = None
    min_region_fill: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.transport_cost + self.fixed_cost + self.unmet_cost

    def summary(self, demand_total: float) -> dict:
        return {
            "method": self.method,
            "total_cost": round(self.total_cost, 2),
            "transport_cost": round(self.transport_cost, 2),
            "fixed_lane_cost": round(self.fixed_cost, 2),
            "unmet_penalty": round(self.unmet_cost, 2),
            "fill_rate": round(float(1 - self.unmet_units / demand_total), 4),
            "min_region_fill": round(float(self.min_region_fill), 4),
            "lanes_used": self.lanes_used,
            "seconds": round(self.seconds, 3),
            "mip_gap": self.mip_gap,
        }


def _cost(net: Network, shipped: np.ndarray, method: str, seconds: float, gap=None) -> Plan:
    served = shipped.sum(axis=0)  # [R, S]
    unmet = np.maximum(net.demand - served, 0)
    used = shipped.sum(axis=2) > 0
    return Plan(
        method=method,
        shipped=shipped,
        lanes_used=int(used.sum()),
        transport_cost=float((shipped.sum(axis=2) * net.unit_cost).sum()),
        fixed_cost=float((used * net.fixed_cost).sum()),
        unmet_cost=float((unmet * net.penalty).sum()),
        unmet_units=float(unmet.sum()),
        seconds=seconds,
        mip_gap=gap,
        min_region_fill=float((np.minimum(served, net.demand).sum(axis=1) / np.maximum(net.demand.sum(axis=1), 1)).min()),
    )


def solve_milp(
    net: Network, min_region_fill: float = 0.0, time_limit: float = 30.0, mip_rel_gap: float = 1e-3
) -> Plan:
    t0 = time.perf_counter()
    W, R, S = len(net.dcs), len(net.regions), len(net.skus)
    nc, ny, nu = W * R * S, W * R, R * S
    ci = lambda w, r, s: (w * R + r) * S + s  # noqa: E731
    yi = lambda w, r: nc + w * R + r  # noqa: E731
    ui = lambda r, s: nc + ny + r * S + s  # noqa: E731
    n = nc + ny + nu

    cost = np.zeros(n)
    for w in range(W):
        for r in range(R):
            for s in range(S):
                cost[ci(w, r, s)] = net.unit_cost[w, r] * net.pack[s]
            cost[yi(w, r)] = net.fixed_cost[w, r]
    for r in range(R):
        for s in range(S):
            cost[ui(r, s)] = net.penalty[r, s]

    rows = W * S + 2 * R * S + W * R * S + R
    A = lil_matrix((rows, n))
    lb = np.full(rows, -np.inf)
    ub = np.full(rows, np.inf)
    k = 0
    for w in range(W):
        for s in range(S):
            for r in range(R):
                A[k, ci(w, r, s)] = net.pack[s]
            ub[k] = net.stock[w, s]
            k += 1
    for r in range(R):
        for s in range(S):
            for w in range(W):
                A[k, ci(w, r, s)] = net.pack[s]
                A[k + 1, ci(w, r, s)] = net.pack[s]
            A[k, ui(r, s)] = 1
            lb[k] = net.demand[r, s]
            ub[k + 1] = net.demand[r, s] + net.pack[s] - 1
            k += 2
    for w in range(W):
        for r in range(R):
            for s in range(S):
                big_m = min(net.stock[w, s], net.demand[r, s] + net.pack[s])
                A[k, ci(w, r, s)] = net.pack[s]
                A[k, yi(w, r)] = -big_m
                ub[k] = 0
                k += 1
    for r in range(R):
        # Floor on demand actually covered (overshipping a case on one SKU must not count).
        for s in range(S):
            A[k, ui(r, s)] = 1
        ub[k] = (1 - min_region_fill) * net.demand[r].sum()
        k += 1

    integrality = np.zeros(n)
    integrality[: nc + ny] = 1
    upper = np.full(n, np.inf)
    upper[nc : nc + ny] = 1
    upper[nc + ny :] = net.demand.reshape(-1)
    res = milp(
        cost,
        constraints=LinearConstraint(A.tocsr(), lb, ub),
        integrality=integrality,
        bounds=Bounds(np.zeros(n), upper),
        options={"time_limit": time_limit, "mip_rel_gap": mip_rel_gap, "disp": False},
    )
    if res.x is None:
        raise RuntimeError(f"MILP failed: {res.message}")
    cases = np.round(res.x[:nc]).reshape(W, R, S)
    shipped = cases * net.pack[None, None, :]
    gap = getattr(res, "mip_gap", None)
    method = f"milp_floor_{min_region_fill:.0%}" if min_region_fill else "milp"
    return _cost(net, shipped, method, time.perf_counter() - t0, None if gap is None else float(gap))


def solve_greedy(net: Network) -> Plan:
    """Today's process: walk regions in priority order, fill each from its cheapest DC that still has
    stock, rounding up to whole cases."""
    t0 = time.perf_counter()
    W, R, S = len(net.dcs), len(net.regions), len(net.skus)
    stock = net.stock.astype(float).copy()
    shipped = np.zeros((W, R, S))
    region_order = np.argsort(-net.penalty.mean(axis=1))
    for r in region_order:
        for s in range(S):
            need = net.demand[r, s]
            for w in np.argsort(net.unit_cost[:, r]):
                if need <= 0:
                    break
                cases = min(np.ceil(need / net.pack[s]), np.floor(stock[w, s] / net.pack[s]))
                if cases <= 0:
                    continue
                units = cases * net.pack[s]
                shipped[w, r, s] += units
                stock[w, s] -= units
                need -= units
    return _cost(net, shipped, "greedy_nearest_dc", time.perf_counter() - t0)

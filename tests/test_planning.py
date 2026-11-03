import numpy as np
import pandas as pd

from fde.forecast.backtest import metrics, run_backtest, split_origins
from fde.forecast.models import MODELS, croston_sba
from fde.forecast.prepare import clean_sales, weekly_matrix
from fde.inventory import allocate, policy


def test_cleaning_matches_cancellations_and_drops_fees(sales_lines):
    clean, rep = clean_sales(sales_lines)
    assert rep["cancellations_matched"] == 1 and rep["dropped_non_product"] == 1
    assert len(clean) == len(sales_lines) - 3


def test_models_and_backtest_shapes(sales_lines):
    d, p = weekly_matrix(clean_sales(sales_lines)[0], n_skus=30, min_history_weeks=20)
    calib, ev = split_origins(len(d), n_eval=4, n_calib=4)
    res = run_backtest(d, p, ["naive", "ma4", "ses", "croston_sba", "lgbm"], ev)
    for r in res.values():
        assert r.forecasts.shape == (4, 2, 30) and (r.forecasts >= 0).all()
        m = metrics(r, d, p)
        assert 0 <= m["wape_h1"] < 5


def test_croston_on_intermittent_series():
    y = np.zeros((80, 1))
    y[::4] = 8
    f = croston_sba(y, 1)[0, 0]
    assert 1.5 < f < 2.6  # ~ 8 every 4 weeks = 2/week, SBA-deflated


def test_simulation_bounds():
    demand = np.full((10, 3), 5.0)
    big = policy.simulate(np.full((10, 3), 1000.0), demand, np.ones(3))
    none = policy.simulate(np.zeros((10, 3)), demand, np.ones(3))
    assert big["fill_rate"] == 1.0 and none["fill_rate"] == 0.0


def _net():
    W, R, S = 2, 3, 4
    rng = np.random.default_rng(0)
    return allocate.Network(
        dcs=["A", "B"],
        regions=["r1", "r2", "r3"],
        skus=list("abcd"),
        unit_cost=rng.uniform(0.1, 1, (W, R)),
        fixed_cost=rng.uniform(10, 50, (W, R)),
        penalty=rng.uniform(1, 3, (R, S)),
        stock=np.full((W, S), 30.0),
        demand=rng.integers(5, 25, (R, S)).astype(float),
        pack=np.array([1.0, 2.0, 3.0, 6.0]),
    )


def test_milp_beats_greedy_and_respects_stock_and_floor():
    net = _net()
    g, m = allocate.solve_greedy(net), allocate.solve_milp(net)
    assert m.total_cost <= g.total_cost + 1e-6
    for plan in (g, m, allocate.solve_milp(net, 0.7)):
        assert (plan.shipped.sum(axis=1) <= net.stock + 1e-9).all()
        assert np.allclose(plan.shipped % net.pack[None, None, :], 0)
    assert allocate.solve_milp(net, 0.7).min_region_fill >= 0.7 - 1e-9


def test_registry_has_all_models():
    assert {"naive", "ma4", "ses", "croston_sba", "lgbm", "ens"} <= set(MODELS)
    assert isinstance(pd.Timestamp("2011-01-01"), pd.Timestamp)

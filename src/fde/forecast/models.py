"""Forecasters. Each takes the demand history up to and including the origin week and returns
forecasts for horizons 1..H for every SKU (array of shape [H, n_skus]).

Baselines are deliberately simple and transparent; the global LightGBM model is the candidate that
has to beat them.
"""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd


def naive(hist: np.ndarray, h: int) -> np.ndarray:
    return np.repeat(hist[-1:], h, axis=0)


def moving_average(hist: np.ndarray, h: int, window: int = 4) -> np.ndarray:
    return np.repeat(hist[-window:].mean(axis=0, keepdims=True), h, axis=0)


def ses(hist: np.ndarray, h: int, alphas=(0.05, 0.1, 0.2, 0.3, 0.5)) -> np.ndarray:
    """Simple exponential smoothing; alpha picked per SKU by in-sample one-step SSE."""
    t, n = hist.shape
    best_level = np.zeros(n)
    best_sse = np.full(n, np.inf)
    for a in alphas:
        level = hist[:4].mean(axis=0)
        sse = np.zeros(n)
        for i in range(4, t):
            sse += (hist[i] - level) ** 2
            level = a * hist[i] + (1 - a) * level
        better = sse < best_sse
        best_sse[better] = sse[better]
        best_level[better] = level[better]
    return np.repeat(best_level[None, :], h, axis=0)


def croston_sba(hist: np.ndarray, h: int, alpha: float = 0.1) -> np.ndarray:
    """Croston with the Syntetos-Boylan bias correction, for intermittent SKUs."""
    _, n = hist.shape
    out = np.zeros(n)
    for j in range(n):
        y = hist[:, j]
        nz = np.flatnonzero(y > 0)
        if len(nz) == 0:
            continue
        z = y[nz[0]]
        p = nz[0] + 1.0
        last = nz[0]
        for i in nz[1:]:
            z = alpha * y[i] + (1 - alpha) * z
            p = alpha * (i - last) + (1 - alpha) * p
            last = i
        out[j] = (1 - alpha / 2) * z / p
    return np.repeat(out[None, :], h, axis=0)


LAGS = (1, 2, 3, 4, 6, 8, 13)


def _features(
    hist: np.ndarray, end: int, horizon: int, price: np.ndarray, first_sale: np.ndarray
) -> np.ndarray:
    """Features available at origin ``end`` (inclusive index) for predicting week end+horizon.

    Everything is scaled by the SKU's trailing 13-week mean so one model serves SKUs whose volume
    differs by three orders of magnitude.
    """
    window = hist[max(0, end - 12) : end + 1]
    scale = np.maximum(window.mean(axis=0), 1.0)
    cols = []
    for lag in LAGS:
        idx = end - lag + 1
        cols.append(hist[idx] / scale if idx >= 0 else np.full(hist.shape[1], np.nan))
    for w in (4, 8, 13):
        cols.append(hist[max(0, end - w + 1) : end + 1].mean(axis=0) / scale)
    cols.append(hist[max(0, end - 7) : end + 1].std(axis=0) / scale)
    cols.append((hist[max(0, end - 12) : end + 1] > 0).mean(axis=0))
    cols.append(np.log1p(scale))
    cols.append(np.log(price))
    cols.append(np.full(hist.shape[1], horizon, dtype=float))
    cols.append(end - first_sale)
    return np.column_stack(cols), scale


class GlobalLGBM:
    """One LightGBM model across all SKUs and horizons (direct multi-horizon strategy).

    Calendar features are intentionally absent: the data covers 53 weeks, so "week of year" would
    let the model memorise a single observation per week rather than learn seasonality.
    """

    def __init__(self, horizons: int = 2, seed: int = 7):
        self.h = horizons
        self.params = dict(
            objective="l1",  # median-ish forecasts: robust to the remaining bulk orders
            learning_rate=0.05,
            num_leaves=15,
            min_data_in_leaf=50,
            feature_fraction=0.8,
            bagging_fraction=0.8,
            bagging_freq=1,
            seed=seed,
            verbose=-1,
            num_threads=2,
        )

    def fit_predict(self, hist: np.ndarray, price: np.ndarray, rounds: int = 300) -> np.ndarray:
        t, n = hist.shape
        first_sale = np.argmax(hist > 0, axis=0).astype(float)
        X, y = [], []
        for end in range(8, t - 1):
            for hz in range(1, self.h + 1):
                if end + hz >= t:
                    continue
                f, scale = _features(hist, end, hz, price, first_sale)
                X.append(f)
                y.append(hist[end + hz] / scale)
        X = np.vstack(X)
        y = np.concatenate(y)
        model = lgb.train(self.params, lgb.Dataset(X, y), num_boost_round=rounds)
        out = np.zeros((self.h, n))
        for hz in range(1, self.h + 1):
            f, scale = _features(hist, t - 1, hz, price, first_sale)
            out[hz - 1] = np.maximum(model.predict(f), 0) * scale
        return out


def lgbm(hist: np.ndarray, h: int, price: np.ndarray) -> np.ndarray:
    return GlobalLGBM(horizons=h).fit_predict(hist, price)


MODELS = {
    "naive": lambda hist, h, price: naive(hist, h),
    "ma4": lambda hist, h, price: moving_average(hist, h, 4),
    "ma8": lambda hist, h, price: moving_average(hist, h, 8),
    "ses": lambda hist, h, price: ses(hist, h),
    "croston_sba": lambda hist, h, price: croston_sba(hist, h),
    "lgbm": lgbm,
    # Equal-weight blend of the global model and per-SKU smoothing.
    "ens": lambda hist, h, price: 0.5 * lgbm(hist, h, price) + 0.5 * ses(hist, h),
}


def as_frame(fc: np.ndarray, skus: list[str], origin: pd.Timestamp) -> pd.DataFrame:
    weeks = [origin + pd.Timedelta(weeks=i + 1) for i in range(fc.shape[0])]
    return pd.DataFrame(fc, index=pd.DatetimeIndex(weeks, name="week"), columns=skus)

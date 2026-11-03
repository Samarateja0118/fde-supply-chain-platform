"""Rolling-origin backtest.

Origins are split into two consecutive blocks so that nothing is tuned on the weeks it is scored on:
* calibration block: forecast errors here size each SKU's safety stock;
* evaluation block: forecast accuracy and the inventory simulation are reported here only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .models import MODELS


@dataclass
class BacktestResult:
    model: str
    origins: list[int]  # index of the last observed week for each origin
    forecasts: np.ndarray  # [n_origins, H, n_skus]
    actuals: np.ndarray  # [n_origins, H, n_skus]
    seconds: float


def run_backtest(
    demand: pd.DataFrame, price: pd.DataFrame, models: list[str], origins: list[int], h: int = 2
) -> dict[str, BacktestResult]:
    y = demand.to_numpy(dtype=float)
    p = price["unit_price"].to_numpy(dtype=float)
    out = {}
    for name in models:
        fn = MODELS[name]
        t0 = time.perf_counter()
        fc, act = [], []
        for end in origins:
            fc.append(fn(y[: end + 1], h, p))
            act.append(y[end + 1 : end + 1 + h])
        out[name] = BacktestResult(name, origins, np.stack(fc), np.stack(act), time.perf_counter() - t0)
    return out


def split_origins(
    n_weeks: int, h: int = 2, n_eval: int = 13, n_calib: int = 13
) -> tuple[list[int], list[int]]:
    last = n_weeks - 1 - h  # last origin with h weeks of actuals after it
    eval_o = list(range(last - n_eval + 1, last + 1))
    calib_o = list(range(eval_o[0] - n_calib, eval_o[0]))
    return calib_o, eval_o


def metrics(res: BacktestResult, demand: pd.DataFrame, price: pd.DataFrame) -> dict:
    f, a = res.forecasts, res.actuals
    err = f - a
    y = demand.to_numpy(dtype=float)
    # MASE scale: in-sample one-step naive MAE per SKU over the weeks before the first origin.
    first = res.origins[0]
    scale = np.maximum(np.abs(np.diff(y[: first + 1], axis=0)).mean(axis=0), 1e-9)
    out = {"model": res.model, "seconds": round(res.seconds, 2)}
    for hz in range(f.shape[1]):
        e, act = err[:, hz], a[:, hz]
        out[f"wape_h{hz + 1}"] = float(np.abs(e).sum() / act.sum())
        out[f"bias_h{hz + 1}"] = float(e.sum() / act.sum())
        out[f"mase_h{hz + 1}"] = float(np.median(np.abs(e).mean(axis=0) / scale))
    # Revenue-weighted WAPE at h=1: errors on expensive SKUs cost more working capital.
    w = price["unit_price"].to_numpy()
    out["wape_rev_h1"] = float((np.abs(err[:, 0]) * w).sum() / (a[:, 0] * w).sum())
    return out

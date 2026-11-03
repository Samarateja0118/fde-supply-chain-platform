"""End-to-end customer deployment demo, in the order it happens on site:

1. customer drop:  the customer's legacy exports appear (generated here from the public dataset)
2. intake:         CSV / fixed-width / REST adapters load the warehouse DB, with DQ reports
3. reconcile:      the loaded data must reproduce the source exactly before anyone forecasts on it
4. plan:           forecast backtest, inventory policy simulation, allocation, next-week plan
5. documents:      the AP / order-entry inbox is processed against the ERP data that was loaded
"""

from __future__ import annotations

import json
import math
import random
import shutil
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from . import db
from .docs.generator import World, catalog_from_sales, generate
from .docs.holdout_styles import HOLDOUT_STYLES
from .docs.service import load_master_data, process_file
from .docs.styles import DEV_STYLES
from .forecast.backtest import metrics, run_backtest, split_origins
from .forecast.models import MODELS
from .forecast.prepare import clean_sales, load_raw_online_retail, weekly_matrix
from .intake.exports import write_inventory_extract, write_sales_exports
from .intake.run import canonical_sales, run_intake
from .inventory import allocate, policy, scenario

RAW_URL = "https://raw.githubusercontent.com/databricks/Spark-The-Definitive-Guide/master/data/retail-data/all/online-retail-dataset.csv"
RAW_SHA256 = "a2f79bbdd4463df6db8a3f5a50b9c980ae8f645a370bf5e2c0d6097f9e817b05"


def prepare_drop(raw_csv: Path, drop: Path, inbox: Path, n_docs: int = 120, seed: int = 7) -> dict:
    t0 = time.perf_counter()
    shutil.rmtree(drop, ignore_errors=True)
    shutil.rmtree(inbox, ignore_errors=True)
    raw = load_raw_online_retail(str(raw_csv))
    files = write_sales_exports(raw, drop)
    clean, _ = clean_sales(raw)
    world = World(catalog_from_sales(clean))
    generate(world, DEV_STYLES + HOLDOUT_STYLES, n_docs, inbox, seed=seed)
    (drop / "erp_world.json").write_text(json.dumps(world.to_json()))
    # On-hand snapshot: ~2 weeks of recent demand per SKU, split over three DCs, a few negatives.
    rng = random.Random(seed)
    recent = clean[clean["order_ts"] >= clean["order_ts"].max() - pd.Timedelta(weeks=4)]
    wk = recent.groupby("sku")["qty"].sum() / 4
    rows = []
    for sku, item in world.catalog.items():
        base = float(wk.get(sku, 0.0)) * 2
        for wh, share in (("MID", 0.6), ("RTM", 0.25), ("VLC", 0.15)):
            q = int(base * share * rng.uniform(0.5, 1.5))
            if rng.random() < 0.01:
                q = -rng.randint(1, 5)
            rows.append(
                {
                    "sku": sku,
                    "description": item.description.upper(),
                    "warehouse": wh,
                    "on_hand": q,
                    "unit_cost": round(item.price * 0.55, 2),
                }
            )
    write_inventory_extract(pd.DataFrame(rows), drop / "inventory_20111209.dat", "20111209")
    return {
        "sales_files": len(files),
        "documents": n_docs,
        "inventory_records": len(rows),
        "seconds": round(time.perf_counter() - t0, 1),
    }


def reconcile(engine, raw_csv: Path) -> dict:
    """Loaded rows must equal the source, and the forecasting input must be bit-identical."""
    raw = load_raw_online_retail(str(raw_csv))
    canon = canonical_sales(engine)
    a, _ = clean_sales(raw)
    b, _ = clean_sales(canon)
    da, _ = weekly_matrix(a)
    dbm, _ = weekly_matrix(b)
    same = (
        da.shape == dbm.shape
        and list(da.columns) == list(dbm.columns)
        and bool(np.array_equal(da.values, dbm.values))
    )
    return {
        "source_rows": len(raw),
        "loaded_rows": len(canon),
        "qty_sum_source": float(raw["qty"].sum()),
        "qty_sum_loaded": float(canon["qty"].sum()),
        "revenue_source": round(float((raw["qty"] * raw["unit_price"]).sum()), 2),
        "revenue_loaded": round(float((canon["qty"] * canon["unit_price"]).sum()), 2),
        "weekly_demand_matrix_identical": same,
        "ok": same and len(raw) == len(canon),
    }


def plan(engine, results: Path, models: list[str] | None = None) -> dict:
    t0 = time.perf_counter()
    results.mkdir(parents=True, exist_ok=True)
    canon = canonical_sales(engine)
    clean, clean_report = clean_sales(canon)
    demand, price = weekly_matrix(clean)
    calib_o, eval_o = split_origins(len(demand))
    prev_o = list(range(calib_o[0] - 13, calib_o[0]))
    models = models or ["naive", "ma4", "ma8", "ses", "croston_sba", "lgbm", "ens"]
    bt = run_backtest(demand, price, models, eval_o)
    cal = run_backtest(demand, price, models, calib_o)
    prev = run_backtest(demand, price, ["ma4", "ens"], prev_o)
    fc_metrics = [metrics(bt[m], demand, price) for m in models]

    up = price["unit_price"].to_numpy()
    frontiers = {f"rmse_ss_{m}": policy.frontier(bt, cal, up, m) for m in ("ma4", "ses", "lgbm", "ens")}
    frontiers |= {f"cover_{m}": policy.cover(bt, up, m) for m in ("ma4", "ses", "lgbm", "ens")}
    at_fill = {
        str(t): {k: policy.inventory_at_fill(v, t) for k, v in frontiers.items()} for t in (0.80, 0.85, 0.90)
    }
    # What you could actually deploy: parameters picked on the calibration block, scored on evaluation.
    tuned = []
    for m in ("ma4", "ens"):
        for target in (0.85, 0.90):
            z = policy.tune_z(prev, cal, up, m, target)
            sd = policy.rmse_2w(cal[m].forecasts, cal[m].actuals)
            res = policy.simulate(policy.order_up_to(bt[m].forecasts, sd, z), bt[m].actuals[:, 0, :], up)
            tuned.append({"policy": f"rmse_ss_{m}", "target_fill": target, "param": z, **res})
            n = policy.tune_cover(cal, up, m, target)
            res = policy.simulate(bt[m].forecasts[:, 0, :] * n, bt[m].actuals[:, 0, :], up)
            tuned.append({"policy": f"cover_{m}", "target_fill": target, "param": n, **res})
    frontiers = {k: [asdict(r) for r in v] for k, v in frontiers.items()}

    # next week's forecast from all data, then allocate it
    y = demand.to_numpy(dtype=float)
    fc = MODELS["ens"](y, 2, up)
    next_week = pd.Series(fc[0], index=demand.columns)
    net = scenario.build(clean, next_week, price["unit_price"])
    total = net.demand.sum()
    plans = [allocate.solve_greedy(net), allocate.solve_milp(net)] + [
        allocate.solve_milp(net, f) for f in (0.6, 0.75)
    ]
    alloc = [p.summary(total) for p in plans]

    # replenishment plan: weeks-of-cover on the ensemble forecast, N tuned on calibration for 90% fill
    n90 = next(t["param"] for t in tuned if t["policy"] == "cover_ens" and t["target_fill"] == 0.90)
    S = fc[0] * n90
    with engine.connect() as c:
        onhand = dict(
            c.execute(
                select(db.inventory.c.sku, func.sum(db.inventory.c.on_hand)).group_by(db.inventory.c.sku)
            ).all()
        )
    rep = pd.DataFrame(
        {
            "sku": demand.columns,
            "forecast_w1": fc[0].round(1),
            "forecast_w2": fc[1].round(1),
            "order_up_to": np.ceil(S),
        }
    )
    rep["on_hand"] = rep["sku"].map(onhand).fillna(0).astype(int)
    rep["order_qty"] = np.maximum(rep["order_up_to"] - rep["on_hand"], 0).astype(int)
    rep.sort_values("order_qty", ascending=False).to_csv(results / "replenishment_plan.csv", index=False)

    out = {
        "data": {
            "weeks": len(demand),
            "skus": demand.shape[1],
            "first_week": str(demand.index[0].date()),
            "last_week": str(demand.index[-1].date()),
            "cleaning": clean_report,
        },
        "origins": {
            "calibration": [str(demand.index[i].date()) for i in (calib_o[0], calib_o[-1])],
            "evaluation": [str(demand.index[i].date()) for i in (eval_o[0], eval_o[-1])],
        },
        "forecast_metrics": fc_metrics,
        "inventory_value_at_fill_rate": at_fill,
        "policy_tuned_on_calibration": tuned,
        "frontiers": frontiers,
        "allocation": {
            "skus": len(net.skus),
            "dcs": net.dcs,
            "regions": net.regions,
            "demand_units": float(total),
            "stock_units": float(net.stock.sum()),
            "results": alloc,
        },
        "replenishment_cover_weeks_for_90pct_fill": n90,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    (results / "planning.json").write_text(
        json.dumps(
            out, indent=1, default=lambda o: None if isinstance(o, float) and math.isnan(o) else str(o)
        )
    )
    return out


def process_inbox(engine, inbox: Path) -> dict:
    t0 = time.perf_counter()
    md = load_master_data(engine)
    manifest = json.loads((inbox / "manifest.json").read_text())
    results = [process_file(engine, inbox / e["file"], md) for e in manifest]
    statuses = pd.Series([r["status"] for r in results]).value_counts().to_dict()
    reasons = pd.Series([x for r in results for x in r["reasons"]]).value_counts().head(10).to_dict()
    return {
        "documents": len(results),
        "status": statuses,
        "top_review_reasons": reasons,
        "seconds": round(time.perf_counter() - t0, 1),
    }


def demo(raw_csv: Path, work: Path, engine_url: str | None, erp_url: str | None) -> dict:
    import httpx
    from fastapi.testclient import TestClient

    from .mock_erp.server import Faults, create_app

    drop, inbox, results = work / "customer_drop", work / "inbox", work / "results"
    summary: dict = {"prepare": prepare_drop(raw_csv, drop, inbox)}
    engine = db.get_engine(engine_url)
    if erp_url:
        client = httpx.Client(base_url=erp_url)
    else:  # in-process mock with the same faults as the compose service
        world = json.loads((drop / "erp_world.json").read_text())
        client = TestClient(create_app(world, Faults(0.15, 0.10, 3, 42)))
    t0 = time.perf_counter()
    with client:
        summary["intake"] = run_intake(drop, engine, client)
    summary["intake_seconds"] = round(time.perf_counter() - t0, 1)
    summary["reconcile"] = reconcile(engine, raw_csv)
    if not summary["reconcile"]["ok"]:
        raise SystemExit("reconciliation failed: not planning on data that does not match the source")
    summary["plan"] = plan(engine, results)
    summary["documents"] = process_inbox(engine, inbox)
    (results / "demo_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary

import json

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from fde import db
from fde.forecast.prepare import clean_sales, weekly_matrix
from fde.intake.adapters import (
    fetch_all,
    load_mapping,
    read_fixed_width,
    read_sales_csv,
    remove_repeated_blocks,
)
from fde.intake.exports import write_inventory_extract, write_sales_exports
from fde.intake.run import canonical_sales, run_intake
from fde.mock_erp.server import Faults, create_app

CFG = load_mapping()["sources"]


def test_repeated_page_removed_but_genuine_duplicates_kept():
    rows = [[str(i)] for i in range(1000)] + [["x"], ["x"]]
    out, removed = remove_repeated_blocks(rows[:500] + rows[300:500] + rows[500:], 50)
    assert removed == 200 and out == rows


def test_sales_csv_roundtrip_reproduces_source(sales_lines, tmp_path):
    paths = write_sales_exports(sales_lines, tmp_path)
    raw = paths[0].read_bytes()
    assert raw.startswith(b"HARBOR & PINE") and b";" in raw
    frames, reports = [], []
    for p in paths:
        df, quarantine, rep = read_sales_csv(p, CFG["sales_orders"])
        assert rep.control_total_ok and not quarantine
        frames.append(df)
        reports.append(rep)
    assert (
        sum(r.duplicates_removed for r in reports) > 0
    )  # the re-exported page (smaller than 400 in this small fixture)
    got = pd.concat(frames, ignore_index=True)
    assert len(got) == len(sales_lines)
    assert got["qty"].sum() == sales_lines["qty"].sum()
    assert (
        abs((got["qty"] * got["unit_price"]).sum() - (sales_lines["qty"] * sales_lines["unit_price"]).sum())
        < 1e-6
    )


def test_truncated_csv_breaks_control_total(sales_lines, tmp_path):
    p = write_sales_exports(sales_lines, tmp_path)[0]
    lines = p.read_bytes().split(b"\r\n")
    p.write_bytes(b"\r\n".join(lines[:20] + lines[-2:]))
    _, _, rep = read_sales_csv(p, CFG["sales_orders"])
    assert rep.control_total_ok is False and "truncated" in rep.notes[-1]


def test_fixed_width_ebcdic(tmp_path):
    snap = pd.DataFrame(
        [
            {
                "sku": "85123A",
                "description": "WHITE HEART",
                "warehouse": "MID",
                "on_hand": 120,
                "unit_cost": 1.4,
            },
            {
                "sku": "22423",
                "description": "CAKESTAND",
                "warehouse": "RTM",
                "on_hand": -3,
                "unit_cost": 6.75,
            },
        ]
    )
    p = write_inventory_extract(snap, tmp_path / "inv.dat", "20111209")
    assert b"\n" not in p.read_bytes() and len(p.read_bytes()) % 68 == 0
    with pytest.raises(ValueError):  # decoding EBCDIC as latin-1 must fail loudly, not load garbage
        read_fixed_width(p, CFG["inventory"] | {"encoding": "latin-1"})
    rows, _, rep = read_fixed_width(p, CFG["inventory"])
    assert rep.control_total_ok and [r["on_hand"] for r in rows] == [120, -3] and rows[1]["unit_cost"] == 6.75


def _world(n=230):
    cat = {
        f"{20000 + i}": {"description": f"Item {i}", "price": 2.0, "pack": 6, "weight_kg": 0.3}
        for i in range(n)
    }
    return {
        "catalog": cat,
        "erp_pos": {"PO-400001": {"supplier": "S", "currency": "GBP", "lines": {"20000": [12, 1.1]}}},
    }


def test_rest_retries_faults_and_schema_drift():
    app = create_app(_world(), Faults(0.25, 0.15, 1, 7))
    with TestClient(app) as cl:
        items, stats, rep = fetch_all(cl, CFG["products"], sleep=lambda s: None)
    assert len(items) == 230 and all("unit_cost" in i and "unitCost" not in i for i in items)
    assert sum(stats.retries.values()) > 0 and "schema drift" in rep.notes[0]


def test_rest_resumes_from_checkpoint(tmp_path):
    ck = tmp_path / "cursor.json"
    app = create_app(_world(), Faults(0.0, 0.0, 99, 0))
    calls = {"n": 0}

    class Flaky(TestClient):
        def get(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise httpx.ConnectError("boom")
            return super().get(*a, **kw)

    with Flaky(app) as cl, pytest.raises(RuntimeError):
        fetch_all(cl, CFG["products"], checkpoint=ck, max_attempts=1, sleep=lambda s: None)
    assert json.loads(ck.read_text())["cursor"]
    with TestClient(app) as cl:
        items, stats, _ = fetch_all(cl, CFG["products"], checkpoint=ck, sleep=lambda s: None)
    assert stats.resumed_from and len(items) == 130 and not ck.exists()


def test_run_intake_is_idempotent_and_reconciles(sales_lines, tmp_path):
    drop = tmp_path / "drop"
    write_sales_exports(sales_lines, drop)
    eng = db.get_engine(f"sqlite:///{tmp_path / 'x.db'}")
    app = create_app(_world(), Faults(0.1, 0.1, 2, 3))
    with TestClient(app) as cl:
        s1 = run_intake(drop, eng, cl)
        s2 = run_intake(drop, eng, cl)
    assert all(r["status"] == "loaded" for r in s1["sales_orders"])
    assert all(r["status"].startswith("skipped") for r in s2["sales_orders"])
    canon = canonical_sales(eng)
    assert len(canon) == len(sales_lines)
    a, _ = weekly_matrix(clean_sales(sales_lines)[0], n_skus=20, min_history_weeks=10)
    b, _ = weekly_matrix(clean_sales(canon)[0], n_skus=20, min_history_weeks=10)
    assert a.equals(b)

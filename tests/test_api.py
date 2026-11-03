import importlib
import json
import random

from fastapi.testclient import TestClient


def test_upload_review_metrics(tmp_path, monkeypatch, catalog):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    from fde import db
    from fde.api import app as app_mod
    from fde.docs.generator import World, make_invoice
    from fde.docs.render import render
    from fde.docs.styles import DEV_STYLES

    app_mod = importlib.reload(app_mod)
    world = World(catalog)
    style = DEV_STYLES[0]
    truth = make_invoice(world, style, random.Random(1), 1, None)
    pdf = tmp_path / "inv.pdf"
    render(truth, style, pdf, random.Random(1))
    # load ERP master data the invoice should match
    db.upsert(
        app_mod.engine,
        db.products,
        [
            {
                "sku": k,
                "description": v.description,
                "list_price": v.price,
                "case_pack": v.pack,
                "weight_kg": v.weight_kg,
            }
            for k, v in catalog.items()
        ],
        ["sku"],
    )
    db.upsert(
        app_mod.engine,
        db.purchase_orders,
        [
            {
                "po_number": p.po_number,
                "supplier": p.supplier,
                "currency": p.currency,
                "lines": {s: list(q) for s, q in p.lines.items()},
            }
            for p in world.erp_pos.values()
        ],
        ["po_number"],
    )

    c = TestClient(app_mod.app)
    assert c.get("/health").json()["ok"]
    r = c.post("/documents", files={"file": ("inv.pdf", pdf.read_bytes(), "application/pdf")}).json()
    assert r["status"] == "auto_approved", r
    r2 = c.post("/documents", files={"file": ("inv2.pdf", pdf.read_bytes(), "application/pdf")}).json()
    assert r2["status"] == "needs_review" and "duplicate_invoice" in r2["reasons"]
    assert (
        c.post("/documents", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 415
    )
    assert c.post("/documents", files={"file": ("fake.pdf", b"hello", "application/pdf")}).status_code == 415
    queue = c.get("/documents", params={"status": "needs_review"}).json()
    assert [d["id"] for d in queue] == [r2["id"]]
    assert "Review queue (1)" in c.get("/review").text
    assert (
        c.post(
            f"/documents/{r2['id']}/review",
            json={"action": "reject", "corrections": {"doc_number": "BW-dup"}, "reviewer": "sam"},
        ).json()["status"]
        == "rejected"
    )
    m = c.get("/metrics").json()
    assert m["by_status"] == {"auto_approved": 1, "rejected": 1} and m["corrections_by_field"] == {
        "doc_number": 1
    }
    json.dumps(m)

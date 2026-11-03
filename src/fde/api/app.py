"""Review-queue API and a minimal server-rendered reviewer page.

POST /documents                 upload a PDF/text document -> extracted, validated, routed
GET  /documents?status=...      list (needs_review = the queue)
GET  /documents/{id}            extraction, confidences, findings, reasons
POST /documents/{id}/review     {"action": "approve"|"reject", "corrections": {...}, "reviewer": "..."}
GET  /metrics                   straight-through rate, backlog, corrections by field and sender
GET  /plan/replenishment        latest replenishment plan written by the planning job
GET  /review                    HTML queue for a reviewer; /review/{id} shows one document
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from .. import db
from ..docs.service import process_bytes, review

MAX_UPLOAD = 10 * 1024 * 1024
ALLOWED = {".pdf", ".txt", ".eml"}

engine = db.get_engine()
app = FastAPI(title="FDE supply-chain platform")
RESULTS = Path(os.environ.get("RESULTS_DIR", "artifacts/results"))


class ReviewIn(BaseModel):
    action: str = "approve"
    corrections: dict = {}
    reviewer: str = "reviewer"


@app.get("/health")
def health():
    with engine.connect() as c:
        c.execute(select(1))
    return {"ok": True}


@app.post("/documents")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "upload.txt").name
    if Path(name).suffix.lower() not in ALLOWED:
        raise HTTPException(415, f"unsupported file type; allowed: {sorted(ALLOWED)}")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large")
    if Path(name).suffix.lower() == ".pdf" and not data.startswith(b"%PDF"):
        raise HTTPException(415, "not a PDF")
    return process_bytes(engine, data, name)


@app.get("/documents")
def list_documents(status: str | None = None, limit: int = 100):
    q = (
        select(
            db.documents.c.id,
            db.documents.c.filename,
            db.documents.c.doc_type,
            db.documents.c.sender,
            db.documents.c.doc_number,
            db.documents.c.status,
            db.documents.c.reasons,
        )
        .order_by(db.documents.c.id.desc())
        .limit(min(limit, 500))
    )
    if status:
        q = q.where(db.documents.c.status == status)
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(q)]


@app.get("/documents/{doc_id}")
def get_document(doc_id: int):
    with engine.connect() as c:
        row = c.execute(select(db.documents).where(db.documents.c.id == doc_id)).mappings().first()
    if row is None:
        raise HTTPException(404)
    return dict(row)


@app.post("/documents/{doc_id}/review")
def post_review(doc_id: int, body: ReviewIn):
    if body.action not in ("approve", "reject"):
        raise HTTPException(422, "action must be approve or reject")
    try:
        return review(engine, doc_id, body.action, body.corrections, body.reviewer)
    except KeyError:
        raise HTTPException(404) from None


@app.get("/metrics")
def get_metrics():
    with engine.connect() as c:
        by_status = dict(
            c.execute(select(db.documents.c.status, func.count()).group_by(db.documents.c.status)).all()
        )
        by_field = dict(
            c.execute(select(db.corrections.c.field, func.count()).group_by(db.corrections.c.field)).all()
        )
        by_sender = dict(
            c.execute(select(db.corrections.c.sender, func.count()).group_by(db.corrections.c.sender)).all()
        )
    total = sum(by_status.values())
    return {
        "documents": total,
        "by_status": by_status,
        "straight_through_rate": round(by_status.get("auto_approved", 0) / total, 4) if total else None,
        "review_backlog": by_status.get("needs_review", 0),
        "corrections_by_field": by_field,
        "corrections_by_sender": by_sender,
    }


@app.get("/plan/replenishment")
def replenishment(limit: int = 50):
    path = RESULTS / "replenishment_plan.csv"
    if not path.exists():
        raise HTTPException(404, "no plan yet; run `fde demo`")
    import pandas as pd

    return pd.read_csv(path).head(limit).to_dict("records")


# --------------------------------------------------------------------------- reviewer HTML

STYLE = """<style>body{font:14px system-ui,sans-serif;margin:24px;max-width:1100px;color:#1d2433;background:#fafaf7}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#eef0ea}.r{color:#9a3412}input{font:inherit;padding:3px 6px;width:240px}button{font:inherit;padding:6px 14px}
code{background:#eef0ea;padding:1px 4px}</style>"""


@app.get("/review", response_class=HTMLResponse)
def review_queue():
    rows = list_documents(status="needs_review", limit=200)
    trs = "".join(
        f"<tr><td><a href='/review/{r['id']}'>{r['id']}</a></td><td>{html.escape(r['filename'] or '')}</td><td>{r['doc_type']}</td>"
        f"<td>{html.escape(r['sender'] or '')}</td><td class=r>{html.escape(', '.join(r['reasons'] or []))}</td></tr>"
        for r in rows
    )
    return f"<!doctype html><title>Review queue</title>{STYLE}<h1>Review queue ({len(rows)})</h1><table><tr><th>#</th><th>File</th><th>Type</th><th>Sender</th><th>Why it is here</th></tr>{trs}</table>"


EDITABLE = ["doc_number", "doc_date", "due_date", "po_number", "subtotal", "tax", "total"]


@app.get("/review/{doc_id}", response_class=HTMLResponse)
def review_page(doc_id: int):
    d = get_document(doc_id)
    ex, conf = d["extracted"], d["confidence"] or {}
    fields = "".join(
        f"<tr><td>{f}</td><td><input name='{f}' value='{html.escape(str(ex.get(f) or ''))}'></td><td>{conf.get(f, '')}</td></tr>"
        for f in EDITABLE
        if f in ex
    )
    lines = "".join(
        f"<tr><td>{html.escape(str(li['sku']))}</td><td>{html.escape(str(li.get('description') or ''))}</td><td>{li['qty']}</td><td>{li.get('unit_price')}</td><td>{li.get('amount')}</td></tr>"
        for li in ex.get("lines", [])
    )
    findings = "".join(
        f"<li class=r><code>{html.escape(f['code'])}</code> {html.escape(f['message'])}</li>"
        for f in d["findings"] or []
    )
    return (
        f"<!doctype html><title>Document {doc_id}</title>{STYLE}<p><a href='/review'>&larr; queue</a></p>"
        f"<h1>{html.escape(d['filename'] or '')}</h1><p>{d['doc_type']} from <b>{html.escape(d['sender'] or '?')}</b> &middot; status <b>{d['status']}</b></p>"
        f"<h2>Findings</h2><ul>{findings or '<li>none</li>'}</ul><p>Reasons: {html.escape(', '.join(d['reasons'] or []))}</p>"
        f"<form method=post action='/review/{doc_id}'><h2>Header fields</h2><table><tr><th>Field</th><th>Value</th><th>Confidence</th></tr>{fields}</table>"
        f"<p><input name=reviewer value='reviewer'> <button name=action value=approve>Approve</button> <button name=action value=reject>Reject</button></p></form>"
        f"<h2>Lines</h2><table><tr><th>SKU</th><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th></tr>{lines}</table>"
    )


@app.post("/review/{doc_id}")
def review_form(
    doc_id: int,
    action: str = Form(...),
    reviewer: str = Form("reviewer"),
    doc_number: str = Form(None),
    doc_date: str = Form(None),
    due_date: str = Form(None),
    po_number: str = Form(None),
    subtotal: str = Form(None),
    tax: str = Form(None),
    total: str = Form(None),
):
    d = get_document(doc_id)["extracted"]
    submitted = {
        "doc_number": doc_number,
        "doc_date": doc_date,
        "due_date": due_date,
        "po_number": po_number,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }
    corrections = {}
    for k, v in submitted.items():
        if v is None or k not in d:
            continue
        old = "" if d.get(k) is None else str(d.get(k))
        if v != old:
            corrections[k] = (
                json.loads(v)
                if k in ("subtotal", "tax", "total") and v.replace(".", "", 1).isdigit()
                else (v or None)
            )
    review(engine, doc_id, action, corrections, reviewer)
    return RedirectResponse("/review", status_code=303)

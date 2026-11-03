"""Document processing as a service: extract -> validate against ERP data in the DB -> route -> store.

Master data (catalogue, purchase orders, previously seen documents, sender date profiles) is read
from the database the intake jobs filled, so the documents flow uses exactly what the ERP said.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import insert, select, update

from .. import db
from .extract import Extraction, RuleBasedExtractor
from .holdout_styles import HOLDOUT_STYLES
from .styles import DEV_STYLES
from .validate import MasterData, route, validate

# The customer's vendor/customer master: every sender is known to the ERP before their documents arrive.
VENDOR_MASTER = sorted({s.sender for s in DEV_STYLES + HOLDOUT_STYLES})


def load_master_data(engine) -> MasterData:
    with engine.connect() as c:
        catalog = {
            r.sku: {
                "description": r.description,
                "price": r.list_price,
                "pack": r.case_pack,
                "weight_kg": r.weight_kg,
            }
            for r in c.execute(select(db.products))
        }
        pos = {
            r.po_number: {"supplier": r.supplier, "currency": r.currency, "lines": r.lines}
            for r in c.execute(select(db.purchase_orders))
        }
        seen = {
            (r.doc_type, r.sender, r.doc_number)
            for r in c.execute(
                select(db.documents.c.doc_type, db.documents.c.sender, db.documents.c.doc_number)
            )
            if r.doc_number
        }
    md = MasterData(catalog=catalog, erp_pos=pos)
    md.seen_documents = seen
    return md


def _profiles(engine) -> dict[str, str]:
    with engine.connect() as c:
        return {r.sender: r.date_order for r in c.execute(select(db.sender_profiles))}


def _save_profiles(engine, before: dict, after: dict) -> None:
    new = [{"sender": k, "date_order": v} for k, v in after.items() if k not in before]
    db.upsert(engine, db.sender_profiles, new, ["sender"])


def _jsonable(ex: Extraction) -> dict:
    return json.loads(ex.doc.model_dump_json())


def process_file(engine, path: Path, md: MasterData | None = None, filename: str | None = None) -> dict:
    md = md or load_master_data(engine)
    profiles = _profiles(engine)
    extractor = RuleBasedExtractor(VENDOR_MASTER, dict(profiles))
    ex = extractor.extract(path)
    _save_profiles(engine, profiles, extractor.date_profiles)
    findings = validate(ex, md)
    decision = route(ex, findings)
    status = "auto_approved" if decision.route == "auto_approve" else "needs_review"
    extracted = _jsonable(ex)
    row = {
        "filename": filename or path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "doc_type": ex.doc.doc_type,
        "sender": ex.doc.sender,
        "doc_number": ex.doc.doc_number,
        "status": status,
        "extracted": extracted,
        "confidence": ex.confidence | {"lines": ex.line_confidence},
        "findings": [vars(f) for f in findings],
        "reasons": decision.reasons,
        "final": extracted if status == "auto_approved" else None,
    }
    with engine.begin() as c:
        doc_id = c.execute(insert(db.documents).values(**row).returning(db.documents.c.id)).scalar_one()
    if ex.doc.doc_number and ex.doc.sender:
        md.seen_documents.add((ex.doc.doc_type, ex.doc.sender, ex.doc.doc_number))
    return {
        "id": doc_id,
        **{k: row[k] for k in ("filename", "doc_type", "sender", "doc_number", "status", "reasons")},
    }


def process_bytes(engine, data: bytes, filename: str) -> dict:
    suffix = Path(filename).suffix.lower() or ".txt"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(data)
        tmp.flush()
        return process_file(engine, Path(tmp.name), filename=filename)


def review(engine, doc_id: int, action: str, corrections: dict, reviewer: str) -> dict:
    """Apply a reviewer's decision. Each corrected field is logged: the corrections table is the
    onboarding backlog (which labels/layouts to teach the extractor next)."""
    with engine.begin() as c:
        row = c.execute(select(db.documents).where(db.documents.c.id == doc_id)).mappings().first()
        if row is None:
            raise KeyError(doc_id)
        final = dict(row["extracted"])
        for fld, value in corrections.items():
            old = final.get(fld)
            if str(old) != str(value):
                c.execute(
                    insert(db.corrections).values(
                        document_id=doc_id,
                        sender=row["sender"],
                        field=fld,
                        extracted_value=json.dumps(old, default=str),
                        corrected_value=json.dumps(value, default=str),
                    )
                )
            final[fld] = value
        status = {"approve": "approved", "reject": "rejected"}[action]
        c.execute(
            update(db.documents)
            .where(db.documents.c.id == doc_id)
            .values(status=status, final=final, reviewer=reviewer, reviewed_at=datetime.utcnow())
        )
    return {"id": doc_id, "status": status, "corrected_fields": sorted(corrections)}

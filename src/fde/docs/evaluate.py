"""Score extraction against ground truth: per-field accuracy, line-item F1 and whole-document accuracy."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from .extract import Extraction
from .schemas import HEADER_FIELDS


def _eq(fld: str, pred, truth) -> bool:
    if truth is None:
        return pred is None
    if pred is None:
        return False
    if fld in ("doc_date", "due_date"):
        return (pred.isoformat() if isinstance(pred, date) else str(pred)) == truth
    if isinstance(truth, float | int):
        return abs(float(pred) - float(truth)) <= 0.011
    return str(pred).strip() == str(truth).strip()


def line_key(li: dict) -> tuple:
    return (
        li["sku"],
        round(float(li["qty"]), 2),
        None if li.get("unit_price") is None else round(float(li["unit_price"]), 2),
        li.get("cartons"),
    )


def score_one(ex: Extraction, truth: dict) -> dict:
    doc = ex.doc.model_dump()
    out = {"doc_type_ok": doc["doc_type"] == truth["doc_type"], "fields": {}}
    for fld in HEADER_FIELDS[truth["doc_type"]]:
        out["fields"][fld] = _eq(fld, doc.get(fld), truth.get(fld))
    pred_lines = [line_key(li) for li in doc["lines"]]
    true_lines = [line_key(li) for li in truth["lines"]]
    tp = 0
    remaining = list(true_lines)
    for p in pred_lines:
        if p in remaining:
            remaining.remove(p)
            tp += 1
    out["line_tp"], out["line_pred"], out["line_true"] = tp, len(pred_lines), len(true_lines)
    out["doc_correct"] = (
        out["doc_type_ok"] and all(out["fields"].values()) and tp == len(true_lines) == len(pred_lines)
    )
    return out


def aggregate(scores: list[dict]) -> dict:
    fields = defaultdict(list)
    for s in scores:
        for f, ok in s["fields"].items():
            fields[f].append(ok)
    tp = sum(s["line_tp"] for s in scores)
    npred = sum(s["line_pred"] for s in scores)
    ntrue = sum(s["line_true"] for s in scores)
    p = tp / npred if npred else 0.0
    r = tp / ntrue if ntrue else 0.0
    all_fields = [ok for v in fields.values() for ok in v]
    return {
        "documents": len(scores),
        "doc_type_accuracy": sum(s["doc_type_ok"] for s in scores) / len(scores),
        "header_field_accuracy": sum(all_fields) / len(all_fields),
        "field_accuracy": {f: round(sum(v) / len(v), 4) for f, v in sorted(fields.items())},
        "line_precision": round(p, 4),
        "line_recall": round(r, 4),
        "line_f1": round(2 * p * r / (p + r), 4) if p + r else 0.0,
        "document_exact_match": sum(s["doc_correct"] for s in scores) / len(scores),
    }


def load_truth(path: Path) -> dict:
    return json.loads(path.with_name(path.stem + ".truth.json").read_text())

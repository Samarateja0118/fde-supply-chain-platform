"""End-to-end document benchmark: generate -> extract -> validate -> route, then score.

Reported per split (dev layouts vs held-out layouts):
* extraction: header field accuracy, line-item F1, whole-document exact match
* validation: recall of each injected business issue, and false alarms on clean documents
* routing: straight-through rate, and the error rate among auto-approved documents
  (an auto-approved document is "bad" if extraction got anything wrong or it carried an issue).
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from .evaluate import aggregate, load_truth, score_one
from .extract import RuleBasedExtractor
from .generator import World, generate
from .styles import Style
from .validate import MasterData, route, validate

# Injected issue -> finding codes that count as catching it.
CATCHES = {
    "unknown_po": {"unknown_po"},
    "price_variance": {"price_variance", "line_math"},
    "qty_exceeds_po": {"qty_exceeds_po"},
    "total_mismatch": {"total_mismatch"},
    "due_before_issue": {"due_before_issue"},
    "duplicate_invoice": {"duplicate_invoice"},
    "carton_mismatch": {"carton_mismatch"},
    "unknown_sku": {"unknown_sku"},
    "price_mismatch_vs_list": {"price_mismatch_vs_list"},
    "qty_not_case_multiple": {"qty_not_case_multiple"},
}


def run(
    catalog: dict, styles: list[Style], known_senders: list[str], n_docs: int, out_dir: Path, seed: int
) -> dict:
    world = World(catalog)
    manifest = generate(world, styles, n_docs, out_dir, seed=seed)
    md = MasterData.from_world_json(json.loads(json.dumps(world.to_json())))
    extractor = RuleBasedExtractor(known_senders)

    scores, per_style = [], defaultdict(list)
    caught, injected = Counter(), Counter()
    clean_docs = clean_flagged = 0
    routes = Counter()
    auto_bad = 0
    review_reasons = Counter()
    rows = []
    t0 = time.perf_counter()
    for entry in manifest:
        path = out_dir / entry["file"]
        truth = load_truth(path)
        ex = extractor.extract(path)
        s = score_one(ex, truth)
        findings = validate(ex, md)
        if ex.doc.doc_number and ex.doc.sender:
            md.seen_documents.add((ex.doc.doc_type, ex.doc.sender, ex.doc.doc_number))
        decision = route(ex, findings)
        codes = {f.code for f in findings}
        scores.append(s)
        per_style[f"{entry['style']}/{entry['format']}"].append(s)
        for issue in truth["issues"]:
            injected[issue] += 1
            if codes & CATCHES[issue]:
                caught[issue] += 1
        if not truth["issues"]:
            clean_docs += 1
            if decision.route == "review":
                clean_flagged += 1
        routes[decision.route] += 1
        for r in decision.reasons:
            review_reasons[r.split(":")[0] if r.startswith("warning") else r] += 1
        if decision.route == "auto_approve" and (truth["issues"] or not s["doc_correct"]):
            auto_bad += 1
        rows.append(
            {
                "file": entry["file"],
                "route": decision.route,
                "reasons": decision.reasons,
                "doc_correct": s["doc_correct"],
                "issues": truth["issues"],
            }
        )
    seconds = time.perf_counter() - t0

    agg = aggregate(scores)
    n = len(manifest)
    # Documents that are clean AND extracted perfectly are the ones that *should* flow straight through.
    should_pass = sum(1 for r in rows if not r["issues"] and r["doc_correct"])
    passed_ok = sum(1 for r in rows if r["route"] == "auto_approve" and not r["issues"] and r["doc_correct"])
    return {
        "documents": n,
        "seconds": round(seconds, 2),
        "ms_per_doc": round(1000 * seconds / n, 1),
        "extraction": agg,
        "per_layout": {
            k: {
                "n": len(v),
                **{
                    m: round(aggregate(v)[m], 4)
                    for m in ("header_field_accuracy", "line_f1", "document_exact_match")
                },
            }
            for k, v in sorted(per_style.items())
        },
        "issue_recall": {k: f"{caught[k]}/{injected[k]}" for k in sorted(injected)},
        "issue_recall_total": round(sum(caught.values()) / max(sum(injected.values()), 1), 4),
        "clean_docs_sent_to_review": f"{clean_flagged}/{clean_docs}",
        "straight_through_rate": round(routes["auto_approve"] / n, 4),
        "straight_through_of_eligible": round(passed_ok / max(should_pass, 1), 4),
        "auto_approved_with_error": f"{auto_bad}/{routes['auto_approve']}",
        "review_reasons": dict(review_reasons.most_common(12)),
        "rows": rows,
    }

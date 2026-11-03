"""Command line entry point: ``fde <command>``."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_RAW = Path("data/raw/online-retail.csv")


def fetch_data(dest: Path = DEFAULT_RAW) -> Path:
    from .pipeline import RAW_SHA256, RAW_URL

    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == RAW_SHA256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {RAW_URL}", file=sys.stderr)
    urllib.request.urlretrieve(RAW_URL, dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != RAW_SHA256:
        raise SystemExit(f"checksum mismatch for {dest}: {digest}")
    return dest


def _catalog(raw: Path):
    from .docs.generator import catalog_from_sales
    from .forecast.prepare import clean_sales, load_raw_online_retail

    clean, _ = clean_sales(load_raw_online_retail(str(raw)))
    return catalog_from_sales(clean)


def cmd_docs_benchmark(args) -> None:
    from .docs.benchmark import run
    from .docs.holdout_styles import HOLDOUT_STYLES
    from .docs.service import VENDOR_MASTER
    from .docs.styles import DEV_STYLES

    styles = {"dev": DEV_STYLES, "holdout": HOLDOUT_STYLES}[args.split]
    res = run(
        _catalog(fetch_data(args.raw)),
        styles,
        VENDOR_MASTER,
        args.n,
        Path(args.out) / args.split,
        seed=args.seed,
    )
    rows = res.pop("rows")
    Path(args.results).mkdir(parents=True, exist_ok=True)
    out = Path(args.results) / f"docs_{args.split}_seed{args.seed}.json"
    out.write_text(json.dumps(res | {"seed": args.seed}, indent=1, default=str))
    (Path(args.results) / f"docs_{args.split}_seed{args.seed}_rows.json").write_text(
        json.dumps(rows, indent=0)
    )
    print(
        json.dumps(
            {
                k: res[k]
                for k in (
                    "documents",
                    "issue_recall_total",
                    "straight_through_rate",
                    "auto_approved_with_error",
                    "clean_docs_sent_to_review",
                )
            }
            | {
                "exact_match": res["extraction"]["document_exact_match"],
                "field_acc": res["extraction"]["header_field_accuracy"],
                "line_f1": res["extraction"]["line_f1"],
            },
            indent=1,
        )
    )
    print(f"wrote {out}")


def cmd_demo(args) -> None:
    from .pipeline import demo

    s = demo(fetch_data(args.raw), Path(args.work), args.database_url, args.erp_url)
    p = s["plan"]
    fm = {m["model"]: m["wape_h1"] for m in p["forecast_metrics"]}
    print("\n=== demo summary ===")
    print(
        f"intake: {sum(r.get('rows_loaded', 0) for r in s['intake']['sales_orders'])} sales lines from {len(s['intake']['sales_orders'])} files, "
        f"{sum(r.get('duplicates_removed', 0) for r in s['intake']['sales_orders'])} repeated-page rows removed"
    )
    for name, rest in s["intake"]["rest"].items():
        print(
            f"  REST {name}: {rest['rows_loaded']} records, {rest['requests']} requests, retries {rest['retries']}"
        )
    print(f"reconcile: {s['reconcile']}")
    print("forecast WAPE (h=1): " + ", ".join(f"{k} {v:.3f}" for k, v in fm.items()))
    print(
        f"allocation: {[(r['method'], r['total_cost'], r['min_region_fill']) for r in p['allocation']['results']]}"
    )
    print(f"documents: {s['documents']}")
    print(f"results in {Path(args.work) / 'results'}")


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run("fde.api.app:app", host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="fde")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fetch-data", help="download the UCI Online Retail CSV (checksummed)")
    s.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    s.set_defaults(func=lambda a: print(fetch_data(a.raw)))

    s = sub.add_parser("demo", help="run the whole customer deployment end to end")
    s.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    s.add_argument("--work", default="artifacts")
    s.add_argument("--database-url", default=None)
    s.add_argument("--erp-url", default=None, help="base URL of the ERP API; default: in-process mock")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("docs-benchmark", help="score extraction/validation/routing on generated documents")
    s.add_argument("--split", choices=["dev", "holdout"], default="holdout")
    s.add_argument("--n", type=int, default=400)
    s.add_argument("--seed", type=int, default=4)
    s.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    s.add_argument("--out", default="data/generated")
    s.add_argument("--results", default="results")
    s.set_defaults(func=cmd_docs_benchmark)

    s = sub.add_parser("serve", help="run the review-queue API")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

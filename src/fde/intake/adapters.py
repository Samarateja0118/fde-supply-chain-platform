"""Intake adapters: messy CSV report exports, EBCDIC fixed-width extracts, and a flaky REST API.

Each adapter returns canonical rows plus a data-quality report. Rows that cannot be parsed are not
silently dropped: they go to a quarantine list with the reason, and the report's control totals
must reconcile (rows read = loaded + rejected + removed duplicates = trailer count).
"""

from __future__ import annotations

import csv
import io
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd
import yaml

from .exports import FIXED_WIDTH

MAPPINGS = Path(__file__).parent / "mappings"


def load_mapping(customer: str = "harbor_pine") -> dict:
    return yaml.safe_load((MAPPINGS / f"{customer}.yaml").read_text())


@dataclass
class Report:
    source: str
    file: str | None = None
    rows_read: int = 0
    rows_loaded: int = 0
    duplicates_removed: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    control_total_expected: int | None = None
    control_total_ok: bool | None = None
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return dict(vars(self))


# --------------------------------------------------------------------------- CSV report export


def decode(raw: bytes, configured: str, report: Report) -> str:
    """Strict UTF-8 first (files get re-saved by people), then the configured legacy encoding."""
    if raw.startswith(b"\xef\xbb\xbf"):
        report.notes.append("UTF-8 BOM found; decoded as utf-8-sig")
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        report.notes.append(f"not UTF-8 (byte 0x{raw[e.start]:02x} at {e.start}); decoded as {configured}")
        return raw.decode(configured)


def remove_repeated_blocks(
    rows: list[list[str]], min_len: int, window: int = 20000
) -> tuple[list[list[str]], int]:
    """Drop runs of >= min_len consecutive rows that exactly repeat an earlier run in the file.

    Row-level dedupe would be wrong here: the source has genuine identical lines (same customer
    buying the same item twice on one invoice), so only whole repeated pages are removed.
    """
    keys = ["\x1f".join(r) for r in rows]
    out: list[str] = []
    seen: dict[str, list[int]] = {}
    removed = 0
    i = 0
    while i < len(keys):
        run = 0
        for pos in reversed(seen.get(keys[i], [])):
            if len(out) - pos > window:
                break
            if (
                i + min_len <= len(keys)
                and pos + min_len <= len(out)
                and out[pos : pos + min_len] == keys[i : i + min_len]
            ):
                run = min_len
                while i + run < len(keys) and pos + run < len(out) and keys[i + run] == out[pos + run]:
                    run += 1
                break
        if run:
            removed += run
            i += run
            continue
        seen.setdefault(keys[i], []).append(len(out))
        out.append(keys[i])
        i += 1
    return [k.split("\x1f") for k in out], removed


def read_sales_csv(path: Path, cfg: dict) -> tuple[pd.DataFrame, list[dict], Report]:
    t0 = time.perf_counter()
    rep = Report("sales_orders", path.name)
    text = decode(path.read_bytes(), cfg["encoding"], rep)
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(cfg["header_starts_with"]))
    if start:
        rep.notes.append(f"skipped {start} preamble line(s)")
    reader = csv.reader(io.StringIO("\n".join(lines[start:])), delimiter=cfg["delimiter"], quotechar='"')
    header = next(reader)
    cols = cfg["columns"]
    missing = [v for v in cols.values() if v not in header]
    if missing:
        raise ValueError(f"{path.name}: expected columns not found: {missing}; got {header}")
    idx = {k: header.index(v) for k, v in cols.items()}
    body = []
    for row in reader:
        if not row:
            continue
        if row[0].startswith(cfg["trailer_starts_with"]):
            rep.control_total_expected = int(row[1])
            continue
        body.append(row)
    rep.rows_read = len(body)
    body, rep.duplicates_removed = remove_repeated_blocks(body, cfg.get("repeated_block_min_rows", 50))

    out, quarantine = [], []
    dec = cfg.get("decimal", ".")
    for row in body:
        if len(row) != len(header):
            rep.reject("wrong_field_count")
            quarantine.append({"row": row, "reason": "wrong_field_count"})
            continue
        try:
            ts = datetime.strptime(row[idx["order_ts"]], cfg["datetime_format"])
        except ValueError:
            rep.reject("bad_datetime")
            quarantine.append({"row": row, "reason": "bad_datetime"})
            continue
        try:
            qty = float(row[idx["qty"]])
            price = float(
                row[idx["unit_price"]].replace(".", "").replace(dec, ".")
                if dec == ","
                else row[idx["unit_price"]]
            )
        except ValueError:
            rep.reject("bad_number")
            quarantine.append({"row": row, "reason": "bad_number"})
            continue
        sku = row[idx["sku"]].strip().upper()
        if not sku:
            rep.reject("missing_sku")
            quarantine.append({"row": row, "reason": "missing_sku"})
            continue
        out.append(
            {
                "order_id": row[idx["order_id"]].strip(),
                "sku": sku,
                "description": row[idx["description"]] or None,
                "qty": qty,
                "order_ts": ts,
                "unit_price": price,
                "customer_id": row[idx["customer_id"]] or None,
                "country": row[idx["country"]],
                "source_file": path.name,
            }
        )
    rep.rows_loaded = len(out)
    if rep.control_total_expected is not None:
        rep.control_total_ok = rep.control_total_expected == rep.rows_read
        if not rep.control_total_ok:
            rep.notes.append(
                f"trailer says {rep.control_total_expected} rows, file has {rep.rows_read}: truncated transfer?"
            )
    rep.seconds = round(time.perf_counter() - t0, 3)
    return pd.DataFrame(out), quarantine, rep


# --------------------------------------------------------------------------- fixed-width EBCDIC


def read_fixed_width(path: Path, cfg: dict) -> tuple[list[dict], list[dict], Report]:
    t0 = time.perf_counter()
    rep = Report("inventory", path.name)
    raw = path.read_bytes()
    n = cfg["record_length"]
    if len(raw) % n:
        rep.notes.append(f"file length {len(raw)} is not a multiple of {n}: partial last record")
    text = raw.decode(cfg["encoding"])
    records = [text[i : i + n] for i in range(0, len(text) - n + 1, n)]
    if not records or not records[0].startswith("HDR"):
        raise ValueError(
            f"{path.name}: no HDR record; wrong encoding or record length? first bytes {raw[:12]!r}"
        )
    out, quarantine = [], []
    trailer = None
    dec = cfg.get("implied_decimals", {})
    for rec in records[1:]:
        kind = rec[:3]
        if kind == "TRL":
            trailer = rec
            continue
        if kind != "DTL":
            rep.reject("unknown_record_type")
            continue
        rep.rows_read += 1
        f = {name: rec[s - 1 : s - 1 + ln] for name, s, ln in FIXED_WIDTH}
        try:
            qty = int(f["on_hand"][:8]) * (-1 if f["on_hand"][8] == "-" else 1)
            cost = int(f["unit_cost"]) / 10 ** dec.get("unit_cost", 0)
        except ValueError:
            rep.reject("bad_number")
            quarantine.append({"record": rec, "reason": "bad_number"})
            continue
        if qty < 0:
            rep.notes.append(
                f"negative on-hand for {f['sku'].strip()} @ {f['warehouse']}: kept, flagged for cycle count"
            )
        out.append(
            {
                "sku": f["sku"].strip(),
                "warehouse": f["warehouse"].strip(),
                "description": f["description"].strip(),
                "on_hand": qty,
                "unit_cost": cost,
                "as_of": f["last_count"],
            }
        )
    if trailer:
        count, hash_total = int(trailer[3:12]), int(trailer[12:27])
        rep.control_total_expected = count
        got_hash = sum(r["on_hand"] for r in out)
        rep.control_total_ok = count == rep.rows_read and hash_total == got_hash
        if hash_total != got_hash:
            rep.notes.append(f"hash total mismatch: trailer {hash_total}, records {got_hash}")
    else:
        rep.notes.append("no TRL record: file truncated")
        rep.control_total_ok = False
    rep.rows_loaded = len(out)
    rep.seconds = round(time.perf_counter() - t0, 3)
    return out, quarantine, rep


# --------------------------------------------------------------------------- REST with retries


@dataclass
class RestStats:
    requests: int = 0
    pages: int = 0
    retries: dict[str, int] = field(default_factory=dict)
    schema_variants: dict[str, int] = field(default_factory=dict)
    resumed_from: str | None = None


def resolve_aliases(item: dict, aliases: dict[str, list[str]], stats: RestStats) -> dict:
    out = dict(item)
    for canonical, names in aliases.items():
        for nm in names:
            if nm in item:
                if nm != canonical:
                    out[canonical] = out.pop(nm)
                stats.schema_variants[nm] = stats.schema_variants.get(nm, 0) + 1
                break
    return out


def fetch_all(
    client: httpx.Client,
    cfg: dict,
    checkpoint: Path | None = None,
    max_attempts: int = 8,
    base_delay: float = 0.2,
    max_delay: float = 5.0,
    sleep=time.sleep,
) -> tuple[list[dict], RestStats, Report]:
    """Cursor-paginated GET with retry on 429 (honours Retry-After) and 5xx/timeouts (exponential
    backoff with full jitter). The cursor is checkpointed after every page, so a crashed run resumes
    where it stopped; records are keyed, so a page fetched twice upserts to the same rows."""
    t0 = time.perf_counter()
    stats, rep = RestStats(), Report(cfg["path"])
    cursor = None
    if checkpoint and checkpoint.exists():
        cursor = json.loads(checkpoint.read_text()).get("cursor")
        stats.resumed_from = cursor
    records: dict[str, dict] = {}
    rng = random.Random(0)
    while True:
        params = {"limit": cfg["page_size"]}
        if cursor:
            params["cursor"] = cursor
        for attempt in range(1, max_attempts + 1):
            stats.requests += 1
            try:
                resp = client.get(cfg["path"], params=params, timeout=10)
            except httpx.TransportError as e:
                code = type(e).__name__
                resp = None
            else:
                code = str(resp.status_code)
                if resp.status_code == 200:
                    break
                if resp.status_code < 500 and resp.status_code != 429:
                    resp.raise_for_status()
            stats.retries[code] = stats.retries.get(code, 0) + 1
            if attempt == max_attempts:
                raise RuntimeError(
                    f"{cfg['path']}: gave up after {attempt} attempts (last {code}); cursor checkpointed at {cursor}"
                )
            retry_after = resp.headers.get("Retry-After") if resp is not None else None
            delay = (
                float(retry_after)
                if retry_after
                else rng.uniform(0, min(max_delay, base_delay * 2 ** (attempt - 1)))
            )
            sleep(delay)
        body = resp.json()
        stats.pages += 1
        for item in body["items"]:
            item = resolve_aliases(item, cfg.get("field_aliases", {}), stats)
            records[str(item[cfg["key"]])] = item
        cursor = body.get("next_cursor")
        if checkpoint:
            checkpoint.write_text(json.dumps({"cursor": cursor}))
        if not cursor:
            break
    if checkpoint and checkpoint.exists():
        checkpoint.unlink()
    rep.rows_read = rep.rows_loaded = len(records)
    rep.seconds = round(time.perf_counter() - t0, 3)
    variants = [k for k in stats.schema_variants if k not in cfg.get("field_aliases", {})]
    if variants:
        rep.notes.append(f"schema drift: fields served under new names {sorted(variants)}")
    return list(records.values()), stats, rep

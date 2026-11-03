# Customer deployment runbook: Harbor & Pine Home Goods

Written as the runbook I would hand to the next engineer on this account. The customer is
fictional; every failure mode in the troubleshooting section is reproduced by this repo's fixtures
or tests, so the fixes below are tested, not guessed.

**Scope.** Three things go live: (1) sales history intake that feeds weekly demand forecasts and a
replenishment plan, (2) an AP and order-entry document inbox (supplier invoices, carrier bills of
lading, customer purchase orders) with three-way matching and a human review queue, (3) a weekly
allocation of DC stock to regions.

**Source systems.**

| Source | How it arrives | Owner on their side | Adapter |
|---|---|---|---|
| ERP sales line report | Monthly CSV on SFTP, cp1252, `;`, decimal comma, report preamble + trailer | ERP admin | `read_sales_csv` |
| Mainframe inventory snapshot | Nightly fixed-width EBCDIC (cp037), 68-byte records, no newlines | Warehouse IT | `read_fixed_width` |
| ERP REST API (products, purchase orders) | Cursor-paginated JSON, rate limited, renamed fields mid-stream in v2 | ERP vendor | `fetch_all` |
| Document inbox | PDFs and pasted email bodies from ~11 senders | AP team, customer service | `RuleBasedExtractor` |

All customer-specific settings live in `src/fde/intake/mappings/harbor_pine.yaml`. Code changes
should never be needed to onboard a new export column or an encoding change.

---

## Phase 0: discovery (week 1)

- [ ] Get **sample files from production**, not a dev export. Ask for one month that included a
      known incident (system outage, re-run batch); those months carry the weird cases.
- [ ] For each file, record: encoding (ask; then verify with `file -i` and by decoding), delimiter,
      decimal separator, date format, whether there is a preamble/trailer, and who can re-run it.
- [ ] Get the **control totals** they trust: row counts and quantity sums per month from the ERP
      screen they already use. Reconciliation is against those, not against my own reading.
- [ ] Walk the AP clerk's day: which documents do they key by hand, which senders are worst, what
      do they check before posting (this became the validation rules list).
- [ ] Confirm the review owner: one named person per queue (AP, order entry) with a backup.

## Phase 1: access and sandbox (week 1–2)

- [ ] Read-only SFTP account; read-only API token scoped to products and purchase orders.
- [ ] Ask for the API's **rate limit in writing**. Theirs was not documented; we saw 429s at about
      15% of requests under normal load. The client honours `Retry-After`.
- [ ] Separate database for the deployment; no writes into the ERP in phase 1 (the review queue
      exports approved documents as CSV for their existing import screen).

## Phase 2: mapping sign-off (week 2)

- [ ] Fill in `harbor_pine.yaml`. Every column mapping is reviewed with the data owner on a call,
      looking at real rows side by side.
- [ ] Run `fde demo --skip-prepare` against the sample drop and send the **data-quality report**
      (`load_runs.report`): rows read / loaded / rejected by reason / duplicates removed / control
      total check, per file.
- [ ] Sign-off criterion: every month reconciles to their control totals, and the weekly demand
      matrix built from loaded data equals the one built from the source file (this repo checks
      that automatically and refuses to plan if it does not).

## Phase 3: parallel run (weeks 3–6)

- [ ] Documents: run the inbox in **shadow mode** for two weeks. AP keeps keying by hand; compare
      their postings to our auto-approved documents every day. Go-live gate: zero auto-approved
      documents that AP had to correct, across at least 300 documents.
- [ ] Planning: produce the replenishment plan alongside their buyer's spreadsheet. Do not replace
      their weeks-of-cover rule on day one (see "what the numbers said" below).
- [ ] Onboard new senders from the corrections table (`/metrics` → `corrections_by_sender`): each
      recurring correction becomes a label alias or a sender profile, then a regression test.

## Phase 4: cutover

1. Freeze: last manual keying day agreed with AP lead.
2. Switch the inbox route so new mail goes to the service; keep the old mailbox read-only for 30 days.
3. Turn on auto-approval only for senders with ≥ 30 shadow documents and zero corrections; every
   other sender stays review-only until they qualify.
4. Hypercare for two weeks: daily check of the review backlog and of the DQ report.

**Rollback.** Auto-approval is a per-sender flag; setting the confidence threshold to 1.01 sends
everything to review without stopping intake. Intake is idempotent (file SHA-256), so a re-run after
a fix never double-loads.

## What the numbers said (and what I would tell the customer)

- On this data the **weeks-of-cover rule beat the textbook safety-stock formula** at every fill
  rate, because errors grow with demand and the Q4 ramp makes summer-sized buffers too small.
  Recommendation: keep the rule they understand, feed it the better forecast (11% less inventory
  at 85% fill than their trailing-average version), and set N on recent weeks before peak.
- The "optimal" allocation abandons small regions whose lost margin is below a truck's fixed cost.
  Ask the commercial team for a **contractual service floor** per region before showing anyone a
  cost saving; with a 75% floor the saving over greedy is 5%, not 42%.

---

## Troubleshooting

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `UnicodeDecodeError: byte 0xa3` (or `0x92`, `0x96`) | File is cp1252 (£, curly quote, en dash), not UTF-8 | DQ report note says "not UTF-8 (byte 0x..)" | Set `encoding: cp1252`; the adapter already falls back to the configured encoding after a strict UTF-8 attempt |
| `expected columns not found` | ERP patch changed header text, or preamble grew | First 5 lines of the file | Update `columns:` / `header_starts_with:` in the mapping; no code change |
| Unit prices 100x too large | Decimal comma read as thousands separator | Compare a row to the ERP screen | `decimal: ","` |
| Month loads with `control_total_ok: false` and note "truncated transfer?" | SFTP transfer cut off; trailer count > rows | File size vs previous months | Ask for re-send; the run is recorded as `loaded_with_control_break` so planning can refuse it |
| Same month's quantity 1–2% high | Export pagination bug re-emitted a page | `duplicates_removed` in DQ report | Already handled: repeated **blocks** of ≥ 50 rows are removed. Do not switch to row-level dedupe: the source has genuine identical lines |
| Inventory file loads 0 rows, error "no HDR record" | EBCDIC decoded as latin-1/ASCII, or wrong record length | First bytes in the error message (`b'\xc8\xc4\xd9'` = "HDR" in cp037) | `encoding: cp037`, `record_length: 68` |
| Negative on-hand rows | Real: picks posted before receipts | DQ report note per SKU | Kept and flagged; send list to warehouse for cycle count |
| Inventory "hash total mismatch" | Corrupt or partial record | Trailer vs sum of `on_hand` | Re-request the extract; do not load partial snapshots |
| REST run slow, many 429s | Rate limit | `retries` in the run report | Client backs off with `Retry-After`; if sustained, ask vendor for a higher limit or move the job off peak |
| REST job died mid-way | 5xx storm beyond max attempts | `.products.cursor` checkpoint file exists | Re-run: it resumes from the checkpointed cursor; records upsert by key |
| Product prices suddenly null after page N | API v2 renamed fields (`unit_cost` → `unitCost`) | Run report: "schema drift" note and `schema_variants` | Add the new name to `field_aliases`; the drift note fires on any unmapped name |
| Invoices from one supplier always in review with `low_confidence:doc_date` | Day/month ambiguity (05/03/2026) and no due date to disambiguate | `sender_profiles` table has no row for them | Profile is learned from the first unambiguous date (e.g. 25/03); or set it by hand |
| New supplier: everything `missing_field` | Labels outside the vocabulary ("Tax Point", "P.O. Number") | `/review/{id}` shows which fields are empty | Add aliases in `extract.py`, add a test document, re-run `fde docs-benchmark` on both splits |
| Lines extracted with no unit price | Right-aligned numbers under short headers, or two values in one column band | `low_confidence:lines` reason | Never auto-approved by design; fix the layout rule, add a regression test |
| `unknown_po` on many invoices from one supplier | They quote their own order number, not ours | Compare `po_number` with ERP | Ask supplier to quote our PO; meanwhile add a cross-reference table |

## Daily checks during hypercare

```bash
curl -s localhost:8000/metrics | jq '{straight_through_rate, review_backlog, corrections_by_field}'
psql $DATABASE_URL -c "select source, file, status, report->>'control_total_ok' from load_runs order by id desc limit 20"
```

## Escalation

- Data issue in a file: customer ERP admin (same day), cc their finance controller if a month will not reconcile.
- API outage > 2 hours: ERP vendor support with the request IDs and timestamps from the run report.
- Document errors that reached posting: stop auto-approval for that sender first, then investigate.

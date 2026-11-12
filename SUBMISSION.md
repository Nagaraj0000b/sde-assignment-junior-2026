# Submission

## Candidate
- **Name:** Nagaraj B  _(edit if you prefer your full name)_
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~8 hours (started at 11:00 AM IST, finished around 7:10 PM IST)

## How to run
```bash
# Python 3.10+ (standard library only - nothing to install).
# From the project root of a clean clone:

# 1. Generate the data (or point ingest at drop folders you were given)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 2. One-time: create the local secret key used to hash phones.
#    .hmac_key is gitignored - never commit or share it.
python solution/phone_hash.py --make-key

# 3. Ingest the drops, then write the five report files
python solution/pipeline.py ingest data/drop_1
python solution/pipeline.py ingest data/drop_2
python solution/pipeline.py report --out reports
```

Run the tests (synthetic, self-contained; no database or key needed):
```bash
python solution/phone_utils.py
python solution/phone_hash.py
python solution/lead_reader.py
python solution/row_validator.py
python solution/lead_ingest.py
python solution/event_ingest.py
python solution/event_linking.py --test
python solution/call_summary.py --test
python solution/report_generator.py --test
python solution/pipeline.py --test
```

## Design

Small single-purpose modules, wired together by `pipeline.py` in the spec's
command shape (`ingest <drop>`, `report --out <dir>`):

- **Lead ingestion** maps headers through schema aliases, validates and
  normalises (phones in six formats, mixed date/currency formats), rejects
  bad rows with reasons, and merges duplicates: the first valid row creates
  the lead and freezes its `batch_id`; later valid rows overwrite only the
  fields where they are non-null.
- **Event ingestion** appends to an immutable ledger. Deduplication is by
  `event_id` across all drops. Linking to leads is NOT decided at ingest.
- **Reports** derive everything at read time: linking (ordinary events by
  workflow + phone hash, overrides through `call_id`), call outcomes, and
  the five output files.

Database schema (SQLite), and why:

- `leads` - one row per `(workflow_id, phone_hash)`. Stores the hash plus
  last-4 for masking, never the full phone; `customer_name` is validated at
  ingest and never stored at all (the reports don't need it).
- `events` - append-only ledger; triggers block UPDATE and DELETE. There is
  deliberately no `is_orphan` column: an event's match is worked out at
  report time, so an event that was an orphan after drop_1 counts as soon as
  its lead arrives in drop_2 - without editing history.
- `ingested_files` - per-file register with a content hash: re-running a
  drop skips unchanged files, and an edited file at a known path is refused.
- `rejections` - feeds `rejections.csv`; carries no PII.

PII: phones are HMAC-SHA256 fingerprints keyed by a local `.hmac_key` file
that is gitignored and never printed. There is no fallback key: if the file
is missing the pipeline refuses to run rather than hashing with a throwaway
key.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Orphan / unknown-disposition status computed at report time | Ledger stays append-only; late-arriving leads adopt earlier events automatically | Flag events at ingest and update them later |
| Overrides link through `call_id` to the call's EARLIEST linked sibling | Deterministic when a call's events ever point at different leads (didn't occur in seed 42) | Latest sibling / majority |
| A call's time and IST date come from its EARLIEST `call.initiated` | First ring starts the call; deterministic (no repeats in seed 42) | Latest initiated |
| Identical `occurred_at` on competing dispositions: larger `event_id` wins | Total order needed for reproducible reports (didn't occur in seed 42) | Reject as malformed |
| Unknown-code disposition events are ignored for outcomes; an unknown override cannot beat a known set | Spec: unknown dispositions are counted and ignored | Let any latest event win |
| A call whose linked events point at different leads belongs to its earliest initiated's lead | One owner per call; deterministic (didn't occur in seed 42) | Split the call / majority |
| Events missing `event_type`, `call_id`, `workflow_id` or a parseable `occurred_at` count as malformed | They can't be stored or ordered; spec's malformed rule covers JSON+`event_id`, this extends it to storage essentials | Store them with nulls |
| Premiums convert to paise with ROUND_HALF_UP | Matches common billing convention; deterministic | Banker's rounding / truncation |
| A batch whose rows are all rejected appears only in `rejections.csv` | `batch_summary.csv` describes lead states; a zero-lead row adds noise (didn't occur in seed 42) | Emit a zero row |
| `rejections.csv` sorts `row_number` numerically | Natural reading of the contract | Text sort |
| The `en` default for `preferred_language` is applied when READING, never stored early | A later drop's real value must still be able to win the merge | Store the default at first insert |
| Naive timestamps are read as IST; everything is stored UTC, reports render IST | Spec states naive means IST and reports use IST dates | Server-local timezone |
| Premiums parsed with `Decimal`, stored as integer paise | Exact money handling; no float rounding; NaN/Infinity rejected | Floats |

## Failure modes considered

Handled:
- The messy inputs the spec calls out: malformed JSON lines and invalid
  rows are counted with reasons and skipped, not fatal. Validation is
  staged in the spec's order (duplicate event check before deeper
  validation).
- Duplicates: lead rows merge by identity; events dedupe by `event_id`,
  including across drops.
- Re-runs: unchanged files are skipped via the content-hash register; an
  edited file at a previously-seen path is refused instead of re-ingested.
- Crash mid-file: each file ingests inside one transaction - it commits on
  success or rolls back completely, so a drop can simply be re-run.
- Ledger tampering: triggers reject UPDATE/DELETE on `events`.
- Timestamp chaos: UTC / offset / naive-IST all normalise to UTC at ingest.

Known gaps (not handled):
- `--scale large` performance is not measured yet (see Results).
- The same file under a different path is re-read as new: identity dedupe
  prevents duplicate leads/events, but per-file counters inflate and its
  rows merge again (a stale copy can overwrite newer field values).
- Concurrent ingest runs against one database are not supported.

## Testing

- Every module carries in-module synthetic tests for its trickiest rules:
  phone normalisation across the six formats, header aliases and renamed
  columns, merge/last-write-wins, timestamp forms, override precedence,
  unknown-code exclusion, orphan isolation, bucket logic, IST date shifts.
- End-to-end `pipeline.py --test`: drop-by-drop vs all-at-once produce
  byte-identical reports; re-ingesting skips everything; a lead arriving in
  drop_2 adopts its orphaned drop_1 events.
- Ran the full pipeline on the real seed-42 data on Windows: all checks
  pass, and re-running ingest skips every file with all report checks
  passing again. Byte-identical report equality is covered by the
  synthetic end-to-end test and the AI-assisted checks below.
- Additional verification done with AI assistance (not personally run):
  a fresh seed-7 dataset produced byte-identical reports across
  sequential, all-at-once and re-run ingestion; report aggregates were
  independently recomputed in raw SQL as a cross-check.
- Not tested: `--scale large` timings, concurrent runs.

## Results

Seed 42, small scale (run on the candidate's machine):
- 2,057 lead rows read -> 1,892 leads (108 rows rejected, 57 duplicates
  merged, 122 field warnings).
- 8,745 event lines read -> 8,500 events stored (8 malformed, 237
  duplicates skipped).
- 2,536 reportable calls: 1,467 connected, 298 telephony failures, 1,417
  with a known outcome (66 corrected by supervisor overrides), 399
  successes.
- Final state: 462 orphan events, 18 unknown dispositions among linked
  events.
- Reports: batch_summary 6 rows, daily_overview 33 rows, lead_report 1,892
  rows, rejections 108 rows.

Surprising in the data: events really do arrive before their leads (drop_1
orphans resolve in drop_2); phones appear in six formats; some disposition
codes sit outside the catalogue; supervisor overrides carry no phone at
all.

Timings: _[TO MEASURE before submitting - e.g. time the three pipeline
commands on this machine; do not submit with this placeholder]_

## If I had another day

1. Measure and optimise `--scale large` (the 2-minute target), reporting
   timings.
2. Push report aggregation into SQL (stretch goal) and compare outputs.
3. Production note: scheduling, backfills and monitoring for 100k
   leads/day.
4. Friendlier diff output when a changed file is refused.

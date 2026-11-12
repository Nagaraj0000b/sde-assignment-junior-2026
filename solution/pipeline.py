"""
Pipeline entry point - Assignment A.

One command for the whole flow, in the shape the README specifies:

    python solution/pipeline.py ingest data/drop_1
    python solution/pipeline.py ingest data/drop_2
    python solution/pipeline.py report --out reports

`ingest` accepts one drop folder (data/drop_1) or the data root (data/),
which processes its drop_* folders in sorted order. Inside a drop, lead
CSVs are processed BEFORE event files, each in filename order - so
"drop_1 then drop_2" and "everything at once" visit the same files in
the same order and produce the same reports. Lead schemas come from
assignment-a-data/schemas/<workflow_id>.json, where the workflow id is
the folder name above each batch CSV.

Re-running ingest on a drop that was already ingested changes nothing:
every file is content-hashed into the ingested_files register and
skipped when unchanged (the register lives in the database itself).

Options:
    --db PATH           database file (default: pipeline.db next to
                        db_setup.py, i.e. your normal working database)
    --schemas DIR       workflow schemas folder
                        (default assignment-a-data/schemas)
    --dispositions PATH disposition catalogue for `report`
                        (default assignment-a-data/schemas/dispositions.csv)

The HMAC key is loaded from .hmac_key next to phone_hash.py BEFORE any
database write; report never needs the key (it reads hashes, never
makes them). `report` opens the database read-only.

Run the tests (they use temporary folders and a test-only key; your
real database and key are never touched):
    python solution/pipeline.py --test
"""

import argparse
import csv
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from db_setup import DEFAULT_DB, create_database
from phone_hash import load_key
from lead_ingest import ingest_file
from event_ingest import ingest_event_file
from report_generator import collect_reports, verify, write_reports
from event_linking import DEFAULT_DISPOSITIONS, load_dispositions

DEFAULT_SCHEMAS = Path("assignment-a-data/schemas")


def find_drops(path) -> list:
    """Resolve the ingest argument to an ordered list of drop folders."""
    path = Path(path)
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]
    if path.is_dir():
        drops = sorted(p for p in path.iterdir()
                       if p.is_dir() and p.name.startswith("drop"))
        if drops:
            return drops
    raise SystemExit(
        f"No leads/ or events/ folder under {path}.\n"
        "Pass a drop folder (data/drop_1) or the data root (data/).")


def drop_files(drop) -> tuple:
    """(lead CSVs, event JSONLs) of one drop, each in filename order."""
    leads_dir, events_dir = drop / "leads", drop / "events"
    leads = sorted(leads_dir.glob("*/*.csv")) if leads_dir.is_dir() else []
    events = sorted(events_dir.glob("*.jsonl")) if events_dir.is_dir() else []
    return leads, events


def do_ingest(path, db, schemas_dir=DEFAULT_SCHEMAS, key=None) -> tuple:
    """Ingest one drop or a whole data root. Returns (ingested, skipped)."""
    key = key if key is not None else load_key()  # BEFORE any DB write
    schemas_dir = Path(schemas_dir)
    conn = create_database(str(db))
    ingested = skipped = 0
    try:
        for drop in find_drops(path):
            leads, events = drop_files(drop)
            if not leads and not events:
                print(f"  {drop}: nothing to ingest")
                continue
            for csv_path in leads:  # leads before events, filename order
                workflow_id = csv_path.parent.name
                schema_path = schemas_dir / f"{workflow_id}.json"
                if not schema_path.exists():
                    raise SystemExit(f"Schema not found: {schema_path} "
                                     f"(needed by {csv_path})")
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                if schema.get("workflow_id") != workflow_id:
                    raise SystemExit(
                        f"Schema mismatch: {schema_path} is for "
                        f"{schema.get('workflow_id')}, not {workflow_id}")
                result = ingest_file(conn, schema, csv_path, key)
                if result == "skipped":
                    skipped += 1
                    print(f"  skipped   {csv_path} (already ingested)")
                else:
                    ingested += 1
                    print(f"  ingested  {csv_path}: "
                          f"{result['lead_rows_total']} rows, "
                          f"{result['lead_rows_rejected']} rejected, "
                          f"{result['duplicate_lead_rows_merged']} merged, "
                          f"{result['field_warnings']} warnings")
            for jsonl_path in events:
                result = ingest_event_file(conn, jsonl_path, key)
                if result == "skipped":
                    skipped += 1
                    print(f"  skipped   {jsonl_path} (already ingested)")
                else:
                    ingested += 1
                    print(f"  ingested  {jsonl_path}: "
                          f"{result['events_total']} lines, "
                          f"{result['malformed_events']} malformed, "
                          f"{result['duplicate_events_dropped']} duplicates")
    finally:
        conn.close()
    print(f"Ingest finished: {ingested} file(s) ingested, "
          f"{skipped} skipped unchanged.")
    return ingested, skipped


def do_report(out_dir, db, dispositions=DEFAULT_DISPOSITIONS) -> int:
    """Write the five report files from an existing database."""
    if not Path(db).exists():
        raise SystemExit(f"Database not found: {db}\n"
                         "Run ingest first.")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        data, stats = collect_reports(conn, load_dispositions(dispositions))
        checks = verify(data, stats, conn)
    finally:
        conn.close()
    paths = write_reports(data, out_dir)
    for p in paths:
        rows = len(data[p.stem]) if p.suffix == ".csv" else None
        print(f"  wrote {p}" + (f"  ({rows} rows)" if rows is not None else ""))
    failed = 0
    for name, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + name)
        failed += 0 if ok else 1
    return 1 if failed else 0


def main(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_in = sub.add_parser("ingest", help="ingest a drop folder or data root")
    p_in.add_argument("path")
    p_in.add_argument("--db", default=str(DEFAULT_DB))
    p_in.add_argument("--schemas", default=str(DEFAULT_SCHEMAS))
    p_re = sub.add_parser("report", help="write the five report files")
    p_re.add_argument("--out", required=True)
    p_re.add_argument("--db", default=str(DEFAULT_DB))
    p_re.add_argument("--dispositions", default=str(DEFAULT_DISPOSITIONS))
    args = parser.parse_args(argv)

    if args.command == "ingest":
        do_ingest(args.path, args.db, args.schemas)
        return 0
    return do_report(args.out, args.db, args.dispositions)


# --- tests -----------------------------------------------------------------
TEST_KEY = b"test-only-key-not-for-production"

SCHEMA = {
    "workflow_id": "test_flow",
    "fields": [
        {"name": "customer_name", "type": "string", "required": True,
         "pii": True, "aliases": ["name"]},
        {"name": "phone", "type": "phone", "required": True, "pii": True,
         "aliases": ["mobile"]},
        {"name": "policy_number", "type": "string", "required": True,
         "reference": True, "aliases": []},
        {"name": "preferred_language", "type": "string", "required": False,
         "aliases": ["language"]},
    ],
}

DISPOSITIONS = ("workflow_id,code,label,is_terminal,is_success\n"
                "test_flow,renewed,Renewed,true,true\n")

BATCH_001 = ("customer_name,phone,policy_number,preferred_language\n"
             "Asha Rao,+919812300001,POL-1,\n"
             "Bala K,098123 00002,POL-2,hi\n"
             "C Bad,12345,POL-3,\n")  # invalid phone -> rejected

BATCH_002 = ("customer_name,phone,policy_number,preferred_language\n"
             "Asha Rao,+919812300001,POL-1,ta\n"   # merged duplicate
             "D New,+919812300009,POL-9,\n")       # lead arrives late

EVENTS_1 = "\n".join([
    '{"event_id": "e1", "event_type": "call.initiated", "workflow_id":'
    ' "test_flow", "call_id": "c1", "occurred_at": "2026-10-05T10:00:00Z",'
    ' "phone": "+919812300001", "payload": {}}',
    '{"event_id": "e2", "event_type": "disposition.set", "workflow_id":'
    ' "test_flow", "call_id": "c1", "occurred_at": "2026-10-05T10:05:00Z",'
    ' "phone": "+919812300001", "payload": {"disposition_code": "renewed"}}',
    # event for POL-9 BEFORE its lead exists (orphan until drop_2)
    '{"event_id": "e3", "event_type": "call.initiated", "workflow_id":'
    ' "test_flow", "call_id": "c2", "occurred_at": "2026-10-05T11:00:00Z",'
    ' "phone": "+919812300009", "payload": {}}',
    '{"event_id": "e1", "event_type": "call.initiated", "workflow_id":'
    ' "test_flow", "call_id": "c1", "occurred_at": "2026-10-05T10:00:00Z",'
    ' "phone": "+919812300001", "payload": {}}',  # duplicate event_id
    "{not json",
]) + "\n"

EVENTS_2 = ('{"event_id": "e4", "event_type": "disposition.set",'
            ' "workflow_id": "test_flow", "call_id": "c2",'
            ' "occurred_at": "2026-10-05T11:05:00Z",'
            ' "phone": "+919812300009",'
            ' "payload": {"disposition_code": "renewed"}}\n')

FIVE = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv",
        "rejections.csv", "data_quality.json"]


def _build_fixture(root: Path):
    schemas = root / "schemas"
    schemas.mkdir()
    (schemas / "test_flow.json").write_text(json.dumps(SCHEMA))
    (schemas / "dispositions.csv").write_text(DISPOSITIONS)
    d1 = root / "data" / "drop_1"
    d2 = root / "data" / "drop_2"
    (d1 / "leads" / "test_flow").mkdir(parents=True)
    (d1 / "events").mkdir(parents=True)
    (d2 / "leads" / "test_flow").mkdir(parents=True)
    (d2 / "events").mkdir(parents=True)
    (d1 / "leads" / "test_flow" / "batch_001.csv").write_text(BATCH_001)
    (d1 / "events" / "events_0001.jsonl").write_text(EVENTS_1)
    (d2 / "leads" / "test_flow" / "batch_002.csv").write_text(BATCH_002)
    (d2 / "events" / "events_1001.jsonl").write_text(EVENTS_2)
    return schemas, d1, d2


def _run_tests() -> int:
    failed = 0

    def check(name, ok):
        nonlocal failed
        print(("PASS  " if ok else "FAIL  ") + name)
        failed += 0 if ok else 1

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        schemas, d1, d2 = _build_fixture(root)
        disp = schemas / "dispositions.csv"

        # sequential: drop_1 then drop_2
        db_seq = root / "seq.db"
        do_ingest(d1, db_seq, schemas, key=TEST_KEY)
        do_ingest(d2, db_seq, schemas, key=TEST_KEY)
        rc_seq = do_report(root / "out_seq", db_seq, disp)

        # everything at once: ingest the data root
        db_all = root / "all.db"
        do_ingest(root / "data", db_all, schemas, key=TEST_KEY)
        do_report(root / "out_all", db_all, disp)

        # idempotence: ingest both drops AGAIN into the sequential DB
        _, skipped = do_ingest(root / "data", db_seq, schemas, key=TEST_KEY)
        do_report(root / "out_rerun", db_seq, disp)

        same_seq_all = all((root / "out_seq" / f).read_bytes()
                           == (root / "out_all" / f).read_bytes() for f in FIVE)
        same_rerun = all((root / "out_seq" / f).read_bytes()
                         == (root / "out_rerun" / f).read_bytes() for f in FIVE)

        quality = json.loads((root / "out_seq" / "data_quality.json")
                             .read_text())
        lead_report = (root / "out_seq" / "lead_report.csv").read_text()
        pol9 = [line for line in lead_report.splitlines()
                if ",POL-9," in line]

        check("drop-by-drop and all-at-once give identical reports",
              same_seq_all)
        check("re-ingesting both drops skips all 4 files, reports unchanged",
              skipped == 4 and same_rerun)
        check("late-arriving lead adopts its earlier orphan event "
              "(final orphan count 0, POL-9 has 1 call + renewed)",
              quality["orphan_events"] == 0 and len(pol9) == 1
              and ",1,0," in pol9[0] and pol9[0].endswith("renewed,true"))
        check("counters: 6 event lines, 1 malformed, 1 duplicate, "
              "5 lead rows, 1 rejected, 1 merged",
              (quality["events_total"], quality["malformed_events"],
               quality["duplicate_events_dropped"], quality["lead_rows_total"],
               quality["lead_rows_rejected"],
               quality["duplicate_lead_rows_merged"]) == (6, 1, 1, 5, 1, 1))
        check("report returns success (all invariants pass)", rc_seq == 0)

    print(f"\n{5 - failed}/5 tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        raise SystemExit(_run_tests())
    raise SystemExit(main(sys.argv[1:]))

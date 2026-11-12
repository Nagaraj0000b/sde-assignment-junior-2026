"""
Database health check - Assignment A helper (read-only).

Prints what actually reached pipeline.db and verifies the ingestion
counters against the tables. It opens the database READ-ONLY, writes
nothing, needs no secret key, and prints only counts and file paths -
never customer data.

Run it after ingesting, from the project's main folder:
    python solution/db_check.py

The arithmetic it checks:
- every non-blank lead row was either rejected, created a lead, or
  merged into one:   lead rows read == leads + rejected + merged
- every non-empty event line was malformed, a duplicate, or stored:
   event lines read == stored events + malformed + duplicates

(Orphan and unknown-disposition counts are deliberately NOT checked
here - those belong to the report step.)

If you generated data with --seed 42 --scale small, your totals should
be close to the reference printed at the bottom; a different seed
produces different (still correct) numbers.
"""

import sqlite3
import sys
from pathlib import Path

from db_setup import DEFAULT_DB

# Informational only, from our verified run on seed 42 / scale small.
REFERENCE_SEED42 = {"leads": 1892, "events": 8500, "rejections": 108}


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open strictly read-only: this script can never mutate the db."""
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}\n"
                         "Run db_setup.py and your ingestion commands first.")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _sum(conn, column):
    return conn.execute(
        f"SELECT COALESCE(SUM({column}), 0) FROM ingested_files").fetchone()[0]


def run_checks(conn) -> list:
    checks = []
    files = conn.execute(
        "SELECT file_path FROM ingested_files ORDER BY file_path").fetchall()
    lead_files = [p for (p,) in files if p.lower().endswith(".csv")]
    event_files = [p for (p,) in files if p.lower().endswith(".jsonl")]

    leads = _count(conn, "leads")
    events = _count(conn, "events")
    rejections = _count(conn, "rejections")
    rows_read = _sum(conn, "lead_rows_total")
    rejected = _sum(conn, "lead_rows_rejected")
    merged = _sum(conn, "duplicate_lead_rows_merged")
    warnings = _sum(conn, "field_warnings")
    lines_read = _sum(conn, "events_total")
    malformed = _sum(conn, "malformed_events")
    duplicates = _sum(conn, "duplicate_events_dropped")

    print("Registered input files:")
    for p in lead_files:
        print(f"  lead   {p}")
    for p in event_files:
        print(f"  events {p}")
    if not files:
        print("  (none yet)")

    print("\nLEADS")
    print(f"  leads in table:            {leads}")
    print(f"  rows read (files):         {rows_read}")
    print(f"  rejected (counter):        {rejected}")
    print(f"  rejections in table:       {rejections}")
    print(f"  merged duplicates:         {merged}")
    print(f"  field warnings:            {warnings}")
    for wf, n in conn.execute(
            "SELECT workflow_id, COUNT(*) FROM leads GROUP BY 1 ORDER BY 1"):
        print(f"    {wf}: {n}")

    print("\nEVENTS")
    print(f"  events in ledger:          {events}")
    print(f"  lines read (files):        {lines_read}")
    print(f"  malformed lines:           {malformed}")
    print(f"  duplicates skipped:        {duplicates}")
    for et, n in conn.execute(
            "SELECT event_type, COUNT(*) FROM events GROUP BY 1 ORDER BY 1"):
        print(f"    {et}: {n}")

    print("\nRejection reasons:")
    for reason, n in conn.execute(
            "SELECT reason, COUNT(*) FROM rejections GROUP BY 1 ORDER BY 1"):
        print(f"    {reason}: {n}")

    checks.append(("lead rows read == leads + rejected + merged",
                   rows_read == leads + rejected + merged))
    checks.append(("rejected counter == rejections table rows",
                   rejected == rejections))
    checks.append(("event lines == stored + malformed + duplicates",
                   lines_read == events + malformed + duplicates))
    checks.append(("all registered files are .csv or .jsonl",
                   len(files) == len(lead_files) + len(event_files)))

    print("\nReference (only if you used --seed 42 --scale small):")
    print(f"  leads {REFERENCE_SEED42['leads']}, "
          f"events {REFERENCE_SEED42['events']}, "
          f"rejections {REFERENCE_SEED42['rejections']}")
    return checks


def main() -> int:
    conn = connect_ro(DEFAULT_DB)
    try:
        checks = run_checks(conn)
    finally:
        conn.close()
    print()
    failed = 0
    for name, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + name)
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

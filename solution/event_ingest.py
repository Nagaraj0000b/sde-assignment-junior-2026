"""
Event ingestion - Assignment A building block 7.

Reads one events JSONL file into the append-only ledger.

Usage (from the project's main folder):
    python solution/event_ingest.py data/drop_1/events/events_0001.jsonl

Per-line spec order (README, "Event files"):
1. Malformed: not valid JSON, or no event_id -> count and skip.
   (The spec covers only those two; a line missing event_type, call_id,
   workflow_id or occurred_at can't be stored or ordered at all, so we
   count it as malformed too - record that call in SUBMISSION.md.)
2. Duplicate: event_id already in the ledger (any drop, any file)
   -> count and skip.
3. Everything else is STORED, including:
   - events whose lead hasn't arrived yet (orphans stay in the ledger,
     excluded from reports until they link at report time), and
   - disposition events with codes outside dispositions.csv.
   Orphan and unknown-disposition COUNTS are computed at report time,
   not here, because the spec wants orphan counts to reflect the final
   state: an event that is an orphan after drop_1 must count once its
   lead arrives in drop_2. Classifying at report time also preserves
   the spec's check order - an unlinked disposition is an orphan first,
   never an unknown disposition.
   (The per-file unknown_disposition_events counter stays 0 for now;
   the report step computes it over linked events.)

Timestamps: ...Z and ...+05:30 carry their zone; a NAIVE timestamp
(2026-10-03 10:01:00) means IST. Everything is stored in UTC.

Privacy: the raw phone is never stored - only its HMAC fingerprint.
disposition.override has no phone: phone_hash stays NULL and reports
link it through call_id.

Same file rules as lead ingestion: unchanged re-run is skipped, a
changed file at a known path is refused, one transaction per file.

Run the tests:
    python solution/event_ingest.py
"""

import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db_setup import create_database
from phone_hash import load_key, phone_hash
from phone_utils import normalise_phone

IST = timezone(timedelta(hours=5, minutes=30))

# Fractional seconds arrive with varying precision; pad/truncate to 6
# digits so every supported Python version parses them.
_FRAC = re.compile(r"\.(\d+)((?:[+-]\d{2}:?\d{2})?)$")


def to_utc(ts: str):
    """Normalise a spec timestamp to a UTC ISO string. None = unparseable."""
    ts = str(ts).strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    ts = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6] + m.group(2), ts)
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)  # naive means IST
    # Fixed microseconds keep the stored text the same width, so plain
    # string sorting is always chronological.
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_event_basics(line: str):
    """Stage 1, the spec's malformed rule: valid JSON with an event_id."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not obj.get("event_id"):
        return None
    return obj


def validate_event(obj: dict):
    """Stage 3, our documented extra: fields needed to store/order the row.

    Returns occurred_utc, or None (caller counts the line as malformed).
    Runs only AFTER the duplicate check, so an already-seen event_id is
    always counted as a duplicate, exactly the spec's check order.
    """
    if any(not obj.get(k) for k in ("event_type", "call_id", "workflow_id")):
        return None
    return to_utc(obj.get("occurred_at", ""))


def ingest_event_file(conn, jsonl_path, key: bytes):
    """Ingest one events JSONL file inside a single transaction.

    Returns the per-file counters dict, or "skipped" for an unchanged
    re-run.
    """
    jsonl_path = Path(jsonl_path).resolve()
    file_id = str(jsonl_path)
    content_hash = hashlib.sha256(jsonl_path.read_bytes()).hexdigest()

    counters = {"events_total": 0, "malformed_events": 0,
                "duplicate_events_dropped": 0, "unknown_disposition_events": 0}

    with conn:  # commit on success, roll back EVERYTHING on any error
        existing = conn.execute(
            "SELECT content_hash FROM ingested_files WHERE file_path = ?",
            (file_id,)).fetchone()
        if existing is not None:
            if existing[0] == content_hash:
                return "skipped"
            raise RuntimeError(
                f"{file_id} was already ingested but its contents changed. "
                "Re-ingesting a changed file is not supported - restore the "
                "original or document the decision in SUBMISSION.md."
            )

        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue  # empty lines are not counted at all
                counters["events_total"] += 1

                obj = parse_event_basics(line)  # stage 1: spec malformed
                if obj is None:
                    counters["malformed_events"] += 1
                    continue

                # stage 2: duplicate check comes before any deeper validation
                if conn.execute("SELECT 1 FROM events WHERE event_id = ?",
                                (obj["event_id"],)).fetchone():
                    counters["duplicate_events_dropped"] += 1
                    continue

                occurred_utc = validate_event(obj)  # stage 3: documented extra
                if occurred_utc is None:
                    counters["malformed_events"] += 1
                    continue

                ph = None
                if obj.get("phone"):
                    canonical = normalise_phone(obj["phone"])
                    if canonical:
                        ph = phone_hash(canonical, key)
                    # an unnormalisable phone simply can't link: the event
                    # is stored with NULL and reads as an orphan later

                conn.execute(
                    "INSERT INTO events (event_id, call_id, workflow_id, "
                    "phone_hash, event_type, occurred_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (obj["event_id"], obj["call_id"], obj["workflow_id"], ph,
                     obj["event_type"], occurred_utc,
                     json.dumps(obj.get("payload") or {}, sort_keys=True)))

        conn.execute(
            "INSERT INTO ingested_files "
            "(file_path, content_hash, processed_at, events_total, "
            "malformed_events, duplicate_events_dropped, "
            "unknown_disposition_events) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, content_hash, datetime.now(timezone.utc).isoformat(),
             counters["events_total"], counters["malformed_events"],
             counters["duplicate_events_dropped"],
             counters["unknown_disposition_events"]))

    return counters


def main(argv) -> int:
    if len(argv) != 2:
        print("Usage: python solution/event_ingest.py <events.jsonl>")
        return 2
    key = load_key()  # BEFORE any database write: missing key, zero mutations
    conn = create_database()
    try:
        result = ingest_event_file(conn, argv[1], key)
    finally:
        conn.close()
    if result == "skipped":
        print(f"{Path(argv[1]).name} was already ingested unchanged - "
              "skipped, nothing changed.")
    else:
        print(f"Ingested {Path(argv[1]).name}: "
              f"{result['events_total']} lines read, "
              f"{result['malformed_events']} malformed, "
              f"{result['duplicate_events_dropped']} duplicates skipped.")
    return 0


# --- tests -----------------------------------------------------------------
TEST_KEY = b"test-only-key-not-for-production"


def _write(tmp, name, lines):
    p = Path(tmp) / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _run_tests() -> int:
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        f1 = _write(tmp, "events_0001.jsonl", [
            '{"event_id": "e1", "event_type": "call.initiated",'
            ' "workflow_id": "test_flow", "call_id": "c1",'
            ' "occurred_at": "2026-10-05T13:20:15Z",'
            ' "phone": "9876543210", "payload": {}}',
            '',  # empty line: not counted
            'not json at all',  # malformed
            '{"event_type": "call.connected", "call_id": "c1",'
            ' "workflow_id": "test_flow",'
            ' "occurred_at": "2026-10-05T13:20:20Z"}',  # no event_id: malformed
            '{"event_id": "e2", "event_type": "call.connected",'
            ' "workflow_id": "test_flow", "call_id": "c1",'
            ' "occurred_at": "2026-10-05T18:50:15+05:30",'
            ' "phone": "91-98765-43210", "payload": {}}',  # same instant as e1
            '{"event_id": "e3", "event_type": "disposition.override",'
            ' "workflow_id": "test_flow", "call_id": "c1",'
            ' "occurred_at": "2026-10-05 19:00:00",'
            ' "payload": {"disposition_code": "renewed",'
            ' "overridden_by": "sup1", "reason": "audit"}}',  # naive = IST
            '{"event_id": "e1", "event_type": "call.initiated",'
            ' "workflow_id": "test_flow", "call_id": "c1",'
            ' "occurred_at": "2026-10-05T13:20:15Z",'
            ' "phone": "9876543210", "payload": {}}',  # duplicate in same file
            '{"event_id": "e4", "event_type": "disposition.set",'
            ' "workflow_id": "test_flow", "call_id": "c1",'
            ' "occurred_at": "2026-10-05T13:25:00Z", "phone": "9876543210",'
            ' "payload": {"disposition_code": "made_up_code"}}',  # unknown code
            '{"event_id": "e5", "event_type": "call.ended",'
            ' "workflow_id": "test_flow", "call_id": "c1",'
            ' "occurred_at": "2026-10-05T13:20:15.5Z",'
            ' "phone": "9876543210", "payload": {"duration_sec": 0,'
            ' "end_reason": "carrier"}}',  # fractional: same second as e1
            '{"event_id": "e2", "occurred_at": "banana"}',
            # ^ already-seen id with garbage fields: duplicate, NOT malformed
        ])

        conn = create_database(":memory:")
        c1 = ingest_event_file(conn, f1, TEST_KEY)
        rows = conn.execute(
            "SELECT event_id, phone_hash, event_type, occurred_at, payload "
            "FROM events ORDER BY event_id").fetchall()
        by_id = {r[0]: r for r in rows}

        checks.append(("counts: 9 non-empty lines, 2 malformed, 2 duplicates",
                       (c1["events_total"], c1["malformed_events"],
                        c1["duplicate_events_dropped"]) == (9, 2, 2)))
        checks.append(("seen id with bad fields counts as duplicate, not malformed",
                       c1["duplicate_events_dropped"] == 2
                       and c1["malformed_events"] == 2))
        checks.append(("5 fresh events stored, malformed/dup lines absent",
                       sorted(by_id) == ["e1", "e2", "e3", "e4", "e5"]))
        checks.append(("Z and +05:30 forms normalise to the same UTC instant",
                       by_id["e1"][3] == by_id["e2"][3]
                       and by_id["e1"][3].endswith("+00:00")))
        checks.append(("naive timestamp treated as IST (19:00 IST = 13:30 UTC)",
                       by_id["e3"][3] == "2026-10-05T13:30:00.000000+00:00"))
        checks.append(("fixed-width timestamps sort chronologically as text",
                       sorted(r[3] for r in (by_id["e1"], by_id["e5"]))
                       == [by_id["e1"][3], by_id["e5"][3]]
                       and by_id["e5"][3].endswith(".500000+00:00")))
        checks.append(("override stored with NULL phone_hash, payload intact",
                       by_id["e3"][1] is None
                       and json.loads(by_id["e3"][4])["disposition_code"] == "renewed"))
        checks.append(("no plaintext phone anywhere in the ledger",
                       all("9876543210" not in str(cell) for r in rows for cell in r)))
        checks.append(("unknown-code disposition stored for report-time handling",
                       "e4" in by_id and c1["unknown_disposition_events"] == 0))

        # duplicate across a SECOND file (same event_id, new file)
        f2 = _write(tmp, "events_0002.jsonl", [
            '{"event_id": "e1", "event_type": "call.initiated",'
            ' "workflow_id": "test_flow", "call_id": "c1",'
            ' "occurred_at": "2026-10-05T13:20:15Z",'
            ' "phone": "9876543210", "payload": {}}',
        ])
        c2 = ingest_event_file(conn, f2, TEST_KEY)
        checks.append(("duplicate across files counted, ledger still has 5",
                       c2["duplicate_events_dropped"] == 1
                       and conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 5))

        checks.append(("unchanged re-run skipped",
                       ingest_event_file(conn, f1, TEST_KEY) == "skipped"))

        # rollback: break the ledger table, nothing from the file persists
        conn2 = create_database(":memory:")
        conn2.execute("DROP TABLE events")
        try:
            ingest_event_file(conn2, f1, TEST_KEY)
            rolled_back = False
        except sqlite3.OperationalError:
            rolled_back = True
        checks.append(("mid-file failure rolls back the whole file",
                       rolled_back
                       and conn2.execute("SELECT COUNT(*) FROM ingested_files")
                       .fetchone()[0] == 0))

    failed = 0
    for i, (name, ok) in enumerate(checks, start=1):
        print(("PASS  " if ok else "FAIL  ") + f"case {i}: {name}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) == 2:
        raise SystemExit(main(sys.argv))
    raise SystemExit(_run_tests())

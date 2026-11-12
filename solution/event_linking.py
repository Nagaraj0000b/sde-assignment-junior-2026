"""
Event linking - Assignment A report foundation (read-only).

Works out, for every event in the ledger, which lead it belongs to -
WITHOUT changing anything in the database. This is the foundation the
report files are built on.

Link rules (README):
- An ordinary event links via (workflow_id, phone_hash) to a lead.
- A disposition.override has no phone: it links through its call_id to
  the lead of that call's other events. (Ordinary events never link by
  call_id - only overrides do.)
- Classification follows the spec's check order: an event with no lead
  is an ORPHAN first; only LINKED disposition.* events whose code is
  missing from dispositions.csv for that workflow count as unknown
  dispositions. So an unlinked disposition is an orphan, never an
  unknown disposition.

Deterministic tie-handling (documented for SUBMISSION.md): if a call's
sibling events ever point at more than one lead, the override takes the
lead of the call's EARLIEST linked sibling (by occurred_at, event_id).

Nothing here writes to the ledger: orphans and unknown dispositions are
read-time classifications, which is exactly why the append-only history
never needs editing.

Usage (from the project's main folder):
    python solution/event_linking.py
    python solution/event_linking.py --dispositions assignment-a-data/schemas/dispositions.csv

Run the tests:
    python solution/event_linking.py --test
"""

import csv
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from db_setup import DEFAULT_DB, create_database

DEFAULT_DISPOSITIONS = Path("assignment-a-data/schemas/dispositions.csv")


def load_dispositions(path) -> dict:
    """(workflow_id, code) -> {is_terminal, is_success, label}."""
    catalogue = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            catalogue[(row["workflow_id"], row["code"])] = {
                "label": row["label"],
                "is_terminal": row["is_terminal"].strip().lower() == "true",
                "is_success": row["is_success"].strip().lower() == "true",
            }
    return catalogue


def link_events(conn) -> list:
    """Return every ledger event with its lead key (or None when orphan).

    Each item: {event_id, workflow_id, call_id, event_type, occurred_at,
    payload (dict), phone_hash, lead_key}. lead_key is the
    (workflow_id, phone_hash) pair of the matched lead.
    """
    lead_keys = {(wf, ph) for wf, ph in conn.execute(
        "SELECT workflow_id, phone_hash FROM leads")}

    events = []
    by_call = {}
    for (eid, wf, cid, ph, etype, occ, payload) in conn.execute(
            "SELECT event_id, workflow_id, call_id, phone_hash, event_type,"
            " occurred_at, payload FROM events"):
        ev = {"event_id": eid, "workflow_id": wf, "call_id": cid,
              "phone_hash": ph, "event_type": etype, "occurred_at": occ,
              "payload": json.loads(payload), "lead_key": None}
        events.append(ev)
        by_call.setdefault(cid, []).append(ev)

    for ev in events:
        if ev["event_type"] == "disposition.override":
            # link through the call's earliest linked sibling
            siblings = sorted(
                (s for s in by_call[ev["call_id"]]
                 if s is not ev and s["phone_hash"] is not None
                 and (s["workflow_id"], s["phone_hash"]) in lead_keys),
                key=lambda s: (s["occurred_at"], s["event_id"]))
            if siblings:
                s = siblings[0]
                ev["lead_key"] = (s["workflow_id"], s["phone_hash"])
        elif (ev["workflow_id"], ev["phone_hash"]) in lead_keys:
            ev["lead_key"] = (ev["workflow_id"], ev["phone_hash"])
    return events


def classify(events: list, catalogue: dict) -> dict:
    """Split linked events into usable / unknown-disposition, count orphans."""
    summary = {"total": len(events), "linked": 0, "orphans": 0,
               "orphan_overrides": 0, "unknown_dispositions": 0}
    for ev in events:
        if ev["lead_key"] is None:
            summary["orphans"] += 1
            if ev["event_type"] == "disposition.override":
                summary["orphan_overrides"] += 1
            continue
        summary["linked"] += 1
        if ev["event_type"].startswith("disposition."):
            code = ev["payload"].get("disposition_code")
            if (ev["workflow_id"], code) not in catalogue:
                summary["unknown_dispositions"] += 1
    return summary


def main(argv) -> int:
    disp_path = DEFAULT_DISPOSITIONS
    if "--dispositions" in argv:
        disp_path = Path(argv[argv.index("--dispositions") + 1])
    if not Path(DEFAULT_DB).exists():
        raise SystemExit(f"Database not found: {DEFAULT_DB}\n"
                         "Run your ingestion commands first.")

    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
    try:
        events = link_events(conn)
    finally:
        conn.close()
    summary = classify(events, load_dispositions(disp_path))

    print("EVENT LINKING (read-only, counts only)")
    print(f"  events in ledger:        {summary['total']}")
    print(f"  linked to a lead:        {summary['linked']}")
    print(f"  orphans (final state):   {summary['orphans']}"
          f"  (of which overrides: {summary['orphan_overrides']})")
    print(f"  unknown dispositions:    {summary['unknown_dispositions']}"
          "  (linked events only)")
    ok = summary["linked"] + summary["orphans"] == summary["total"]
    print(("PASS  " if ok else "FAIL  ") + "linked + orphans == total")
    return 0 if ok else 1


# --- tests -----------------------------------------------------------------
TEST_KEY = b"test-only-key-not-for-production"
CATALOGUE = {("test_flow", "renewed"): {"label": "Renewed",
                                        "is_terminal": True,
                                        "is_success": True}}


def _add_lead(conn, wf, ph):
    conn.execute(
        "INSERT INTO leads VALUES (?, ?, '0000', 'batch_001', 'REF',"
        " NULL, NULL, NULL, NULL, NULL)", (wf, ph))


def _add_event(conn, eid, cid, wf, ph, etype, occ, code=None):
    payload = {"disposition_code": code} if code else {}
    conn.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
        (eid, cid, wf, ph, etype, occ, json.dumps(payload)))


def _run_tests() -> int:
    conn = create_database(":memory:")
    _add_lead(conn, "test_flow", "hashA")
    _add_lead(conn, "test_flow", "hashB")
    _add_event(conn, "e1", "c1", "test_flow", "hashA", "call.initiated",
               "2026-10-05T10:00:00.000000+00:00")
    _add_event(conn, "e2", "c1", "test_flow", "hashA", "disposition.set",
               "2026-10-05T10:01:00.000000+00:00", code="renewed")
    _add_event(conn, "e3", "c1", "test_flow", None, "disposition.override",
               "2026-10-05T10:02:00.000000+00:00", code="renewed")
    _add_event(conn, "e4", "c2", "test_flow", "hashZZZ", "call.initiated",
               "2026-10-05T10:00:00.000000+00:00")          # orphan: no lead
    _add_event(conn, "e5", "c2", "test_flow", "hashZZZ", "disposition.set",
               "2026-10-05T10:01:00.000000+00:00", code="nonsense")
    _add_event(conn, "e6", "c3", "test_flow", None, "disposition.override",
               "2026-10-05T10:02:00.000000+00:00", code="renewed")
    _add_event(conn, "e7", "c4", "test_flow", "hashB", "call.initiated",
               "2026-10-05T10:05:00.000000+00:00")
    _add_event(conn, "e8", "c4", "test_flow", "hashA", "call.connected",
               "2026-10-05T10:01:00.000000+00:00")  # earlier sibling -> lead A
    _add_event(conn, "e9", "c4", "test_flow", None, "disposition.override",
               "2026-10-05T10:06:00.000000+00:00", code="renewed")
    _add_event(conn, "e10", "c1", "test_flow", "hashA", "disposition.set",
               "2026-10-05T10:03:00.000000+00:00", code="nonsense")
    conn.commit()

    events = {e["event_id"]: e for e in link_events(conn)}
    summary = classify(list(events.values()), CATALOGUE)

    checks = [
        ("ordinary event links by workflow + phone hash",
         events["e1"]["lead_key"] == ("test_flow", "hashA")),
        ("override links through call_id to its call's lead",
         events["e3"]["lead_key"] == ("test_flow", "hashA")),
        ("event with unknown phone is an orphan",
         events["e4"]["lead_key"] is None),
        ("override without a linked sibling is an orphan",
         events["e6"]["lead_key"] is None),
        ("tie: override takes the EARLIEST linked sibling's lead",
         events["e9"]["lead_key"] == ("test_flow", "hashA")),
        ("counts: 7 linked, 3 orphans, 1 orphan override",
         (summary["linked"], summary["orphans"],
          summary["orphan_overrides"]) == (7, 3, 1)),
        ("spec order: orphan with unknown code counts as orphan, not unknown",
         summary["unknown_dispositions"] == 1),  # only linked e10
    ]

    failed = 0
    for i, (name, ok) in enumerate(checks, start=1):
        print(("PASS  " if ok else "FAIL  ") + f"case {i}: {name}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        raise SystemExit(_run_tests())
    raise SystemExit(main(sys.argv))

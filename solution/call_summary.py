"""
Call summaries - Assignment A report foundation (read-only).

Groups each call's linked events into one call record and derives the
facts the report files will need. Still no report files, still no
database writes.

Spec rules applied (README "Derived facts"):
- A call counts only if it has a linked call.initiated. The call's date
  is the IST date of that event (the ledger stores UTC; IST is +05:30).
- A call is connected if it has a call.connected event, and
  telephony_failed if it has a call.telephony_failed event. Orphan
  events stay out of every report, so only linked events set flags.
- Effective disposition: the latest KNOWN disposition.override by
  occurred_at; if there is no known override, the latest known
  disposition.set; otherwise the call has no outcome. Unknown codes
  were counted by event_linking and are ignored here - an unknown
  override cannot beat a known set.
- Lead bucket, first match wins: yet_to_reach_out (no calls),
  completed (any call's effective disposition is terminal),
  in_progress_telephony_failed (every call telephony-failed),
  in_progress_dialed (everything else).

Deterministic tie rules, documented for SUBMISSION.md:
- More than one call.initiated for a call: the EARLIEST
  (occurred_at, event_id) sets the call's time and IST date.
- Two valid dispositions of the winning kind with identical
  occurred_at: the larger event_id wins.
- If a call's linked events ever point at different leads, the call
  belongs to the lead of its earliest linked call.initiated.

Nothing here writes to the ledger: like linking, outcomes are read-time
derivations over the append-only history.

Usage (from the project's main folder):
    python solution/call_summary.py
    python solution/call_summary.py --dispositions assignment-a-data/schemas/dispositions.csv

Run the tests:
    python solution/call_summary.py --test
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db_setup import DEFAULT_DB, create_database
from event_linking import (DEFAULT_DISPOSITIONS, classify, link_events,
                           load_dispositions)

IST = timezone(timedelta(hours=5, minutes=30))


def summarize_calls(events, catalogue):
    """Group linked events into call records. Returns (calls, stats).

    Only linked events take part (orphans are out of every report).
    Each call: call_id, workflow_id, lead_key, initiated_at (UTC ISO),
    date_ist, connected, telephony_failed, disposition (None or dict
    with code, source set/override, occurred_at, is_terminal,
    is_success).
    """
    by_call = {}
    stats = {"groups": 0, "groups_without_initiated": 0,
             "events_in_non_call_groups": 0, "conflicting_lead_calls": 0,
             "orphan_events_excluded": 0}
    for ev in events:
        if ev["lead_key"] is None:
            stats["orphan_events_excluded"] += 1
            continue
        by_call.setdefault(ev["call_id"], []).append(ev)
    stats["groups"] = len(by_call)

    calls = []
    for cid, evs in sorted(by_call.items()):
        initiated = sorted(
            (e for e in evs if e["event_type"] == "call.initiated"),
            key=lambda e: (e["occurred_at"], e["event_id"]))
        if not initiated:
            stats["groups_without_initiated"] += 1
            stats["events_in_non_call_groups"] += len(evs)
            continue
        first = initiated[0]  # earliest initiated sets time and date
        if len({e["lead_key"] for e in evs}) > 1:
            stats["conflicting_lead_calls"] += 1

        def known(etype):
            return sorted(
                (e for e in evs if e["event_type"] == etype
                 and (e["workflow_id"], e["payload"].get("disposition_code"))
                 in catalogue),
                key=lambda e: (e["occurred_at"], e["event_id"]))

        overrides, sets = known("disposition.override"), known("disposition.set")
        chosen, source = (overrides[-1], "override") if overrides else \
                         ((sets[-1], "set") if sets else (None, None))
        disposition = None
        if chosen is not None:
            code = chosen["payload"]["disposition_code"]
            entry = catalogue[(chosen["workflow_id"], code)]
            disposition = {"code": code, "source": source,
                           "occurred_at": chosen["occurred_at"],
                           "is_terminal": entry["is_terminal"],
                           "is_success": entry["is_success"]}

        calls.append({
            "call_id": cid,
            "workflow_id": first["workflow_id"],
            "lead_key": first["lead_key"],
            "initiated_at": first["occurred_at"],
            "date_ist": datetime.fromisoformat(first["occurred_at"])
                        .astimezone(IST).date().isoformat(),
            "connected": any(e["event_type"] == "call.connected" for e in evs),
            "telephony_failed": any(e["event_type"] == "call.telephony_failed"
                                    for e in evs),
            "disposition": disposition,
        })
    return calls, stats


def lead_bucket(calls):
    """The lead's bucket from its call records, in spec order."""
    if not calls:
        return "yet_to_reach_out"
    if any(c["disposition"] and c["disposition"]["is_terminal"] for c in calls):
        return "completed"
    if all(c["telephony_failed"] for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


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
    catalogue = load_dispositions(disp_path)
    calls, stats = summarize_calls(events, catalogue)

    by_source = {"set": 0, "override": 0, "none": 0}
    terminal = success = connected = failed = 0
    for c in calls:
        connected += c["connected"]
        failed += c["telephony_failed"]
        if c["disposition"] is None:
            by_source["none"] += 1
        else:
            by_source[c["disposition"]["source"]] += 1
            terminal += c["disposition"]["is_terminal"]
            success += c["disposition"]["is_success"]

    print("CALL SUMMARY (read-only, counts only)")
    print(f"  linked call groups:        {stats['groups']}")
    print(f"  reportable calls:          {len(calls)}  (linked call.initiated)")
    print(f"  connected:                 {connected}")
    print(f"  telephony_failed:          {failed}")
    print(f"  with effective disposition:{by_source['set'] + by_source['override']}"
          f"  (set: {by_source['set']}, override: {by_source['override']})")
    print(f"    of which terminal:       {terminal}")
    print(f"    of which success:        {success}")
    print(f"  without a known outcome:   {by_source['none']}")
    print(f"  excluded: orphan events {stats['orphan_events_excluded']},"
          f" groups without initiated {stats['groups_without_initiated']}"
          f" ({stats['events_in_non_call_groups']} events)")
    if stats["conflicting_lead_calls"]:
        print(f"  note: {stats['conflicting_lead_calls']} call(s) had events pointing"
              " at different leads; assigned to the earliest initiated's lead")
    ok1 = sum(by_source.values()) == len(calls)
    ok2 = len(calls) + stats["groups_without_initiated"] == stats["groups"]
    ok3 = connected <= len(calls) and failed <= len(calls)
    for name, ok in [("sources add up to total calls", ok1),
                     ("calls + non-call groups == all groups", ok2),
                     ("flags within bounds", ok3)]:
        print(("PASS  " if ok else "FAIL  ") + name)
    return 0 if (ok1 and ok2 and ok3) else 1


# --- tests -----------------------------------------------------------------
CATALOGUE = {
    ("test_flow", "renewed"): {"label": "Renewed", "is_terminal": True,
                               "is_success": True},
    ("test_flow", "not_interested"): {"label": "Not interested",
                                      "is_terminal": True,
                                      "is_success": False},
    ("test_flow", "callback"): {"label": "Callback", "is_terminal": False,
                                "is_success": False},
}


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
    T = "test_flow"
    # c1: basic call; 20:30 UTC is 02:00 IST the NEXT day (date shift)
    _add_event(conn, "e01", "c1", T, "hashA", "call.initiated",
               "2026-10-05T20:30:00.000000+00:00")
    _add_event(conn, "e02", "c1", T, "hashA", "call.connected",
               "2026-10-05T20:31:00.000000+00:00")
    _add_event(conn, "e03", "c1", T, "hashA", "disposition.set",
               "2026-10-05T20:35:00.000000+00:00", code="renewed")
    # c2: later override beats set; latest override beats earlier override
    _add_event(conn, "e10", "c2", T, "hashA", "call.initiated",
               "2026-10-05T10:00:00.000000+00:00")
    _add_event(conn, "e11", "c2", T, "hashA", "disposition.set",
               "2026-10-05T10:01:00.000000+00:00", code="callback")
    _add_event(conn, "e12", "c2", T, None, "disposition.override",
               "2026-10-05T10:02:00.000000+00:00", code="not_interested")
    _add_event(conn, "e13", "c2", T, None, "disposition.override",
               "2026-10-05T10:03:00.000000+00:00", code="renewed")
    # c3: unknown codes are ignored - an unknown override cannot beat a known set
    _add_event(conn, "e20", "c3", T, "hashA", "call.initiated",
               "2026-10-05T11:00:00.000000+00:00")
    _add_event(conn, "e21", "c3", T, "hashA", "disposition.set",
               "2026-10-05T11:01:00.000000+00:00", code="renewed")
    _add_event(conn, "e22", "c3", T, "hashA", "disposition.set",
               "2026-10-05T11:02:00.000000+00:00", code="bogus")
    _add_event(conn, "e23", "c3", T, None, "disposition.override",
               "2026-10-05T11:03:00.000000+00:00", code="bogus2")
    # c4: telephony failure, no outcome
    _add_event(conn, "e30", "c4", T, "hashB", "call.initiated",
               "2026-10-05T12:00:00.000000+00:00")
    _add_event(conn, "e31", "c4", T, "hashB", "call.telephony_failed",
               "2026-10-05T12:00:30.000000+00:00")
    # c5: events but NO initiated -> not a call
    _add_event(conn, "e40", "c5", T, "hashA", "call.connected",
               "2026-10-05T09:00:00.000000+00:00")
    _add_event(conn, "e41", "c5", T, "hashA", "disposition.set",
               "2026-10-05T09:01:00.000000+00:00", code="renewed")
    # c6: orphan connected event (unknown phone) must NOT flag the call
    _add_event(conn, "e50", "c6", T, "hashA", "call.initiated",
               "2026-10-05T13:00:00.000000+00:00")
    _add_event(conn, "e51", "c6", T, "hashZZZ", "call.connected",
               "2026-10-05T13:01:00.000000+00:00")
    # c7: identical occurred_at on two valid sets -> larger event_id wins
    _add_event(conn, "e60", "c7", T, "hashB", "call.initiated",
               "2026-10-05T14:00:00.000000+00:00")
    _add_event(conn, "e61", "c7", T, "hashB", "disposition.set",
               "2026-10-05T14:01:00.000000+00:00", code="renewed")
    _add_event(conn, "e62", "c7", T, "hashB", "disposition.set",
               "2026-10-05T14:01:00.000000+00:00", code="not_interested")
    # c8: orphan initiated (no lead) -> not a call
    _add_event(conn, "e70", "c8", T, "hashZZZ", "call.initiated",
               "2026-10-05T15:00:00.000000+00:00")
    conn.commit()

    events = link_events(conn)
    calls, stats = summarize_calls(events, CATALOGUE)
    by_id = {c["call_id"]: c for c in calls}

    failed_only = [{"disposition": None, "telephony_failed": True}]
    plain = [{"disposition": None, "telephony_failed": False}]

    checks = [
        ("call date is the IST date of call.initiated (UTC midnight shift)",
         by_id["c1"]["date_ist"] == "2026-10-06"),
        ("basic call: connected, outcome from the known set",
         by_id["c1"]["connected"] and
         (by_id["c1"]["disposition"]["code"],
          by_id["c1"]["disposition"]["source"]) == ("renewed", "set")),
        ("latest valid override beats set and earlier overrides",
         (by_id["c2"]["disposition"]["code"],
          by_id["c2"]["disposition"]["source"]) == ("renewed", "override")),
        ("unknown codes ignored: unknown override cannot beat a known set",
         (by_id["c3"]["disposition"]["code"],
          by_id["c3"]["disposition"]["source"]) == ("renewed", "set")),
        ("no initiated -> not a call (c5, c8 excluded)",
         "c5" not in by_id and "c8" not in by_id and len(calls) == 6),
        ("orphan events never set flags: c6 not connected",
         by_id["c6"]["connected"] is False),
        ("tie on occurred_at: larger event_id wins (e62 not_interested)",
         by_id["c7"]["disposition"]["code"] == "not_interested"),
        ("buckets: yet_to_reach_out / completed / failed / dialed",
         lead_bucket([]) == "yet_to_reach_out"
         and lead_bucket([c for c in calls if c["lead_key"] == (T, "hashA")])
         == "completed"
         and lead_bucket(failed_only) == "in_progress_telephony_failed"
         and lead_bucket(plain) == "in_progress_dialed"),
        ("counts: 6 calls, 1 connected, 1 failed, sources 3/1/2, success 3",
         len(calls) == 6
         and sum(c["connected"] for c in calls) == 1
         and sum(c["telephony_failed"] for c in calls) == 1
         and sum(1 for c in calls if c["disposition"]
                 and c["disposition"]["source"] == "set") == 3
         and sum(1 for c in calls if c["disposition"]
                 and c["disposition"]["source"] == "override") == 1
         and sum(1 for c in calls if c["disposition"] is None) == 2
         and sum(1 for c in calls if c["disposition"]
                 and c["disposition"]["is_success"]) == 3),
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

"""
Report generator - Assignment A: writes the five report files.

Reads the database read-only, derives everything through event_linking
and call_summary, and writes the output contract's five files:

    batch_summary.csv    workflow_id,batch_id,total_leads,yet_to_reach_out,
                         in_progress_telephony_failed,in_progress_dialed,completed
                         - one row per (workflow, batch) that HAS leads; a batch
                           whose rows were all rejected appears only in
                         rejections.csv (documented for SUBMISSION.md).
                         Buckets per lead follow call_summary.lead_bucket.
    daily_overview.csv   workflow_id,date,total_calls,connected_calls,
                         connectivity_rate,success_leads
                         - one row per workflow per IST date WITH at least one
                           call; rate = connected/total with exactly 4 decimals;
                         success_leads = DISTINCT leads with a successful
                           effective disposition on that date.
    lead_report.csv      workflow_id,external_ref,batch_id,phone_masked,bucket,
                         total_calls,connected_calls,last_call_at,
                         current_disposition,ever_success
                         - phone_masked = +91****** + last4 (only masked form);
                         last_call_at = latest call.initiated in IST, seconds
                           precision (2026-10-14T17:55:24+05:30), empty if none;
                         current_disposition = effective disposition of the
                           most recent call THAT HAS ONE, empty if none;
                         ever_success as lowercase true/false.
    rejections.csv       workflow_id,batch_id,row_number,reason (no PII),
                         sorted by workflow, batch, row_number (numeric).
    data_quality.json    counters summed over every ingested file;
                         rejections_by_reason from the rejections table;
                         orphan_events and unknown_disposition_events are the
                         FINAL-state classifications from event_linking.

Sorting exactly as the contract states: batch_summary by
workflow_id,batch_id; daily_overview by workflow_id,date; lead_report by
workflow_id,external_ref; rejections by workflow_id,batch_id,row_number.
CSVs: header row, comma delimiter, \n line endings, no index column.
Generating is idempotent: same database -> byte-identical files.

Usage (from the project's main folder):
    python solution/report_generator.py --out reports
    python solution/report_generator.py --dispositions assignment-a-data/schemas/dispositions.csv --out reports

Run the tests:
    python solution/report_generator.py --test
"""

import csv
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from db_setup import DEFAULT_DB, create_database
from event_linking import (DEFAULT_DISPOSITIONS, classify, link_events,
                           load_dispositions)
from call_summary import IST, lead_bucket, summarize_calls

BATCH_HEADER = ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
                "in_progress_telephony_failed", "in_progress_dialed",
                "completed"]
DAILY_HEADER = ["workflow_id", "date", "total_calls", "connected_calls",
                "connectivity_rate", "success_leads"]
LEAD_HEADER = ["workflow_id", "external_ref", "batch_id", "phone_masked",
               "bucket", "total_calls", "connected_calls", "last_call_at",
               "current_disposition", "ever_success"]
REJECTION_HEADER = ["workflow_id", "batch_id", "row_number", "reason"]
BUCKETS = ["yet_to_reach_out", "in_progress_telephony_failed",
           "in_progress_dialed", "completed"]


def ist_timestamp(utc_iso: str) -> str:
    """2026-10-14T12:25:24.000000+00:00 -> 2026-10-14T17:55:24+05:30."""
    return (datetime.fromisoformat(utc_iso).astimezone(IST)
            .isoformat(timespec="seconds"))


def collect_reports(conn, catalogue):
    """Build every report's rows from the ledger. Returns (data, stats)."""
    events = link_events(conn)
    link_summary = classify(events, catalogue)
    calls, _ = summarize_calls(events, catalogue)

    calls_by_lead = {}
    for c in calls:
        calls_by_lead.setdefault(c["lead_key"], []).append(c)

    leads = conn.execute(
        "SELECT workflow_id, phone_hash, phone_last4, batch_id, external_ref"
        " FROM leads").fetchall()

    # --- lead_report + batch_summary -------------------------------------
    lead_rows, batches = [], {}
    for wf, ph, last4, batch, ref in leads:
        lc = sorted(calls_by_lead.get((wf, ph), []),
                    key=lambda c: (c["initiated_at"], c["call_id"]))
        bucket = lead_bucket(lc)
        with_disp = [c for c in lc if c["disposition"]]
        lead_rows.append([
            wf, ref, batch, "+91******" + last4, bucket,
            str(len(lc)),
            str(sum(c["connected"] for c in lc)),
            ist_timestamp(lc[-1]["initiated_at"]) if lc else "",
            with_disp[-1]["disposition"]["code"] if with_disp else "",
            "true" if any(c["disposition"] and c["disposition"]["is_success"]
                          for c in lc) else "false",
        ])
        row = batches.setdefault((wf, batch), [0, 0, 0, 0, 0])
        row[0] += 1
        row[1 + BUCKETS.index(bucket)] += 1
    lead_rows.sort(key=lambda r: (r[0], r[1]))
    batch_rows = [[wf, batch] + [str(n) for n in counts]
                  for (wf, batch), counts in sorted(batches.items())]

    # --- daily_overview ---------------------------------------------------
    daily = {}
    for c in calls:
        d = daily.setdefault((c["workflow_id"], c["date_ist"]),
                             {"total": 0, "connected": 0, "success": set()})
        d["total"] += 1
        d["connected"] += c["connected"]
        if c["disposition"] and c["disposition"]["is_success"]:
            d["success"].add(c["lead_key"])
    daily_rows = [[wf, day, str(d["total"]), str(d["connected"]),
                   f"{d['connected'] / d['total']:.4f}",
                   str(len(d["success"]))]
                  for (wf, day), d in sorted(daily.items())]

    # --- rejections --------------------------------------------------------
    rejection_rows = [[wf, batch, str(rn), reason] for wf, batch, rn, reason
                      in sorted(conn.execute(
                          "SELECT workflow_id, batch_id, row_number, reason"
                          " FROM rejections"),
                          key=lambda r: (r[0], r[1], r[2]))]

    # --- data_quality.json -------------------------------------------------
    (lt, lr, dm, fw, et, me, dd) = conn.execute(
        "SELECT COALESCE(SUM(lead_rows_total),0),"
        " COALESCE(SUM(lead_rows_rejected),0),"
        " COALESCE(SUM(duplicate_lead_rows_merged),0),"
        " COALESCE(SUM(field_warnings),0),"
        " COALESCE(SUM(events_total),0),"
        " COALESCE(SUM(malformed_events),0),"
        " COALESCE(SUM(duplicate_events_dropped),0)"
        " FROM ingested_files").fetchone()
    reasons = {r: str(n) for r, n in conn.execute(
        "SELECT reason, count(*) FROM rejections GROUP BY reason")}
    quality = {
        "lead_rows_total": lt,
        "lead_rows_rejected": lr,
        "rejections_by_reason": {k: int(reasons[k]) for k in sorted(reasons)},
        "duplicate_lead_rows_merged": dm,
        "field_warnings": fw,
        "events_total": et,
        "malformed_events": me,
        "duplicate_events_dropped": dd,
        "orphan_events": link_summary["orphans"],
        "unknown_disposition_events": link_summary["unknown_dispositions"],
    }

    data = {"batch_summary": batch_rows, "daily_overview": daily_rows,
            "lead_report": lead_rows, "rejections": rejection_rows,
            "data_quality": quality}
    stats = {"leads": len(leads), "calls": len(calls),
             "rejections": len(rejection_rows)}
    return data, stats


def write_reports(data, out_dir) -> list:
    """Write the five files. Returns the paths written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []

    def write_csv(name, header, rows):
        p = out / name
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
        paths.append(p)

    write_csv("batch_summary.csv", BATCH_HEADER, data["batch_summary"])
    write_csv("daily_overview.csv", DAILY_HEADER, data["daily_overview"])
    write_csv("lead_report.csv", LEAD_HEADER, data["lead_report"])
    write_csv("rejections.csv", REJECTION_HEADER, data["rejections"])
    q = out / "data_quality.json"
    with open(q, "w", encoding="utf-8") as f:
        json.dump(data["data_quality"], f, indent=2)
        f.write("\n")
    paths.append(q)
    return paths


def verify(data, stats, conn) -> list:
    """Cross-checks between the reports and the database."""
    batch = data["batch_summary"]
    ok_bucket_sums = all(int(r[2]) == sum(int(x) for x in r[3:]) for r in batch)
    ok_batch_total = sum(int(r[2]) for r in batch) == stats["leads"]
    leads = data["lead_report"]
    ok_lead_rows = len(leads) == stats["leads"]
    ok_call_totals = (sum(int(r[5]) for r in leads) == stats["calls"]
                      == sum(int(r[2]) for r in data["daily_overview"]))
    ok_conn_totals = (sum(int(r[6]) for r in leads)
                      == sum(int(r[3]) for r in data["daily_overview"]))
    db_rejections = conn.execute("SELECT count(*) FROM rejections").fetchone()[0]
    ok_rej = len(data["rejections"]) == db_rejections == stats["rejections"]
    q = data["data_quality"]
    ok_quality = (q["lead_rows_rejected"] == db_rejections
                  and sum(q["rejections_by_reason"].values()) == db_rejections)
    return [("batch buckets sum to total_leads per row", ok_bucket_sums),
            ("batch rows cover every lead", ok_batch_total),
            ("one lead_report row per lead", ok_lead_rows),
            ("call totals agree across lead/daily reports", ok_call_totals),
            ("connected totals agree across lead/daily reports", ok_conn_totals),
            ("rejections.csv matches the rejections table", ok_rej),
            ("data_quality counters match the rejections table", ok_quality)]


def main(argv) -> int:
    out_dir, disp_path = "reports", DEFAULT_DISPOSITIONS
    if "--out" in argv:
        out_dir = argv[argv.index("--out") + 1]
    if "--dispositions" in argv:
        disp_path = Path(argv[argv.index("--dispositions") + 1])
    if not Path(DEFAULT_DB).exists():
        raise SystemExit(f"Database not found: {DEFAULT_DB}\n"
                         "Run your ingestion commands first.")

    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
    try:
        data, stats = collect_reports(conn, load_dispositions(disp_path))
        checks = verify(data, stats, conn)
    finally:
        conn.close()
    paths = write_reports(data, out_dir)

    print("REPORT GENERATOR")
    for p in paths:
        rows = len(data[p.stem]) if p.suffix == ".csv" else None
        print(f"  wrote {p}" + (f"  ({rows} rows)" if rows is not None else ""))
    failed = 0
    for name, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + name)
        failed += 0 if ok else 1
    return 1 if failed else 0


# --- tests -----------------------------------------------------------------
CATALOGUE = {
    ("test_flow", "renewed"): {"label": "Renewed", "is_terminal": True,
                               "is_success": True},
    ("test_flow", "callback"): {"label": "Callback", "is_terminal": False,
                                "is_success": False},
}

EXPECTED = {
    "batch_summary.csv":
        "workflow_id,batch_id,total_leads,yet_to_reach_out,"
        "in_progress_telephony_failed,in_progress_dialed,completed\n"
        "test_flow,batch_001,2,0,1,0,1\n"
        "test_flow,batch_002,1,1,0,0,0\n",
    "daily_overview.csv":
        "workflow_id,date,total_calls,connected_calls,connectivity_rate,"
        "success_leads\n"
        "test_flow,2026-10-05,3,2,0.6667,1\n"
        "test_flow,2026-10-06,1,0,0.0000,0\n",
    "lead_report.csv":
        "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,"
        "connected_calls,last_call_at,current_disposition,ever_success\n"
        "test_flow,REF-A,batch_001,+91******0001,completed,3,2,"
        "2026-10-06T02:00:00+05:30,renewed,true\n"
        "test_flow,REF-B,batch_001,+91******0002,"
        "in_progress_telephony_failed,1,0,2026-10-05T16:30:00+05:30,,false\n"
        "test_flow,REF-C,batch_002,+91******0003,yet_to_reach_out,0,0,,,false\n",
    "rejections.csv":
        "workflow_id,batch_id,row_number,reason\n"
        "test_flow,batch_001,9,invalid_phone\n"
        "test_flow,batch_001,10,missing_required:customer_name\n"
        "test_flow,batch_002,3,invalid_phone\n",
    "data_quality.json":
        '{\n  "lead_rows_total": 9,\n  "lead_rows_rejected": 3,\n'
        '  "rejections_by_reason": {\n    "invalid_phone": 2,\n'
        '    "missing_required:customer_name": 1\n  },\n'
        '  "duplicate_lead_rows_merged": 1,\n  "field_warnings": 2,\n'
        '  "events_total": 12,\n  "malformed_events": 1,\n'
        '  "duplicate_events_dropped": 2,\n  "orphan_events": 1,\n'
        '  "unknown_disposition_events": 1\n}\n',
}


def _add_lead(conn, ph, last4, batch, ref):
    conn.execute(
        "INSERT INTO leads VALUES ('test_flow', ?, ?, ?, ?,"
        " NULL, NULL, NULL, NULL, NULL)", (ph, last4, batch, ref))


def _add_event(conn, eid, cid, ph, etype, occ, code=None):
    payload = {"disposition_code": code} if code else {}
    conn.execute(
        "INSERT INTO events VALUES (?, ?, 'test_flow', ?, ?, ?, ?)",
        (eid, cid, ph, etype, occ, json.dumps(payload)))


def _fixture(conn):
    _add_lead(conn, "hashA", "0001", "batch_001", "REF-A")
    _add_lead(conn, "hashB", "0002", "batch_001", "REF-B")
    _add_lead(conn, "hashC", "0003", "batch_002", "REF-C")
    # c1 (lead A): 10:00 UTC = 15:30 IST on 10-05; connected; renewed
    _add_event(conn, "e01", "c1", "hashA", "call.initiated",
               "2026-10-05T10:00:00.000000+00:00")
    _add_event(conn, "e02", "c1", "hashA", "call.connected",
               "2026-10-05T10:01:00.000000+00:00")
    _add_event(conn, "e03", "c1", "hashA", "disposition.set",
               "2026-10-05T10:05:00.000000+00:00", code="renewed")
    # c2 (lead A): 20:30 UTC = 02:00 IST on 10-06; no outcome
    _add_event(conn, "e04", "c2", "hashA", "call.initiated",
               "2026-10-05T20:30:00.000000+00:00")
    # c4 (lead A): second successful call on 10-05 -> success_leads stays 1
    _add_event(conn, "e09", "c4", "hashA", "call.initiated",
               "2026-10-05T12:00:00.000000+00:00")
    _add_event(conn, "e10", "c4", "hashA", "call.connected",
               "2026-10-05T12:01:00.000000+00:00")
    _add_event(conn, "e11", "c4", "hashA", "disposition.set",
               "2026-10-05T12:05:00.000000+00:00", code="renewed")
    # c3 (lead B): telephony failed; bogus code is a linked unknown disposition
    _add_event(conn, "e05", "c3", "hashB", "call.initiated",
               "2026-10-05T11:00:00.000000+00:00")
    _add_event(conn, "e06", "c3", "hashB", "call.telephony_failed",
               "2026-10-05T11:00:30.000000+00:00")
    _add_event(conn, "e07", "c3", "hashB", "disposition.set",
               "2026-10-05T11:05:00.000000+00:00", code="bogus")
    # orphan event (no lead for hashZZZ)
    _add_event(conn, "e08", "c9", "hashZZZ", "call.initiated",
               "2026-10-05T12:00:00.000000+00:00")
    # ingestion counters: two lead files + one events file
    conn.execute("INSERT INTO ingested_files VALUES"
                 " ('batch_001.csv','h1','t',5,2,1,1,0,0,0,0)")
    conn.execute("INSERT INTO ingested_files VALUES"
                 " ('batch_002.csv','h2','t',4,1,0,1,0,0,0,0)")
    conn.execute("INSERT INTO ingested_files VALUES"
                 " ('events_0001.jsonl','h3','t',0,0,0,0,12,1,2,0)")
    conn.execute("INSERT INTO rejections VALUES"
                 " ('batch_001.csv',10,'test_flow','batch_001',"
                 " 'missing_required:customer_name')")
    conn.execute("INSERT INTO rejections VALUES"
                 " ('batch_001.csv',9,'test_flow','batch_001','invalid_phone')")
    conn.execute("INSERT INTO rejections VALUES"
                 " ('batch_002.csv',3,'test_flow','batch_002','invalid_phone')")
    conn.commit()


def _run_tests() -> int:
    failed = 0

    def check(name, ok):
        nonlocal failed
        print(("PASS  " if ok else "FAIL  ") + name)
        failed += 0 if ok else 1

    conn = create_database(":memory:")
    _fixture(conn)
    data, stats = collect_reports(conn, CATALOGUE)
    for name, ok in verify(data, stats, conn):
        check(f"invariant: {name}", ok)

    with tempfile.TemporaryDirectory() as d1, \
            tempfile.TemporaryDirectory() as d2:
        write_reports(data, d1)
        for fname, expected in EXPECTED.items():
            actual = (Path(d1) / fname).read_text(encoding="utf-8")
            check(f"{fname} matches the contract byte-for-byte",
                  actual == expected)
        # idempotent regeneration: same database -> byte-identical files
        data2, _ = collect_reports(conn, CATALOGUE)
        write_reports(data2, d2)
        same = all((Path(d1) / f).read_bytes() == (Path(d2) / f).read_bytes()
                   for f in EXPECTED)
        check("regeneration is byte-identical", same)

    total = len(EXPECTED) + 8  # 7 invariants + 5 files + 1 determinism
    print(f"\n{total - failed}/{total} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        raise SystemExit(_run_tests())
    raise SystemExit(main(sys.argv))

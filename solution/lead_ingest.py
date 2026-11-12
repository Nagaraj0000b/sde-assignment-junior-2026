"""
Lead ingestion - Assignment A building block 6.

Connects the earlier units: read a lead CSV, validate every row, and
write the results into SQLite. Events are NOT handled here.

Usage (from the project's main folder):
    python solution/lead_ingest.py <schema.json> <leads.csv>
Example:
    python solution/lead_ingest.py assignment-a-data/schemas/motor_renewal.json data/drop_1/leads/motor_renewal/batch_001.csv

What it guarantees:
- MERGE: first valid row for (workflow_id, phone_hash) creates the lead
  and fixes batch_id forever. Each later valid row for the same identity
  counts as one merged duplicate and overwrites only the fields where
  its incoming value is non-null (last write wins). No defaults are
  stored; preferred_language stays NULL until a real value arrives.
- PRIVACY: only phone_hash + phone_last4 are stored; customer_name is
  validated and then dropped.
- ATOMIC: one transaction per file. Leads, rejections, counters and the
  ingested_files record are all written together or not at all.
- IDEMPOTENT: re-ingesting an unchanged file is skipped and changes
  nothing. If the file at a previously-ingested path has CHANGED, we
  refuse loudly instead of silently rewriting history (a deliberate
  design call - document it in SUBMISSION.md).
- The secret key must already exist (phone_hash.load_key); it is loaded
  BEFORE any database write, so a missing key means zero mutations.

Run the tests (they use synthetic CSVs and a test-only key):
    python solution/lead_ingest.py
"""

import hashlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from db_setup import create_database
from lead_reader import read_lead_rows
from phone_hash import load_key, phone_hash, phone_last4
from row_validator import validate_row

# Optional lead columns, in table order. external_ref merges too:
# it is required (never null on a valid row), so last write wins.
MERGEABLE_COLUMNS = ("external_ref", "policy_expiry", "premium_amount",
                     "product", "order_date", "preferred_language")


def ingest_file(conn, schema: dict, csv_path, key: bytes):
    """Ingest one lead CSV inside a single transaction.

    Returns the per-file counters dict, or the string "skipped" when the
    unchanged file was already ingested.
    """
    # resolve() so the same physical file has ONE identity whether it is
    # passed as data/drop_1/... or an absolute path or with ./ or ../
    csv_path = Path(csv_path).resolve()
    file_id = str(csv_path)
    content_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    workflow_id = schema["workflow_id"]
    batch_id = csv_path.stem
    ref_field = next(f["name"] for f in schema["fields"] if f.get("reference"))

    counters = {"lead_rows_total": 0, "lead_rows_rejected": 0,
                "duplicate_lead_rows_merged": 0, "field_warnings": 0}

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

        for row_number, record in read_lead_rows(csv_path, schema):
            counters["lead_rows_total"] += 1
            accepted, values, reason, warnings = validate_row(record, schema)

            if not accepted:
                conn.execute(
                    "INSERT INTO rejections "
                    "(source_file, row_number, workflow_id, batch_id, reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (file_id, row_number, workflow_id, batch_id, reason))
                counters["lead_rows_rejected"] += 1
                continue

            counters["field_warnings"] += warnings

            canonical = values["phone"]
            ph = phone_hash(canonical, key)
            col_vals = {}
            for f in schema["fields"]:
                name = f["name"]
                if name in ("customer_name", "phone") or f.get("reference"):
                    continue
                v = values.get(name)
                if name == "premium_amount" and v is not None:
                    v = int(v * 100)  # Decimal rupees -> integer paise
                col_vals[name] = v
            col_vals["external_ref"] = values[ref_field]

            found = conn.execute(
                "SELECT 1 FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                (workflow_id, ph)).fetchone()

            if found is None:
                conn.execute(
                    "INSERT INTO leads (workflow_id, phone_hash, phone_last4, "
                    "batch_id, external_ref, policy_expiry, premium_amount, "
                    "product, order_date, preferred_language) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (workflow_id, ph, phone_last4(canonical), batch_id,
                     col_vals.get("external_ref"), col_vals.get("policy_expiry"),
                     col_vals.get("premium_amount"), col_vals.get("product"),
                     col_vals.get("order_date"),
                     col_vals.get("preferred_language")))
            else:
                counters["duplicate_lead_rows_merged"] += 1
                # Last write wins for non-null incoming values only;
                # batch_id and the identity columns are never touched.
                sets, params = [], []
                for col in MERGEABLE_COLUMNS:
                    if col_vals.get(col) is not None:
                        sets.append(f"{col} = ?")
                        params.append(col_vals[col])
                if sets:
                    conn.execute(
                        f"UPDATE leads SET {', '.join(sets)} "
                        "WHERE workflow_id = ? AND phone_hash = ?",
                        (*params, workflow_id, ph))

        conn.execute(
            "INSERT INTO ingested_files "
            "(file_path, content_hash, processed_at, lead_rows_total, "
            "lead_rows_rejected, duplicate_lead_rows_merged, field_warnings) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, content_hash, datetime.now(timezone.utc).isoformat(),
             counters["lead_rows_total"], counters["lead_rows_rejected"],
             counters["duplicate_lead_rows_merged"],
             counters["field_warnings"]))

    return counters


def main(argv) -> int:
    if len(argv) != 3:
        print("Usage: python solution/lead_ingest.py <schema.json> <leads.csv>")
        return 2
    schema = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    key = load_key()  # BEFORE any database write: missing key, zero mutations
    conn = create_database()
    try:
        result = ingest_file(conn, schema, argv[2], key)
    finally:
        conn.close()
    if result == "skipped":
        print(f"{Path(argv[2]).name} was already ingested unchanged - "
              "skipped, nothing changed.")
    else:
        print(f"Ingested {Path(argv[2]).name} into {schema['workflow_id']}: "
              f"{result['lead_rows_total']} rows read, "
              f"{result['lead_rows_rejected']} rejected, "
              f"{result['duplicate_lead_rows_merged']} merged duplicates, "
              f"{result['field_warnings']} field warnings.")
    return 0


# --- tests -----------------------------------------------------------------
TEST_KEY = b"test-only-key-not-for-production"
TEST_SCHEMA = {
    "workflow_id": "test_flow",
    "fields": [
        {"name": "customer_name", "type": "string", "required": True,
         "aliases": ["name", "customer name"]},
        {"name": "phone", "type": "phone", "required": True,
         "aliases": ["mobile", "mobile no", "contact"]},
        {"name": "policy_number", "type": "string", "required": True,
         "reference": True, "aliases": ["policy no", "policy"]},
        {"name": "policy_expiry", "type": "date", "required": False,
         "aliases": ["expiry date", "expiry"]},
        {"name": "premium_amount", "type": "decimal", "required": False,
         "aliases": ["premium"]},
        {"name": "preferred_language", "type": "enum", "required": False,
         "values": ["en", "hi", "ta"], "default": "en",
         "aliases": ["language"]},
    ],
}

BATCH_001 = (
    "Customer Name,Mobile,Policy No,Expiry Date,Premium,Language\n"
    "Test Person,9876543210,POL-1,01/12/2026,Rs. 100.50,hi\n"     # row 1 valid
    "Bad Phone,12345,POL-2,,,\n"                                  # row 2 invalid_phone
    "Test Person,91-98765-43210,POL-1b,,Rs. 200,\n"               # row 3 dup: merges
    "Warn Person,9876543211,POL-3,31/02/2026,,\n"                 # row 4: 1 warning
    ",,,,,\n"                                                     # blank: not counted
)
BATCH_002 = (
    "Name,Contact,Policy,Expiry Date,Premium,Language\n"
    "Test Person,09876543210,POL-1c,02-Jan-2027,,ta\n"            # merges into lead
    "New Person,9876543212,POL-4,,,\n"                            # new lead
)


def _write(tmp, name, text):
    p = Path(tmp) / name
    p.write_text(text, encoding="utf-8")
    return p


def _run_tests() -> int:
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        f1 = _write(tmp, "batch_001.csv", BATCH_001)
        f2 = _write(tmp, "batch_002.csv", BATCH_002)
        conn = create_database(":memory:")
        c1 = ingest_file(conn, TEST_SCHEMA, f1, TEST_KEY)

        leads = conn.execute(
            "SELECT batch_id, external_ref, policy_expiry, premium_amount, "
            "preferred_language FROM leads").fetchall()
        lead1 = [r for r in leads if r[1] == "POL-1b"][0]

        checks.append(("first ingest counts: 4 read, 1 rejected, 1 merged, 1 warning",
                       c1 == {"lead_rows_total": 4, "lead_rows_rejected": 1,
                              "duplicate_lead_rows_merged": 1, "field_warnings": 1}))
        checks.append(("within-file merge: later non-null premium won (20000 paise), "
                       "earlier language kept (later was null), batch_id frozen",
                       lead1 == ("batch_001", "POL-1b", "2026-12-01", 20000, "hi")))
        checks.append(("rejected row stored with provenance, no PII",
                       conn.execute("SELECT workflow_id, batch_id, row_number, reason "
                                    "FROM rejections").fetchall()
                       == [("test_flow", "batch_001", 2, "invalid_phone")]))

        again = ingest_file(conn, TEST_SCHEMA, f1, TEST_KEY)
        checks.append(("unchanged re-run skipped, state unchanged",
                       again == "skipped"
                       and conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2
                       and conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1))

        # same physical file via a different lexical path must NOT count twice
        (Path(tmp) / "sub").mkdir()
        alias = Path(tmp) / "sub" / ".." / "batch_001.csv"
        checks.append(("same file via ../ alias path also skipped",
                       ingest_file(conn, TEST_SCHEMA, alias, TEST_KEY) == "skipped"
                       and conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1))

        c2 = ingest_file(conn, TEST_SCHEMA, f2, TEST_KEY)
        lead1 = conn.execute(
            "SELECT batch_id, external_ref, policy_expiry, premium_amount, "
            "preferred_language FROM leads WHERE external_ref = 'POL-1c'").fetchone()
        checks.append(("cross-file merge: new expiry + language won, premium kept, "
                       "batch_id still batch_001",
                       lead1 == ("batch_001", "POL-1c", "2027-01-02", 20000, "ta")))
        checks.append(("second file counters are its own",
                       c2["lead_rows_total"] == 2 and c2["duplicate_lead_rows_merged"] == 1))

        # changed file at the same path must be refused without mutation
        before = (conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0])
        f1.write_text(BATCH_001 + "Extra Person,9876543213,POL-9,,,\n", encoding="utf-8")
        try:
            ingest_file(conn, TEST_SCHEMA, f1, TEST_KEY)
            changed_refused = False
        except RuntimeError:
            changed_refused = True
        checks.append(("changed file at ingested path refused, nothing mutated",
                       changed_refused and before == (
                           conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
                           conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0])))

        # forced mid-file failure must roll back the whole file:
        # row 1 inserts a lead (uncommitted), row 2's rejection collides
        # with a conflicting row we planted beforehand
        f3 = _write(tmp, "batch_003.csv",
                    "Name,Contact,Policy\n"
                    "Good Person,9876543214,POL-5\n"
                    "Bad Person,999,POL-6\n")
        conn2 = create_database(":memory:")
        conn2.execute(
            "INSERT INTO rejections VALUES (?, ?, ?, ?, ?)",
            (str(f3), 2, "test_flow", "batch_003", "invalid_phone"))
        conn2.commit()
        try:
            ingest_file(conn2, TEST_SCHEMA, f3, TEST_KEY)
            rolled_back = False
        except sqlite3.IntegrityError:
            rolled_back = True
        checks.append(("mid-file error rolls back the entire file",
                       rolled_back
                       and conn2.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
                       and conn2.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 0
                       and conn2.execute("SELECT COUNT(*) FROM rejections").fetchone()[0] == 1))

    failed = 0
    for i, (name, ok) in enumerate(checks, start=1):
        print(("PASS  " if ok else "FAIL  ") + f"case {i}: {name}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) == 3:
        raise SystemExit(main(sys.argv))
    raise SystemExit(_run_tests())

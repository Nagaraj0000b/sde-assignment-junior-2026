"""
Lead CSV reader - Assignment A building block 4.

Reads a lead CSV and turns each row into a dict keyed by the SCHEMA's
field names. No validation and no database writes yet - this unit only
answers "which column is which, and what are the row's values?".

Spec rules it implements (README, "Lead files"):
- Header matching: lower-case the header, trim it, and replace every
  run of underscores or whitespace with a single space. Match the
  result against the field `name` or any of its `aliases` (normalised
  the same way). So "MOBILE_NO", "mobile no" and "  Mobile  No " all
  mean the schema field `phone`. Columns that match nothing are ignored.
- Blank rows (every cell empty or whitespace) are skipped and NOT counted.
- Row numbers count non-blank data rows from 1. The header is not counted.
- Values are trimmed. An empty value counts as missing -> None.

Run the tests:
    python solution/lead_reader.py
"""

import csv
import json
import re
import tempfile
from pathlib import Path

_HEADER_SEP = re.compile(r"[_\s]+")


def normalise_header(header: str) -> str:
    """Spec header normalisation: trim, lower-case, collapse _/space runs."""
    return _HEADER_SEP.sub(" ", str(header).strip().lower())


def build_header_map(schema: dict) -> dict:
    """Map every normalised name/alias -> the schema field name."""
    mapping = {}
    for field in schema["fields"]:
        mapping[normalise_header(field["name"])] = field["name"]
        for alias in field.get("aliases", []):
            mapping[normalise_header(alias)] = field["name"]
    return mapping


def read_lead_rows(csv_path, schema):
    """Yield (row_number, record) for each non-blank data row.

    record maps schema field names to trimmed values; missing values
    are None and unknown columns are left out.
    """
    header_map = build_header_map(schema)
    # utf-8-sig also tolerates a BOM, which Windows Excel CSVs often add.
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        headers = next(reader)  # the header row is not a data row
        columns = [header_map.get(normalise_header(h)) for h in headers]

        row_number = 0
        for raw in reader:
            values = [cell.strip() for cell in raw]
            if all(v == "" for v in values):
                continue  # blank row: skipped and not counted
            row_number += 1
            record = {}
            for field_name, value in zip(columns, values):
                # first matching column wins if two columns map to one field
                if field_name is not None and field_name not in record:
                    record[field_name] = value if value != "" else None
            yield row_number, record


# --- tests -----------------------------------------------------------------
# Synthetic schema and data only - no real customer data in tests.
TEST_SCHEMA = {
    "workflow_id": "test_flow",
    "fields": [
        {"name": "customer_name", "aliases": ["name", "customer name"]},
        {"name": "phone", "aliases": ["mobile", "mobile no", "contact"]},
        {"name": "policy_number", "aliases": ["policy no", "policy"]},
        {"name": "preferred_language", "aliases": ["language"]},
    ],
}

TEST_CSV = (
    "Customer Name,MOBILE_NO,Policy No,Junk Column\n"      # aliases + ignored col
    "  Test Person  , 9876543210 ,POL-1,zzz\n"             # trimmed on read
    ",, ,\n"                                               # blank row: skipped
    "Second Person,+91 98765 43210,POL-2,yyy\n"
)


def _run_tests() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "batch_001.csv"
        csv_path.write_text(TEST_CSV, encoding="utf-8")
        rows = list(read_lead_rows(csv_path, TEST_SCHEMA))

    checks = [
        ("blank row skipped, 2 data rows counted", len(rows) == 2),
        ("row numbers start at 1 and skip nothing",
         [n for n, _ in rows] == [1, 2]),
        ("aliases matched and values trimmed",
         rows[0][1].get("customer_name") == "Test Person"
         and rows[0][1].get("phone") == "9876543210"
         and rows[0][1].get("policy_number") == "POL-1"),
        ("unknown column ignored", "Junk Column" not in rows[0][1]
         and "junk_column" not in rows[0][1]),
        ("messy header MOBILE_NO still maps to phone",
         rows[1][1].get("phone") == "+91 98765 43210"),
        ("missing field reads as None",
         rows[0][1].get("preferred_language") is None),
        ("header itself is not counted as a row",
         all(rec.get("customer_name") != "Customer Name" for _, rec in rows)),
    ]

    failed = 0
    for i, (name, ok) in enumerate(checks, start=1):
        print(("PASS  " if ok else "FAIL  ") + f"case {i}: {name}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_tests())

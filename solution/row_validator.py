"""
Row validation - Assignment A building block 5.

Takes one record from lead_reader and checks it against the schema's
field rules. Still no database writes.

Spec rules it implements (README, "Field types" and "Rejection"):
- string:   the trimmed value.
- phone:    normalise_phone() from phone_utils.
- date:     YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY or DD-Mon-YYYY
            (month name case-insensitive). Dates are day-first.
            Impossible dates (31/02) are invalid. Stored as YYYY-MM-DD.
- decimal:  optional Rs/Rs./INR/rupee-symbol prefix (case-insensitive,
            optional space), commas are thousands separators, must be
            >= 0, rounded to 2 places.
- enum:     case-insensitive match against values, stored lower-case.

Rejection (walk fields in SCHEMA ORDER, first failure wins):
- required field missing           -> missing_required:<field_name>
- required phone present but bad   -> invalid_phone

Optional field present but invalid -> store None, count 1 field warning.
Warnings are counted only on rows that are NOT rejected.
Defaults (preferred_language -> en) are NOT applied here; they are
applied after merging, when the value is read.

Run the tests:
    python solution/row_validator.py
"""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from phone_utils import normalise_phone

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y")
_DECIMAL_PREFIX = re.compile(r"^\s*(?:₹|rs\.?|inr)\s*", re.IGNORECASE)


def parse_date(value: str):
    """Return the date as YYYY-MM-DD, or None if it is not a valid date."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_decimal(value: str):
    """Return the amount as a Decimal rounded to 2 places, or None."""
    cleaned = _DECIMAL_PREFIX.sub("", value).replace(",", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None  # NaN / Infinity sneak past Decimal(); reject them
    try:
        if amount < 0:
            return None
        # Spec doesn't name a rounding mode; we choose half-up and say so in
        # SUBMISSION.md. (Ingestion will store paise: int(amount * 100).)
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        # absurd exponents (e.g. 1e999999999) can't be quantised
        return None


def parse_value(field: dict, raw: str):
    """Normalise one present value by the field's type. None = invalid."""
    ftype = field["type"]
    if ftype == "string":
        return raw
    if ftype == "phone":
        return normalise_phone(raw)
    if ftype == "date":
        return parse_date(raw)
    if ftype == "decimal":
        return parse_decimal(raw)
    if ftype == "enum":
        lowered = raw.lower()
        return lowered if lowered in [v.lower() for v in field["values"]] else None
    return None  # unknown type in the schema: treat as invalid


def validate_row(record: dict, schema: dict):
    """Check one record against the schema.

    Returns (accepted, values, reason, warnings):
    - accepted: True/False
    - values:   field_name -> normalised value (None when rejected)
    - reason:   None, or missing_required:<field>, or invalid_phone
    - warnings: optional-field warning count (0 when rejected)
    """
    values = {}
    warnings = 0

    for field in schema["fields"]:  # schema order matters: first failure wins
        name = field["name"]
        raw = record.get(name)

        # Trim at the boundary: an empty value counts as missing, and
        # downstream parsers receive the trimmed string.
        raw = None if raw is None else str(raw).strip()
        if not raw:
            if field.get("required"):
                return False, None, f"missing_required:{name}", 0
            values[name] = None
            continue

        parsed = parse_value(field, raw)
        if parsed is not None:
            values[name] = parsed
            continue

        # present but invalid
        if field.get("required"):
            # The spec defines invalid_phone; required fields in these
            # schemas are otherwise strings, which always parse. If a
            # required non-phone value is ever unparseable we record it
            # as missing_required and document that call in SUBMISSION.md.
            reason = "invalid_phone" if field["type"] == "phone" \
                else f"missing_required:{name}"
            return False, None, reason, 0
        values[name] = None
        warnings += 1

    return True, values, None, warnings


# --- tests -----------------------------------------------------------------
# Synthetic schema + data only.
TEST_SCHEMA = {
    "workflow_id": "test_flow",
    "fields": [
        {"name": "customer_name", "type": "string", "required": True},
        {"name": "phone", "type": "phone", "required": True},
        {"name": "policy_number", "type": "string", "required": True},
        {"name": "policy_expiry", "type": "date", "required": False},
        {"name": "premium_amount", "type": "decimal", "required": False},
        {"name": "preferred_language", "type": "enum", "required": False,
         "values": ["en", "hi", "ta"], "default": "en"},
    ],
}


def _validate(**kwargs):
    return validate_row(kwargs, TEST_SCHEMA)


def _run_tests() -> int:
    base = {"customer_name": "Test Person", "phone": "9876543210",
            "policy_number": "POL-1"}

    ok = _validate(**base, policy_expiry="01-Dec-2026",
                   premium_amount="Rs. 1,250.50", preferred_language="HI")
    rej_missing = _validate(phone="9876543210", policy_number="POL-1")
    rej_first = _validate(phone="123", policy_number="POL-1")  # no name + bad phone
    rej_phone = _validate(**{**base, "phone": "12345"})
    warn_date = _validate(**base, policy_expiry="31/02/2026")
    warn_two = _validate(**base, policy_expiry="31/02/2026", premium_amount="-5")
    missing_optional = _validate(**base, policy_expiry="")
    dates = [_validate(**base, policy_expiry=v)[1]["policy_expiry"]
             for v in ("2026-12-01", "01/12/2026", "01-12-2026",
                       "01-Dec-2026", "01-dec-2026")]
    decimals = [_validate(**base, premium_amount=v)[1]["premium_amount"]
                for v in ("₹1,250.50", "Rs 1250.50", "Rs.1250.5", "INR 10")]

    nonfinite = [_validate(**base, premium_amount=v)
                 for v in ("NaN", "Infinity", "-Infinity")]
    huge = _validate(**base, premium_amount="1e999999999")
    padded = _validate(**{**base, "customer_name": "   Test Person   "})

    checks = [
        ("NaN / Infinity rejected as warnings, no crash",
         all(r[0] and r[1]["premium_amount"] is None and r[3] == 1
             for r in nonfinite)),
        ("absurd exponent rejected, no crash",
         huge[0] and huge[1]["premium_amount"] is None and huge[3] == 1),
        ("values trimmed at the boundary",
         padded[1]["customer_name"] == "Test Person"),
        ("valid row accepted with normalised values",
         ok[0] and ok[1]["phone"] == "+919876543210"
         and ok[1]["policy_expiry"] == "2026-12-01"
         and ok[1]["premium_amount"] == Decimal("1250.50")
         and ok[1]["preferred_language"] == "hi"
         and ok[3] == 0),
        ("missing required -> missing_required:<field>",
         rej_missing == (False, None, "missing_required:customer_name", 0)),
        ("schema order: missing name beats invalid phone",
         rej_first[2] == "missing_required:customer_name"),
        ("bad required phone -> invalid_phone",
         rej_phone[2] == "invalid_phone" and rej_phone[3] == 0),
        ("impossible optional date -> accepted, None, 1 warning",
         warn_date[0] and warn_date[1]["policy_expiry"] is None and warn_date[3] == 1),
        ("two bad optionals -> 2 warnings, row kept",
         warn_two[0] and warn_two[3] == 2
         and warn_two[1]["premium_amount"] is None),
        ("empty optional is missing, NOT a warning",
         missing_optional[0] and missing_optional[3] == 0),
        ("all four date formats -> same ISO date",
         dates == ["2026-12-01"] * 5),
        ("currency prefixes and commas handled",
         decimals == [Decimal("1250.50"), Decimal("1250.50"),
                      Decimal("1250.50"), Decimal("10.00")]),
    ]

    failed = 0
    for i, (name, result) in enumerate(checks, start=1):
        print(("PASS  " if result else "FAIL  ") + f"case {i}: {name}")
        failed += 0 if result else 1
    print(f"\n{len(checks) - failed}/{len(checks)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_tests())

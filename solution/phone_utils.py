"""
Phone normalisation - Assignment A building block 2.

Spec rule (README, lead field type `phone`):
1. Strip every non-digit character.
2. If the result is 12 digits starting with 91, drop the 91.
3. If it is 11 digits starting with 0, drop the 0.
4. It is valid only if what remains is exactly 10 digits
   starting with 6, 7, 8 or 9.
5. Canonical in-memory form: +91XXXXXXXXXX.
   (This is the form we HASH before anything touches the database;
   the full number is never stored or logged - it is PII.)

normalise_phone() returns the canonical string, or None when invalid.

We will call this ONE function for both lead CSV rows and event JSON
lines. That is how differently written numbers like "91-98765-43210"
and "9876543210" end up matching the same lead.

Run the tests at the bottom:
    python solution/phone_utils.py
"""
import re
_NON_DIGIT = re.compile(r"\D")


def normalise_phone(value):
    """Return the phone as +91XXXXXXXXXX, or None if it is invalid."""
    if value is None:
        return None

    digits = _NON_DIGIT.sub("", str(value))          # step 1

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]                          # step 2
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]                          # step 3

    if len(digits) == 10 and digits[0] in "6789":    # step 4
        return "+91" + digits                        # step 5
    return None


# --- tests -----------------------------------------------------------------
# Synthetic tutorial numbers only, never real event/lead data.
# (input, expected output). None means the input must be rejected.
TESTS = [
    # the six messy formats the generator produces
    ("9876543210", "+919876543210"),      # plain 10 digits
    ("91-98765-43210", "+919876543210"),  # dashes + 91
    ("+91 98765 43210", "+919876543210"), # plus and spaces
    ("09876543210", "+919876543210"),     # leading 0
    ("919876543210", "+919876543210"),    # 12 digits with 91
    ("+91-98765-43210", "+919876543210"), # plus, 91 and dashes
    # invalid ones
    ("1234567890", None),                 # 10 digits but starts with 1
    ("911234567890", None),               # 91 dropped, then starts with 1
    ("59876543210", None),                # 11 digits, no leading 0
    ("98765 4321", None),                 # only 9 digits
    ("", None),                           # empty
    (None, None),
]


if __name__ == "__main__":
    failed = 0
    for i, (raw, expected) in enumerate(TESTS, start=1):
        got = normalise_phone(raw)
        # print the case id only, never the phone value itself
        if got == expected:
            print(f"PASS  case {i}")
        else:
            failed += 1
            print(f"FAIL  case {i}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    raise SystemExit(1 if failed else 0)

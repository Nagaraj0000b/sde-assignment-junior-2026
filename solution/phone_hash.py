"""
Phone hashing - Assignment A building block 3.

The database stores phone_hash and phone_last4, never the full number:

    canonical phone          ->  phone_hash         (HMAC-SHA256 hex)
    "+919876543210"          ->  "4b2c..."          (64 hex characters)
                             ->  phone_last4 = "3210"  (for the masked report)

HMAC needs a secret key. The rules for that key:
- It lives in a small file called .hmac_key NEXT TO THIS FILE.
- It is created once, locally, by running:
      python solution/phone_hash.py --make-key
- Add .hmac_key to your .gitignore (also pipeline.db and data/).
  The key must NEVER go into Git, chat, or screenshots.
- The same key must be used for every run and every drop, otherwise the
  same phone hashes differently and leads stop matching events.
- If you lose or change the key, old database rows can no longer be
  matched to new input. Treat it like a password: keep a private backup.

There is deliberately NO automatic fallback key: if the key file is
missing, this module refuses to work rather than silently hashing with
a throwaway key.

Run the tests (they use a fixed, test-only key):
    python solution/phone_hash.py
"""
import hashlib
import hmac
import secrets
import sys
from pathlib import Path

KEY_PATH = Path(__file__).resolve().with_name(".hmac_key")


def generate_key_file() -> None:
    """Create the local secret key file once. Never prints the key."""
    if KEY_PATH.exists():
        print(".hmac_key already exists - keeping the current key.")
        return
    KEY_PATH.write_text(secrets.token_hex(32) + "\n")
    print("Created .hmac_key next to this file.")
    print("Add it to .gitignore now. Do not copy it anywhere public.")


def load_key() -> bytes:
    """Read the secret key, or fail loudly if it is missing."""
    if not KEY_PATH.exists():
        raise RuntimeError(
            "Missing .hmac_key. Create it once with: "
            "python solution/phone_hash.py --make-key"
        )
    return bytes.fromhex(KEY_PATH.read_text().strip())


def phone_hash(canonical_phone: str, key: bytes) -> str:
    """Stable HMAC-SHA256 fingerprint of a canonical +91XXXXXXXXXX phone."""
    return hmac.new(key, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()


def phone_last4(canonical_phone: str) -> str:
    """Last 4 digits, for the +91******XXXX masked report display."""
    return canonical_phone[-4:]


# --- tests -----------------------------------------------------------------
# A fixed, obviously synthetic key for tests only. Real runs use .hmac_key.
TEST_KEY = b"test-only-key-not-for-production"


def _run_tests() -> int:
    h1 = phone_hash("+919876543210", TEST_KEY)

    checks = [
        ("hash is stable for same phone + key",
         h1 == phone_hash("+919876543210", TEST_KEY)),
        ("hash is 64 hex characters",
         len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)),
        ("different phone -> different hash",
         h1 != phone_hash("+919876543211", TEST_KEY)),
        ("different key -> different hash",
         h1 != phone_hash("+919876543210", b"another-test-key")),
        ("last4 is the final 4 digits",
         phone_last4("+919876543210") == "3210"),
    ]

    failed = 0
    for i, (name, ok) in enumerate(checks, start=1):
        print(("PASS  " if ok else "FAIL  ") + f"case {i}: {name}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--make-key" in sys.argv:
        generate_key_file()
    else:
        raise SystemExit(_run_tests())

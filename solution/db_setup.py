import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().with_name("pipeline.db")

SCHEMA="""
---LEADs TABLE : current state of each customer. ater valid rows overwrite only non-null fields,But batch_id is fixed for all records 
CREATE TABLE IF NOT EXISTS leads (
    workflow_id         TEXT NOT NULL,
    phone_hash          TEXT NOT NULL,   -- HMAC fingerprint of the canonical +91 number
    phone_last4         TEXT NOT NULL,   -- for +91******XXXX display in lead_report
    batch_id            TEXT NOT NULL,   -- file stem of first valid row, e.g. batch_001
    external_ref        TEXT NOT NULL,   -- policy_number or order_id ("reference": true)
    policy_expiry       TEXT,            -- YYYY-MM-DD, motor_renewal only
    premium_amount      INTEGER,         -- paise, e.g. 1250.50 -> 125050. Exact, no float
                                         -- rounding. Parse with Decimal at ingest, then
                                         -- multiply by 100; convert back when reading.
    product             TEXT,            -- policy_feedback only
    order_date          TEXT,            -- YYYY-MM-DD, policy_feedback only
    preferred_language  TEXT,            -- raw merged value; NULL means "never supplied".
                                         -- Apply the 'en' default when READING, not here,
                                         -- so a later drop's real value can still win the merge.
    PRIMARY KEY (workflow_id, phone_hash)
    -- NOTE: no customer_name column. It is validated at ingest, then not stored.
);
--- EVENTS: the append-only interaction ledger.
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY NOT NULL,  -- re-seen event_id => duplicate, count and skip
    call_id     TEXT NOT NULL,              -- groups one call's events
    workflow_id TEXT NOT NULL,
    phone_hash  TEXT,                       -- NULL on disposition.override (it has no phone);
                                            -- link overrides through call_id instead
    event_type  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,              -- normalised to UTC at ingest; IST only in reports
    payload     TEXT NOT NULL               -- the event's payload as raw JSON text
);

-- Speeds up the two lookups reports and linking rely on:
CREATE INDEX IF NOT EXISTS idx_events_lead ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS idx_events_call ON events (call_id);

-- Append-only enforcement, not just a comment: any UPDATE or DELETE on
-- the ledger aborts. (create_database also enables recursive_triggers so
-- an INSERT OR REPLACE - which deletes the conflicting row under the
-- hood - is caught by the DELETE trigger as well.)
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: DELETE is not allowed');
END;
---INGESTED_FILES: finished-files register + per-file counters A row is written in the SAME transaction as the file's data
CREATE TABLE IF NOT EXISTS ingested_files (
    file_path                   TEXT PRIMARY KEY,  -- includes drop/workflow folders
    content_hash                TEXT NOT NULL,     -- fingerprint of the file contents
    processed_at                TEXT NOT NULL,
    lead_rows_total             INTEGER NOT NULL DEFAULT 0,
    lead_rows_rejected          INTEGER NOT NULL DEFAULT 0,
    duplicate_lead_rows_merged  INTEGER NOT NULL DEFAULT 0,
    field_warnings              INTEGER NOT NULL DEFAULT 0,
    events_total                INTEGER NOT NULL DEFAULT 0,  -- every non-empty line read
    malformed_events            INTEGER NOT NULL DEFAULT 0,
    duplicate_events_dropped    INTEGER NOT NULL DEFAULT 0,
    unknown_disposition_events  INTEGER NOT NULL DEFAULT 0
    -- orphan_events is NOT stored here: it is counted at report time
    -- (events still unmatched then), because later drops can fix orphans.
);
--  REJECTIONS: one row per rejected lead row.
--    source_file is internal provenance so the same batch stem and row
--    number in a different drop/file can never collide. rejections.csv
--    exports only (workflow_id, batch_id, row_number, reason) - no PII.
CREATE TABLE IF NOT EXISTS rejections (
    source_file TEXT NOT NULL,           -- internal: which file this row came from
    row_number  INTEGER NOT NULL,        -- non-blank data rows from 1, header excluded
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    reason      TEXT NOT NULL,           -- missing_required:<field> or invalid_phone
    UNIQUE (source_file, row_number)
);
"""

def create_database(path: str | None = None) -> sqlite3.Connection:
    """Create (or open) the database and make sure the tables exist.

    With no argument, uses pipeline.db next to this script.
    """
    db_path = str(path) if path is not None else str(DEFAULT_DB)
    conn = sqlite3.connect(db_path)
    # So INSERT OR REPLACE / UPDATE OR REPLACE cannot sneak past the
    # append-only triggers (their hidden DELETE/UPDATE only fires
    # triggers when recursive triggers are enabled).
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


if __name__ == "__main__":
    conn = create_database()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    print(f"{DEFAULT_DB.name} ready. Tables: {', '.join(tables)}")
    conn.close()

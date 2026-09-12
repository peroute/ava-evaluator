"""SQLite persistence for Step 1.

Only knows about two tables: step1_turns and step1_quarantine. No pipeline
logic lives here.
"""

import json
import sqlite3

DEFAULT_DB_PATH = "eval_store.db"

CREATE_TURNS_TABLE = """
CREATE TABLE IF NOT EXISTS step1_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    pair_index INTEGER NOT NULL,
    caller_asked TEXT NOT NULL,
    agent_said TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, pair_index)
)
"""

CREATE_QUARANTINE_TABLE = """
CREATE TABLE IF NOT EXISTS step1_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    reason TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# Update in place on conflict so the existing row (and its id) survives a
# re-run. INSERT OR REPLACE would delete and recreate the row instead.
UPSERT_TURN = """
INSERT INTO step1_turns (session_id, pair_index, caller_asked, agent_said)
VALUES (?, ?, ?, ?)
ON CONFLICT (session_id, pair_index) DO UPDATE SET
    caller_asked = excluded.caller_asked,
    agent_said = excluded.agent_said
"""

INSERT_QUARANTINE = """
INSERT INTO step1_quarantine (session_id, reason, raw_payload)
VALUES (?, ?, ?)
"""


def connect(db_path=DEFAULT_DB_PATH):
    """Open a connection with foreign keys on and rows accessible by name."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(connection):
    """Create the Step 1 tables if they do not exist yet."""
    connection.execute(CREATE_TURNS_TABLE)
    connection.execute(CREATE_QUARANTINE_TABLE)
    connection.commit()


def upsert_turns(connection, turns):
    """Insert or update normalized turns. Returns the number of turns written."""
    rows = [
        (turn["session_id"], turn["pair_index"], turn["caller_asked"], turn["agent_said"])
        for turn in turns
    ]
    connection.executemany(UPSERT_TURN, rows)
    connection.commit()
    return len(rows)


def insert_quarantine(connection, session_id, reason, raw_payload):
    """Record a rejected conversation (or unreadable file) for later inspection."""
    if not isinstance(raw_payload, str):
        raw_payload = json.dumps(raw_payload, ensure_ascii=False, default=str)
    connection.execute(INSERT_QUARANTINE, (session_id, reason, raw_payload))
    connection.commit()


def count_turns(connection, session_id=None):
    """Small helper used by the summary output and the tests."""
    if session_id is None:
        cursor = connection.execute("SELECT COUNT(*) FROM step1_turns")
    else:
        cursor = connection.execute(
            "SELECT COUNT(*) FROM step1_turns WHERE session_id = ?", (session_id,)
        )
    return cursor.fetchone()[0]

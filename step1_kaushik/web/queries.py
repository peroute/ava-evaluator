"""Read-only SQLite queries for the Step 1 inspection dashboard.

Deliberately separate from src/database.py: that module owns writes for the
pipeline, this one only ever reads. Every connection is opened in SQLite's
read-only mode, so a bug here cannot modify eval_store.db.
"""

import os
import sqlite3

DEFAULT_DB_PATH = "eval_store.db"


class DatabaseMissing(Exception):
    """Raised when eval_store.db does not exist yet (Step 1 never ran)."""


def resolve_db_path(db_path=None):
    """Pick the database file: explicit argument, then EVAL_DB_PATH, then default."""
    return db_path or os.environ.get("EVAL_DB_PATH") or DEFAULT_DB_PATH


def connect_read_only(db_path):
    """Open db_path read-only. SQLite rejects any write through this handle."""
    if not os.path.isfile(db_path):
        raise DatabaseMissing("database not found: %s" % db_path)

    uri = "file:%s?mode=ro" % db_path.replace("?", "%3f").replace("#", "%23")
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(connection, sql, params=()):
    """Run a parameterized query; treat a not-yet-created table as empty."""
    try:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return []
        raise


def get_summary(connection):
    """Counts for the overview panel."""
    turns = _rows(
        connection,
        "SELECT COUNT(*) AS turns, COUNT(DISTINCT session_id) AS conversations"
        " FROM step1_turns",
    )
    quarantine = _rows(connection, "SELECT COUNT(*) AS quarantined FROM step1_quarantine")

    return {
        "conversations_accepted": turns[0]["conversations"] if turns else 0,
        "turns_stored": turns[0]["turns"] if turns else 0,
        "quarantined_records": quarantine[0]["quarantined"] if quarantine else 0,
    }


def list_conversations(connection):
    """One entry per unique session_id, with its turn count."""
    return _rows(
        connection,
        "SELECT session_id,"
        "       COUNT(*) AS turn_count,"
        "       MIN(created_at) AS first_seen_at"
        " FROM step1_turns"
        " GROUP BY session_id"
        " ORDER BY session_id",
    )


def get_conversation_turns(connection, session_id):
    """Turns for one session in pair_index order. Empty list if unknown."""
    return _rows(
        connection,
        "SELECT pair_index, caller_asked, agent_said, created_at"
        " FROM step1_turns"
        " WHERE session_id = ?"
        " ORDER BY pair_index",
        (session_id,),
    )


def list_quarantine(connection):
    """Quarantined records, newest first."""
    return _rows(
        connection,
        "SELECT id, session_id, reason, raw_payload, created_at"
        " FROM step1_quarantine"
        " ORDER BY id DESC",
    )

"""Step 1 pipeline: ingest -> validate -> normalize -> store (or quarantine).

Run with:  python main.py
"""

import json

from src import database
from src.ingest import (
    DEFAULT_INCOMING_DIR,
    IngestError,
    iter_incoming_files,
    load_conversations,
)
from src.normalize import normalize_conversation
from src.validation import validate_conversation


def run_step1(db_path=database.DEFAULT_DB_PATH, incoming_dir=DEFAULT_INCOMING_DIR):
    """Process every JSON file in incoming_dir. Returns a summary dict."""
    summary = {
        "files_processed": 0,
        "files_failed": 0,
        "conversations_accepted": 0,
        "conversations_quarantined": 0,
        "turns_stored": 0,
    }

    connection = database.connect(db_path)
    try:
        database.init_db(connection)

        for path in iter_incoming_files(incoming_dir):
            summary["files_processed"] += 1
            try:
                conversations = load_conversations(path)
            except IngestError as error:
                # A bad file is quarantined, then we move on to the next one.
                summary["files_failed"] += 1
                summary["conversations_quarantined"] += 1
                database.insert_quarantine(connection, None, "%s (%s)" % (error, path), path)
                continue

            for conversation in conversations:
                _process_conversation(connection, conversation, summary)
    finally:
        connection.close()

    return summary


def _process_conversation(connection, conversation, summary):
    """Validate one conversation and either store or quarantine it."""
    is_valid, reason = validate_conversation(conversation)
    if not is_valid:
        summary["conversations_quarantined"] += 1
        database.insert_quarantine(
            connection,
            _best_effort_session_id(conversation),
            reason,
            json.dumps(conversation, ensure_ascii=False, default=str),
        )
        return

    turns = normalize_conversation(conversation)
    summary["turns_stored"] += database.upsert_turns(connection, turns)
    summary["conversations_accepted"] += 1


def _best_effort_session_id(conversation):
    """Session id for the quarantine row, or None if it is missing/unusable."""
    if isinstance(conversation, dict):
        session_id = conversation.get("provider_session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
    return None


def print_summary(summary):
    print("Step 1 complete")
    print("  Files processed:           %d" % summary["files_processed"])
    print("  Files unreadable:          %d" % summary["files_failed"])
    print("  Conversations accepted:    %d" % summary["conversations_accepted"])
    print("  Conversations quarantined: %d" % summary["conversations_quarantined"])
    print("  Turns stored:              %d" % summary["turns_stored"])


if __name__ == "__main__":
    print_summary(run_step1())

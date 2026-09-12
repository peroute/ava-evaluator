"""Step 1 sources, raw payload preservation, and reprocessing.

The validation rules themselves are covered in test_ingest.py. These tests are
about the promises layered on top: the provider's original payload is kept, any
source feeds the same validation, and re-ingesting a session always leaves the
database describing its *latest* version, never a mix of old and new.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from ava_eval.config import PROJECT_ROOT
from ava_eval.db import store
from ava_eval.models import ConversationStatus
from ava_eval.steps.s1_ingest import (
    FetchedConversation,
    JsonTranscriptSource,
    TranscriptSource,
    ingest,
    ingest_directory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pair(index: int, asked: str = "q", said: str = "a") -> dict:
    """Build one well-formed pair."""
    return {"pair_index": index, "caller_asked": asked, "agent_said": said}


def _conversation(session_id: str = "conv_1", pairs: list[dict] | None = None, **extra) -> dict:
    """Build an export-v1 payload, optionally with extra provider fields."""
    payload = {
        "provider_session_id": session_id,
        "raw_pairs": pairs if pairs is not None else [_pair(0), _pair(1)],
    }
    payload.update(extra)
    return payload


class FakeApiSource(TranscriptSource):
    """A stand-in provider API whose response shape differs from the export.

    A ``str`` item simulates a response that could not be decoded.
    """

    name = "fake_api"

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses

    def fetch_conversations(self) -> Iterator[FetchedConversation]:
        for position, response in enumerate(self.responses):
            if isinstance(response, str):
                yield FetchedConversation(
                    origin=f"api#{position}", source_type=self.name, error="undecodable response"
                )
                continue
            yield FetchedConversation(
                origin=f"api:{response.get('conversation_id')}",
                source_type=self.name,
                position=position,
                raw_payload=response,
                canonical_payload=self.to_canonical(response),
            )

    @staticmethod
    def to_canonical(response: dict) -> dict:
        """Pair each user message with the agent reply after it. Maps, never validates."""
        messages = response.get("transcript", [])
        pairs = []
        for number, start in enumerate(range(0, len(messages), 2)):
            user = messages[start]
            agent = messages[start + 1] if start + 1 < len(messages) else {}
            pairs.append(
                {
                    "pair_index": number,
                    "caller_asked": user.get("message"),
                    "agent_said": agent.get("message"),
                }
            )
        return {"provider_session_id": response.get("conversation_id"), "raw_pairs": pairs}


def _api_response(conversation_id: str = "abc123", exchanges: list[tuple] | None = None) -> dict:
    """A provider response in the fake API's own shape."""
    if exchanges is None:
        exchanges = [("What GPA do I need?", "You need a 2.0 GPA."), ("Deadline?", "August 1.")]
    transcript = []
    for asked, said in exchanges:
        transcript.append({"role": "user", "message": asked})
        transcript.append({"role": "agent", "message": said})
    return {
        "conversation_id": conversation_id,
        "agent_id": "agent_1",
        "metadata": {"duration": 120},
        "transcript": transcript,
    }


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """An empty transcripts directory."""
    path = tmp_path / "transcripts"
    path.mkdir()
    return path


def _write(directory: Path, name: str, payload: Any) -> Path:
    path = directory / name
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def _record(db_path: Path, session_id: str) -> dict | None:
    with store.connect(db_path) as conn:
        return store.get_conversation_record(conn, session_id)


def _turns(db_path: Path, session_id: str | None = None) -> list[tuple]:
    with store.connect(db_path) as conn:
        return [
            (t.session_id, t.pair_index, t.caller_asked, t.agent_said)
            for t in store.get_turns(conn, session_id)
        ]


def _count(db_path: Path, table: str) -> int:
    with store.connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------------------
# Raw transcript storage
# ---------------------------------------------------------------------------


def test_raw_payload_is_preserved_with_extra_metadata(source_dir: Path, db_path: Path) -> None:
    original = _conversation(
        pairs=[_pair(0, asked="  padded  ")],
        metadata={"agent": "ava", "duration_secs": 12},
        call_outcome="resolved",
    )
    _write(source_dir, "c.json", original)
    ingest_directory(source_dir, db_path=db_path)

    record = _record(db_path, "conv_1")
    assert json.loads(record["raw_payload"]) == original
    # The normalised turn is trimmed; the raw copy is not.
    assert _turns(db_path) == [("conv_1", 0, "padded", "a")]


def test_source_type_is_stored(source_dir: Path, db_path: Path) -> None:
    _write(source_dir, "c.json", _conversation("from_json"))
    ingest_directory(source_dir, db_path=db_path)
    ingest(FakeApiSource([_api_response("from_api")]), db_path=db_path)

    assert _record(db_path, "from_json")["source_type"] == "json_export"
    api = _record(db_path, "from_api")
    assert api["source_type"] == "fake_api"
    assert api["source_file"] == "api:from_api"


def test_raw_payload_is_stored_as_json_text(source_dir: Path, db_path: Path) -> None:
    _write(source_dir, "c.json", _conversation())
    ingest_directory(source_dir, db_path=db_path)
    with store.connect(db_path) as conn:
        kind = conn.execute("SELECT typeof(raw_payload) FROM conversations").fetchone()[0]
    assert kind == "text"


def test_rejected_conversation_keeps_raw_payload_and_reason(
    source_dir: Path, db_path: Path
) -> None:
    bad = _conversation("bad_1", pairs=[_pair(0), _pair(2)])
    _write(source_dir, "bad.json", bad)
    ingest_directory(source_dir, db_path=db_path)

    record = _record(db_path, "bad_1")
    assert record["status"] == ConversationStatus.REJECTED.value
    assert "sequential" in record["note"]
    assert json.loads(record["raw_payload"]) == bad


def test_rejected_payload_without_session_id_is_kept_under_fallback_id(
    source_dir: Path, db_path: Path
) -> None:
    payload = {"raw_pairs": [_pair(0)], "provider_metadata": {"x": 1}}
    _write(source_dir, "nameless.json", payload)
    report = ingest_directory(source_dir, db_path=db_path)

    assert [r.session_id for r in report.rejected] == ["nameless.json#0"]
    assert json.loads(_record(db_path, "nameless.json#0")["raw_payload"]) == payload


def test_upsert_without_raw_payload_stays_compatible(db_path: Path) -> None:
    """Existing callers (and other steps' tests) pass no raw payload."""
    with store.connect(db_path) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
    record = _record(db_path, "s1")
    assert record["raw_payload"] is None
    assert record["source_type"] == "json_export"


def test_existing_database_gains_new_columns(tmp_path: Path) -> None:
    """A database created before raw payloads were captured keeps working."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE conversations (session_id TEXT PRIMARY KEY, source_file TEXT NOT NULL,"
        " status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', ingested_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO conversations VALUES ('old_1', 'old.json', 'accepted', '', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    store.init_db(path)
    store.init_db(path)  # and again: must be safe to repeat

    old = _record(path, "old_1")
    assert old["raw_payload"] is None
    assert old["source_type"] == "json_export"
    ingest(FakeApiSource([_api_response("new_1")]), db_path=path)
    assert _record(path, "new_1")["raw_payload"] is not None


# ---------------------------------------------------------------------------
# Current JSON ingestion is unchanged
# ---------------------------------------------------------------------------


def test_sample_transcripts_ingest_as_before(db_path: Path) -> None:
    report = ingest_directory(PROJECT_ROOT / "sample_data" / "transcripts", db_path=db_path)

    assert sorted(c.session_id for c in report.accepted) == ["conv_demo_001", "conv_demo_002"]
    assert report.turn_count == 8
    assert [r.session_id for r in report.rejected] == ["conv_demo_003"]
    assert "'agent_said' is empty" in report.rejected[0].reason
    assert report.unreadable_files == [] and report.failed == []


def test_json_source_yields_canonical_payload(source_dir: Path) -> None:
    payload = _conversation("c1")
    _write(source_dir, "c.json", [payload, _conversation("c2")])

    items = list(JsonTranscriptSource(source_dir).fetch_conversations())
    assert [i.position for i in items] == [0, 1]
    assert items[0].canonical_payload == payload
    assert items[0].raw_payload is items[0].canonical_payload
    assert (items[0].origin, items[0].source_type, items[0].error) == ("c.json", "json_export", None)


def test_json_source_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        JsonTranscriptSource(tmp_path / "nope")


# ---------------------------------------------------------------------------
# Reprocessing
# ---------------------------------------------------------------------------


def _ingest_one(source_dir: Path, db_path: Path, payload: dict):
    _write(source_dir, "session.json", payload)
    return ingest_directory(source_dir, db_path=db_path)


def test_same_valid_transcript_twice_is_idempotent(source_dir: Path, db_path: Path) -> None:
    payload = _conversation("s1", [_pair(0, "q0", "a0"), _pair(1, "q1", "a1")])
    _ingest_one(source_dir, db_path, payload)
    first = (_turns(db_path), json.loads(_record(db_path, "s1")["raw_payload"]))
    _ingest_one(source_dir, db_path, payload)

    assert (_turns(db_path), json.loads(_record(db_path, "s1")["raw_payload"])) == first
    assert _count(db_path, "conversations") == 1
    assert _count(db_path, "turns") == 2


def test_valid_to_changed_valid(source_dir: Path, db_path: Path) -> None:
    _ingest_one(source_dir, db_path, _conversation("s1", [_pair(0, "q0", "a0"), _pair(1)]))
    changed = _conversation("s1", [_pair(0, "q0", "new a0"), _pair(1, "new q1", "a1")])
    _ingest_one(source_dir, db_path, changed)

    assert _turns(db_path) == [("s1", 0, "q0", "new a0"), ("s1", 1, "new q1", "a1")]
    assert json.loads(_record(db_path, "s1")["raw_payload"]) == changed


def test_transcript_shrinks(source_dir: Path, db_path: Path) -> None:
    _ingest_one(source_dir, db_path, _conversation("s1", [_pair(0), _pair(1), _pair(2)]))
    _ingest_one(source_dir, db_path, _conversation("s1", [_pair(0), _pair(1)]))
    assert [t[1] for t in _turns(db_path, "s1")] == [0, 1]


def test_transcript_grows(source_dir: Path, db_path: Path) -> None:
    _ingest_one(source_dir, db_path, _conversation("s1", [_pair(0), _pair(1)]))
    _ingest_one(source_dir, db_path, _conversation("s1", [_pair(i) for i in range(4)]))
    assert [t[1] for t in _turns(db_path, "s1")] == [0, 1, 2, 3]


def test_valid_to_invalid_removes_turns(source_dir: Path, db_path: Path) -> None:
    _ingest_one(source_dir, db_path, _conversation("s1", [_pair(0), _pair(1), _pair(2)]))
    invalid = _conversation("s1", [_pair(0), _pair(1, said="   ")])
    report = _ingest_one(source_dir, db_path, invalid)

    assert [r.session_id for r in report.rejected] == ["s1"]
    assert _turns(db_path, "s1") == []
    record = _record(db_path, "s1")
    assert record["status"] == ConversationStatus.REJECTED.value
    assert "agent_said" in record["note"]
    assert json.loads(record["raw_payload"]) == invalid
    with store.connect(db_path) as conn:
        assert store.list_sessions(conn, ConversationStatus.ACCEPTED) == []


def test_invalid_to_valid(source_dir: Path, db_path: Path) -> None:
    _ingest_one(source_dir, db_path, _conversation("s1", [_pair(0), _pair(0)]))
    assert _record(db_path, "s1")["status"] == ConversationStatus.REJECTED.value

    valid = _conversation("s1", [_pair(0, "q0", "a0"), _pair(1, "q1", "a1")])
    _ingest_one(source_dir, db_path, valid)

    record = _record(db_path, "s1")
    assert (record["status"], record["note"]) == (ConversationStatus.ACCEPTED.value, "")
    assert json.loads(record["raw_payload"]) == valid
    assert _turns(db_path, "s1") == [("s1", 0, "q0", "a0"), ("s1", 1, "q1", "a1")]


def test_rejecting_one_session_leaves_others_alone(source_dir: Path, db_path: Path) -> None:
    _write(source_dir, "a.json", _conversation("keep"))
    _write(source_dir, "b.json", _conversation("s1"))
    ingest_directory(source_dir, db_path=db_path)
    _write(source_dir, "b.json", _conversation("s1", pairs=[]))
    ingest_directory(source_dir, db_path=db_path)

    assert [t[0] for t in _turns(db_path)] == ["keep", "keep"]


# ---------------------------------------------------------------------------
# Source abstraction
# ---------------------------------------------------------------------------


def test_base_source_must_be_implemented() -> None:
    with pytest.raises(NotImplementedError):
        TranscriptSource().fetch_conversations()


def test_fake_api_and_equivalent_json_export_produce_identical_turns(
    source_dir: Path, tmp_path: Path, db_path: Path
) -> None:
    response = _api_response("same_session")
    _write(source_dir, "same.json", FakeApiSource.to_canonical(response))
    ingest_directory(source_dir, db_path=db_path)

    api_db = tmp_path / "api.db"
    store.init_db(api_db)
    report = ingest(FakeApiSource([response]), db_path=api_db)

    assert report.turn_count == 2
    assert _turns(api_db) == _turns(db_path)
    assert _turns(api_db)[0] == ("same_session", 0, "What GPA do I need?", "You need a 2.0 GPA.")


def test_api_source_stores_provider_payload_not_canonical_copy(db_path: Path) -> None:
    response = _api_response()
    ingest(FakeApiSource([response]), db_path=db_path)
    assert json.loads(_record(db_path, "abc123")["raw_payload"]) == response


def test_api_source_uses_the_same_validation(db_path: Path) -> None:
    broken = _api_response("api_bad", exchanges=[("Hi", "   ")])
    report = ingest(FakeApiSource([broken]), db_path=db_path)

    assert [r.session_id for r in report.rejected] == ["api_bad"]
    assert report.rejected[0].reason == "api_bad[0]: 'agent_said' is empty"
    assert _turns(db_path) == []


# ---------------------------------------------------------------------------
# Failure isolation and transactions
# ---------------------------------------------------------------------------


def test_malformed_file_does_not_stop_later_valid_files(source_dir: Path, db_path: Path) -> None:
    _write(source_dir, "a_broken.json", "{not json")
    _write(source_dir, "b_broken.json", "[{")
    _write(source_dir, "c_good.json", _conversation("good_1"))
    report = ingest_directory(source_dir, db_path=db_path)

    assert [name for name, _ in report.unreadable_files] == ["a_broken.json", "b_broken.json"]
    assert [c.session_id for c in report.accepted] == ["good_1"]
    assert _count(db_path, "conversations") == 1  # unparseable input is not forced in


def test_failing_source_item_does_not_stop_later_conversations(db_path: Path) -> None:
    report = ingest(FakeApiSource(["<html>502</html>", _api_response("ok_1")]), db_path=db_path)
    assert report.unreadable_files == [("api#0", "undecodable response")]
    assert [c.session_id for c in report.accepted] == ["ok_1"]


def test_failure_midway_rolls_back_only_that_session(
    source_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(source_dir, "b_victim.json", _conversation("victim", [_pair(0, "q0", "a0"), _pair(1)]))
    ingest_directory(source_dir, db_path=db_path)
    before = (_record(db_path, "victim"), _turns(db_path, "victim"))

    real_replace_turns = store.replace_turns

    def replace_then_crash(conn, session_id, turns):
        written = real_replace_turns(conn, session_id, turns)
        if session_id == "victim":  # row updated and turns rewritten before the crash
            raise RuntimeError("simulated crash")
        return written

    monkeypatch.setattr(store, "replace_turns", replace_then_crash)
    _write(source_dir, "a_first.json", _conversation("first"))
    _write(source_dir, "b_victim.json", _conversation("victim", [_pair(0, "changed", "x")]))
    _write(source_dir, "c_last.json", _conversation("last"))
    report = ingest_directory(source_dir, db_path=db_path)

    assert report.failed == [("b_victim.json", "RuntimeError: simulated crash")]
    assert sorted(c.session_id for c in report.accepted) == ["first", "last"]
    # The failed session is exactly as it was; its neighbours were committed.
    assert (_record(db_path, "victim"), _turns(db_path, "victim")) == before
    assert {t[0] for t in _turns(db_path)} == {"first", "victim", "last"}

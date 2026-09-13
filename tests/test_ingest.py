"""Step 1 validation tests.

The point of these is not that valid input parses — it is that *invalid* input
is rejected with a specific reason and does not take the rest of the batch down
with it. A validator that silently accepts a transcript with a missing turn
produces verdicts that look fine and are quietly based on half a conversation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ava_eval.db import store
from ava_eval.models import ConversationStatus
from ava_eval.steps.s1_ingest import TranscriptFormatError, ingest_directory, parse_export_v1


def _pair(index: int, asked: str = "q", said: str = "a") -> dict:
    """Build one well-formed pair."""
    return {"pair_index": index, "caller_asked": asked, "agent_said": said}


def _conversation(session_id: str = "conv_1", pairs: list[dict] | None = None) -> dict:
    """Build a well-formed conversation payload."""
    return {
        "provider_session_id": session_id,
        "raw_pairs": pairs if pairs is not None else [_pair(0), _pair(1)],
    }


def test_parses_a_valid_conversation() -> None:
    conversation = parse_export_v1(_conversation(), "file.json")
    assert conversation.session_id == "conv_1"
    assert [t.pair_index for t in conversation.turns] == [0, 1]


def test_sorts_out_of_order_pairs() -> None:
    """Order is restored rather than rejected, as long as the set is complete."""
    payload = _conversation(pairs=[_pair(1), _pair(0)])
    conversation = parse_export_v1(payload, "file.json")
    assert [t.pair_index for t in conversation.turns] == [0, 1]


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"raw_pairs": [_pair(0)]}, "provider_session_id"),
        ({"provider_session_id": "  ", "raw_pairs": [_pair(0)]}, "empty"),
        ({"provider_session_id": "c"}, "raw_pairs"),
        ({"provider_session_id": "c", "raw_pairs": []}, "empty"),
        ({"provider_session_id": "c", "raw_pairs": {}}, "must be a list"),
    ],
)
def test_rejects_malformed_conversations(payload: dict, fragment: str) -> None:
    with pytest.raises(TranscriptFormatError, match=fragment):
        parse_export_v1(payload, "file.json")


def test_rejects_empty_agent_said() -> None:
    payload = _conversation(pairs=[_pair(0), _pair(1, said="   ")])
    with pytest.raises(TranscriptFormatError, match="agent_said"):
        parse_export_v1(payload, "file.json")


def test_rejects_empty_caller_asked() -> None:
    payload = _conversation(pairs=[_pair(0, asked="")])
    with pytest.raises(TranscriptFormatError, match="caller_asked"):
        parse_export_v1(payload, "file.json")


def test_rejects_duplicate_pair_index() -> None:
    payload = _conversation(pairs=[_pair(0), _pair(0)])
    with pytest.raises(TranscriptFormatError, match="duplicate"):
        parse_export_v1(payload, "file.json")


def test_rejects_gapped_pair_index() -> None:
    """A gap means a turn is missing, which silently corrupts step 4's scoping."""
    payload = _conversation(pairs=[_pair(0), _pair(2)])
    with pytest.raises(TranscriptFormatError, match="sequential"):
        parse_export_v1(payload, "file.json")


def test_rejects_non_integer_pair_index() -> None:
    payload = _conversation(pairs=[{"pair_index": "0", "caller_asked": "q", "agent_said": "a"}])
    with pytest.raises(TranscriptFormatError, match="integer"):
        parse_export_v1(payload, "file.json")


def test_rejects_boolean_pair_index() -> None:
    """bool is a subclass of int in Python, so this needs an explicit guard."""
    payload = _conversation(pairs=[{"pair_index": True, "caller_asked": "q", "agent_said": "a"}])
    with pytest.raises(TranscriptFormatError, match="integer"):
        parse_export_v1(payload, "file.json")


def test_bad_conversation_does_not_stop_the_batch(tmp_path: Path, db_url: str) -> None:
    """The whole reason rejections are recorded instead of raised."""
    source = tmp_path / "transcripts"
    source.mkdir()
    (source / "good.json").write_text(json.dumps(_conversation("good_1")), encoding="utf-8")
    (source / "bad.json").write_text(
        json.dumps(_conversation("bad_1", pairs=[_pair(0), _pair(5)])), encoding="utf-8"
    )
    (source / "broken.json").write_text("{not json", encoding="utf-8")

    report = ingest_directory(source, database_url=db_url)

    assert [c.session_id for c in report.accepted] == ["good_1"]
    assert [r.session_id for r in report.rejected] == ["bad_1"]
    assert [name for name, _ in report.unreadable_files] == ["broken.json"]

    # The rejected conversation is still recorded, so turn counts stay honest.
    with store.connect(db_url) as conn:
        assert store.list_sessions(conn, ConversationStatus.ACCEPTED) == ["good_1"]
        assert store.list_sessions(conn, ConversationStatus.REJECTED) == ["bad_1"]


def test_accepts_a_json_array_of_conversations(tmp_path: Path, db_url: str) -> None:
    source = tmp_path / "transcripts"
    source.mkdir()
    payload = [_conversation("a_1"), _conversation("a_2")]
    (source / "batch.json").write_text(json.dumps(payload), encoding="utf-8")

    report = ingest_directory(source, database_url=db_url)
    assert sorted(c.session_id for c in report.accepted) == ["a_1", "a_2"]


def test_reingesting_replaces_turns(tmp_path: Path, db_url: str) -> None:
    """Re-running step 1 must not accumulate duplicate turns."""
    source = tmp_path / "transcripts"
    source.mkdir()
    (source / "c.json").write_text(json.dumps(_conversation("c_1")), encoding="utf-8")

    ingest_directory(source, database_url=db_url)
    ingest_directory(source, database_url=db_url)

    with store.connect(db_url) as conn:
        assert len(store.get_turns(conn, "c_1")) == 2

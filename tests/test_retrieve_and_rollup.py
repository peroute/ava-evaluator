"""Retrieval ordering and the severity rollup.

Both are places where a subtle bug produces plausible-looking output. Retrieval
that returns the right chunks in the wrong order still "works"; a rollup that
picks the wrong verdict per turn still produces a report. So these tests use
hand-written vectors and hand-written verdicts where the correct answer is known
in advance.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ava_eval.db import store
from ava_eval.models import (
    Claim,
    ClaimVerdict,
    ConversationStatus,
    KbChunk,
    Turn,
    Verdict,
    VerdictTag,
)
from ava_eval.steps.s5_retrieve import KbIndex, load_index

# --- retrieval ------------------------------------------------------------


def _index(vectors: list[list[float]]) -> KbIndex:
    """Build an in-memory index from unit-ish vectors."""
    matrix = np.asarray(vectors, dtype=np.float32)
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    meta = [
        {
            "chunk_id": f"c{i}",
            "text": f"chunk {i}",
            "section": "S",
            "source_doc": "d.docx",
        }
        for i in range(len(vectors))
    ]
    return KbIndex(meta=meta, matrix=matrix)


def test_returns_the_nearest_chunk_first() -> None:
    index = _index([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]])
    results = index.search([1.0, 0.0], k=3)
    assert [r.chunk_id for r in results] == ["c0", "c1", "c2"]
    assert [r.rank for r in results] == [0, 1, 2]


def test_scores_descend() -> None:
    index = _index([[1.0, 0.0], [0.7, 0.7], [0.0, 1.0]])
    scores = [r.score for r in index.search([1.0, 0.0], k=3)]
    assert scores == sorted(scores, reverse=True)


def test_k_larger_than_the_index_is_safe() -> None:
    index = _index([[1.0, 0.0], [0.0, 1.0]])
    assert len(index.search([1.0, 0.0], k=99)) == 2


def test_k_smaller_than_the_index_truncates_correctly() -> None:
    """argpartition finds the top k without sorting; this checks it kept the right ones."""
    index = _index([[1.0, 0.0], [0.9, 0.2], [0.1, 1.0], [0.0, 1.0]])
    assert [r.chunk_id for r in index.search([1.0, 0.0], k=2)] == ["c0", "c1"]


def test_empty_index_returns_nothing() -> None:
    assert KbIndex(meta=[], matrix=np.zeros((0, 0), dtype=np.float32)).search([1.0], k=3) == []


def test_query_is_normalised_before_comparison() -> None:
    """An unnormalised query must not change the ranking, only the raw scale."""
    index = _index([[1.0, 0.0], [0.0, 1.0]])
    small = index.search([0.001, 0.0], k=2)
    large = index.search([500.0, 0.0], k=2)
    assert [r.chunk_id for r in small] == [r.chunk_id for r in large]
    assert small[0].score == pytest.approx(large[0].score, abs=1e-5)


def test_embeddings_survive_a_round_trip_through_the_database(db_path: Path) -> None:
    """Storing float32 bytes must not quietly change the vectors."""
    with store.connect(db_path) as conn:
        store.upsert_document(conn, "d", "d.docx", "d.docx", "hash", "2026-01-01")
        chunks = [
            KbChunk(chunk_id="c0", doc_id="d", section="S", ordinal=0, text="alpha"),
            KbChunk(chunk_id="c1", doc_id="d", section="S", ordinal=1, text="beta"),
        ]
        store.insert_chunks(conn, chunks, [[3.0, 0.0], [0.0, 4.0]], "fake-model")

    with store.connect(db_path) as conn:
        index = load_index(conn)

    assert index.size == 2
    # Stored normalised, so both vectors are unit length regardless of input scale.
    assert np.allclose(np.linalg.norm(index.matrix, axis=1), 1.0)
    assert index.search([1.0, 0.0], k=1)[0].chunk_id == "c0"


# --- severity rollup ------------------------------------------------------


def _seed_turn(conn, session: str, pair: int, verdicts: list[tuple[str, Verdict]]) -> None:
    """Create a turn with the given claim verdicts attached."""
    store.replace_turns(
        conn, session, [Turn(session_id=session, pair_index=pair, caller_asked="q", agent_said="a")]
    )
    claims = [
        Claim(claim_id=cid, session_id=session, pair_index=pair, subject_tag="S", text="t")
        for cid, _ in verdicts
    ]
    store.replace_claims_for_session(conn, session, claims)
    for claim_id, verdict in verdicts:
        store.save_verdict(conn, ClaimVerdict(claim_id=claim_id, verdict=verdict))


@pytest.mark.parametrize(
    "verdicts, expected",
    [
        ([Verdict.SUPPORTED, Verdict.SUPPORTED], "SUPPORTED"),
        ([Verdict.SUPPORTED, Verdict.NOT_FOUND], "NOT_FOUND"),
        ([Verdict.SUPPORTED, Verdict.CONTRADICTED], "CONTRADICTED"),
        ([Verdict.NOT_FOUND, Verdict.CONTRADICTED], "CONTRADICTED"),
        ([Verdict.CONTRADICTED], "CONTRADICTED"),
    ],
)
def test_turn_verdict_is_the_worst_of_its_claims(
    db_path: Path, verdicts: list[Verdict], expected: str
) -> None:
    """CONTRADICTED > NOT_FOUND > SUPPORTED, regardless of claim order."""
    with store.connect(db_path) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        _seed_turn(conn, "s1", 0, [(f"c{i}", v) for i, v in enumerate(verdicts)])

    with store.connect(db_path) as conn:
        rows = store.turn_results(conn, "s1")

    assert rows[0]["turn_verdict"] == expected
    assert rows[0]["claim_count"] == len(verdicts)


def test_turn_with_no_claims_is_labelled_not_dropped(db_path: Path) -> None:
    """Turn counts in reports have to stay honest, so skipped turns still appear."""
    with store.connect(db_path) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        store.replace_turns(
            conn, "s1", [Turn(session_id="s1", pair_index=0, caller_asked="hi", agent_said="hello")]
        )

    with store.connect(db_path) as conn:
        rows = store.turn_results(conn, "s1")

    assert len(rows) == 1
    assert rows[0]["turn_verdict"] == "UNCLASSIFIED"


def test_conversation_summary_counts_each_severity_separately(db_path: Path) -> None:
    """CONTRADICTED and NOT_FOUND must never be merged into one 'flagged' count."""
    with store.connect(db_path) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        store.replace_turns(
            conn,
            "s1",
            [
                Turn(session_id="s1", pair_index=i, caller_asked="q", agent_said="a")
                for i in range(3)
            ],
        )
        claims = [
            Claim(claim_id=f"c{i}", session_id="s1", pair_index=i, subject_tag="S", text="t")
            for i in range(3)
        ]
        store.replace_claims_for_session(conn, "s1", claims)
        for i, verdict in enumerate([Verdict.CONTRADICTED, Verdict.NOT_FOUND, Verdict.SUPPORTED]):
            store.save_verdict(conn, ClaimVerdict(claim_id=f"c{i}", verdict=verdict))

    with store.connect(db_path) as conn:
        summary = store.conversation_summary(conn, "s1")[0]

    assert summary["contradicted_turns"] == 1
    assert summary["not_found_turns"] == 1
    assert summary["supported_turns"] == 1


def test_rejudging_a_claim_updates_the_rollup(db_path: Path) -> None:
    """The reason turn level is a view: it cannot go stale relative to claims."""
    with store.connect(db_path) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        _seed_turn(conn, "s1", 0, [("c0", Verdict.SUPPORTED)])
        assert store.turn_results(conn, "s1")[0]["turn_verdict"] == "SUPPORTED"

        store.save_verdict(
            conn, ClaimVerdict(claim_id="c0", verdict=Verdict.CONTRADICTED, attempts=1)
        )
        assert store.turn_results(conn, "s1")[0]["turn_verdict"] == "CONTRADICTED"


def test_replacing_claims_cascades_to_verdicts(db_path: Path) -> None:
    """A reworded claim must not keep the old claim's verdict attached."""
    with store.connect(db_path) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        _seed_turn(conn, "s1", 0, [("c0", Verdict.CONTRADICTED)])

        store.replace_claims_for_session(conn, "s1", [])
        assert conn.execute("SELECT COUNT(*) n FROM verdicts").fetchone()["n"] == 0


def test_verdict_tags_are_counted(db_path: Path) -> None:
    with store.connect(db_path) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        _seed_turn(conn, "s1", 0, [("c0", Verdict.NOT_FOUND)])
        store.save_verdict(
            conn,
            ClaimVerdict(
                claim_id="c0", verdict=Verdict.NOT_FOUND, tag=VerdictTag.KB_GAP, attempts=2
            ),
        )
        stats = store.verdict_stats(conn)

    assert stats["by_tag"] == {"kb_gap": 1}

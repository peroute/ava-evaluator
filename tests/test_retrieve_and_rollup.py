"""Retrieval ordering and the severity rollup.

Both are places where a subtle bug produces plausible-looking output. Retrieval
that returns the right chunks in the wrong order still "works"; a rollup that
picks the wrong verdict per turn still produces a report. So these tests use
hand-written vectors and hand-written verdicts where the correct answer is known
in advance.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from ava_eval.db import store
from ava_eval.models import (
    Claim,
    ClaimVerdict,
    ConversationStatus,
    KbChunk,
    RetrievedChunk,
    Turn,
    Verdict,
    VerdictTag,
)

# --- retrieval ------------------------------------------------------------


def _seed_chunks(conn: store.Connection, vectors: list[list[float]]) -> None:
    """Index one document whose chunks c0, c1, ... carry the given vectors."""
    store.upsert_document(conn, "d", "d.docx", "d.docx", "hash", date(2026, 1, 1))
    chunks = [
        KbChunk(chunk_id=f"c{i}", doc_id="d", section="S", ordinal=i, text=f"chunk {i}")
        for i in range(len(vectors))
    ]
    store.insert_chunks(conn, chunks, vectors, "fake-model")


def _search(
    db_url: str, vectors: list[list[float]], query: list[float], k: int
) -> list[RetrievedChunk]:
    """Seed ``vectors`` (if any) and run one search."""
    with store.connect(db_url) as conn:
        if vectors:
            _seed_chunks(conn, vectors)
        return store.search_chunks(conn, query, k)


def test_returns_the_nearest_chunk_first(db_url: str) -> None:
    results = _search(db_url, [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], [1.0, 0.0], k=3)
    assert [r.chunk_id for r in results] == ["c0", "c1", "c2"]
    assert [r.rank for r in results] == [0, 1, 2]


def test_scores_descend(db_url: str) -> None:
    results = _search(db_url, [[1.0, 0.0], [0.7, 0.7], [0.0, 1.0]], [1.0, 0.0], k=3)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_k_larger_than_the_index_is_safe(db_url: str) -> None:
    assert len(_search(db_url, [[1.0, 0.0], [0.0, 1.0]], [1.0, 0.0], k=99)) == 2


def test_k_smaller_than_the_index_truncates_correctly(db_url: str) -> None:
    """LIMIT must keep the best k, not an arbitrary k."""
    vectors = [[1.0, 0.0], [0.9, 0.2], [0.1, 1.0], [0.0, 1.0]]
    assert [r.chunk_id for r in _search(db_url, vectors, [1.0, 0.0], k=2)] == ["c0", "c1"]


def test_empty_index_returns_nothing(db_url: str) -> None:
    assert _search(db_url, [], [1.0], k=3) == []


def test_query_is_normalised_before_comparison(db_url: str) -> None:
    """An unnormalised query must not change the ranking, only the raw scale."""
    vectors = [[1.0, 0.0], [0.0, 1.0]]
    small = _search(db_url, vectors, [0.001, 0.0], k=2)
    with store.connect(db_url) as conn:
        large = store.search_chunks(conn, [500.0, 0.0], k=2)
    assert [r.chunk_id for r in small] == [r.chunk_id for r in large]
    assert small[0].score == pytest.approx(large[0].score, abs=1e-5)


def test_search_is_exact_brute_force(db_url: str) -> None:
    """pgvector must return exactly what a full numpy comparison would.

    This is the property the whole design relies on: with no ANN index, search
    is exhaustive, so a relevant chunk can never be skipped. Random vectors make
    near-ties realistic rather than hand-picked.
    """
    rng = np.random.default_rng(7)
    vectors = rng.normal(size=(60, 8)).astype(np.float32)
    query = rng.normal(size=8).astype(np.float32)

    results = _search(db_url, vectors.tolist(), query.tolist(), k=10)

    unit = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    scores = unit @ (query / np.linalg.norm(query))
    order = np.argsort(-scores)[:10]
    assert [r.chunk_id for r in results] == [f"c{i}" for i in order]
    assert [r.score for r in results] == pytest.approx(scores[order].tolist(), abs=1e-5)


def test_embeddings_are_stored_normalised(db_url: str) -> None:
    """Search relies on unit vectors, whatever scale the embedder returned."""
    with store.connect(db_url) as conn:
        _seed_chunks(conn, [[3.0, 0.0], [0.0, 4.0]])
        stored = conn.execute("SELECT embedding FROM kb_chunks ORDER BY chunk_id").fetchall()
        top = store.search_chunks(conn, [1.0, 0.0], k=1)

    assert len(stored) == 2
    # pgvector reads the column back as its own Vector type, not a numpy array.
    assert all(np.isclose(np.linalg.norm(row["embedding"].to_numpy()), 1.0) for row in stored)
    assert top[0].chunk_id == "c0"


# --- severity rollup ------------------------------------------------------


def _seed_turn(
    conn: store.Connection, session: str, pair: int, verdicts: list[tuple[str, Verdict]]
) -> None:
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
    db_url: str, verdicts: list[Verdict], expected: str
) -> None:
    """CONTRADICTED > NOT_FOUND > SUPPORTED, regardless of claim order."""
    with store.connect(db_url) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        _seed_turn(conn, "s1", 0, [(f"c{i}", v) for i, v in enumerate(verdicts)])

    with store.connect(db_url) as conn:
        rows = store.turn_results(conn, "s1")

    assert rows[0]["turn_verdict"] == expected
    assert rows[0]["claim_count"] == len(verdicts)


def test_turn_with_no_claims_is_labelled_not_dropped(db_url: str) -> None:
    """Turn counts in reports have to stay honest, so skipped turns still appear."""
    with store.connect(db_url) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        store.replace_turns(
            conn, "s1", [Turn(session_id="s1", pair_index=0, caller_asked="hi", agent_said="hello")]
        )

    with store.connect(db_url) as conn:
        rows = store.turn_results(conn, "s1")

    assert len(rows) == 1
    assert rows[0]["turn_verdict"] == "UNCLASSIFIED"
    assert rows[0]["contains_claims"] is False


def test_conversation_summary_counts_each_severity_separately(db_url: str) -> None:
    """CONTRADICTED and NOT_FOUND must never be merged into one 'flagged' count."""
    with store.connect(db_url) as conn:
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

    with store.connect(db_url) as conn:
        summary = store.conversation_summary(conn, "s1")[0]

    assert summary["contradicted_turns"] == 1
    assert summary["not_found_turns"] == 1
    assert summary["supported_turns"] == 1


def test_rejudging_a_claim_updates_the_rollup(db_url: str) -> None:
    """The reason turn level is a view: it cannot go stale relative to claims."""
    with store.connect(db_url) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        _seed_turn(conn, "s1", 0, [("c0", Verdict.SUPPORTED)])
        assert store.turn_results(conn, "s1")[0]["turn_verdict"] == "SUPPORTED"

        store.save_verdict(
            conn, ClaimVerdict(claim_id="c0", verdict=Verdict.CONTRADICTED, attempts=1)
        )
        assert store.turn_results(conn, "s1")[0]["turn_verdict"] == "CONTRADICTED"


def test_replacing_claims_cascades_to_verdicts(db_url: str) -> None:
    """A reworded claim must not keep the old claim's verdict attached."""
    with store.connect(db_url) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        _seed_turn(conn, "s1", 0, [("c0", Verdict.CONTRADICTED)])

        store.replace_claims_for_session(conn, "s1", [])
        assert conn.execute("SELECT COUNT(*) AS n FROM verdicts").fetchone()["n"] == 0


def test_verdict_tags_are_counted(db_url: str) -> None:
    with store.connect(db_url) as conn:
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


def test_database_rejects_an_unknown_verdict(db_url: str) -> None:
    """The CHECK constraints are the last line of defence against a typo'd verdict."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with store.connect(db_url) as conn:
            store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
            _seed_turn(conn, "s1", 0, [("c0", Verdict.SUPPORTED)])
            conn.execute("UPDATE verdicts SET verdict = 'MAYBE' WHERE claim_id = 'c0'")

"""Chunk kinds, KB replacement, and the stale-evidence warning.

Chunk ids are positional (``t11.r02``), so after TTU edits the document the
same id can name a different passage. The agreed trade-off is to warn rather
than delete: every verdict records the KB version it was judged against, and
``v_claim_results`` flags the ones judged against an older version. These tests
pin that flag, because a warning that never fires looks exactly like one that
has nothing to warn about.
"""

from __future__ import annotations

from datetime import date

import pytest

from ava_eval.db import store
from ava_eval.db.migrate import downgrade_to, upgrade_to_head
from ava_eval.models import (
    Claim,
    ClaimVerdict,
    ChunkKind,
    ConversationStatus,
    JudgeResponse,
    KbChunk,
    Turn,
    Verdict,
)
from ava_eval.steps import s6_judge


def _seed(conn: store.Connection, kb_hash: str = "h1") -> Claim:
    """One conversation with one claim, and a one-chunk KB indexed at ``kb_hash``."""
    store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
    store.replace_turns(
        conn, "s1", [Turn(session_id="s1", pair_index=0, caller_asked="q", agent_said="a")]
    )
    claim = Claim(claim_id="c0", session_id="s1", pair_index=0, subject_tag="S", text="t")
    store.replace_claims_for_session(conn, "s1", [claim])
    store.upsert_document(conn, "online", "online.docx", "online.docx", kb_hash, date(2026, 1, 1))
    store.insert_chunks(
        conn,
        [
            KbChunk(
                chunk_id="t01.q01",
                doc_id="online",
                section="FAQ > Can I walk the stage?",
                ordinal=0,
                text="FAQ\n\nCan I walk the stage?\nYes.",
                kind=ChunkKind.QA,
            )
        ],
        [[1.0, 0.0]],
        "fake",
    )
    return claim


def test_chunk_kind_is_stored(db_url: str) -> None:
    with store.connect(db_url) as conn:
        _seed(conn)
        row = conn.execute("SELECT kind FROM kb_chunks WHERE chunk_id = 't01.q01'").fetchone()
    assert row["kind"] == "qa"


def test_database_rejects_an_unknown_chunk_kind(db_url: str) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with store.connect(db_url) as conn:
            _seed(conn)
            conn.execute("UPDATE kb_chunks SET kind = 'blob'")


def test_clear_kb_removes_every_document_and_chunk(db_url: str) -> None:
    with store.connect(db_url) as conn:
        _seed(conn)
        store.clear_kb(conn)
        assert store.count_chunks(conn) == 0
        assert store.current_kb_hash(conn) is None


def test_judged_verdicts_record_the_kb_version(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        s6_judge,
        "judge_claim",
        lambda claim, chunks: JudgeResponse(
            verdict=Verdict.SUPPORTED, rationale="scripted", evidence_chunk_id="t01.q01"
        ),
    )
    with store.connect(db_url) as conn:
        claim = _seed(conn, kb_hash="h1")
        s6_judge.judge_claims(conn, [claim], {"c0": store.search_chunks(conn, [1.0, 0.0], 1)})
        row = conn.execute("SELECT kb_content_hash FROM verdicts WHERE claim_id = 'c0'").fetchone()
    assert row["kb_content_hash"] == "h1"


def test_view_flags_a_verdict_once_the_kb_changes(db_url: str) -> None:
    with store.connect(db_url) as conn:
        _seed(conn, kb_hash="h1")
        store.save_verdict(
            conn,
            ClaimVerdict(
                claim_id="c0",
                verdict=Verdict.SUPPORTED,
                evidence_chunk_id="t01.q01",
                kb_content_hash="h1",
            ),
        )
        before = store.get_claim_result(conn, "c0")
        store.upsert_document(conn, "online", "online.docx", "online.docx", "h2", date(2026, 2, 1))
        after = store.get_claim_result(conn, "c0")

    assert before["kb_changed_since_judged"] is False
    assert before["evidence_kind"] == "qa"
    assert after["kb_changed_since_judged"] is True


def test_a_claim_with_no_verdict_is_not_flagged(db_url: str) -> None:
    with store.connect(db_url) as conn:
        _seed(conn)
        assert store.get_claim_result(conn, "c0")["kb_changed_since_judged"] is None


def test_report_prints_the_warning_for_stale_verdicts(db_url: str) -> None:
    from ava_eval.steps.s7_aggregate import conversation_report, format_report

    with store.connect(db_url) as conn:
        _seed(conn, kb_hash="h1")
        store.save_verdict(
            conn,
            ClaimVerdict(
                claim_id="c0",
                verdict=Verdict.CONTRADICTED,
                evidence_chunk_id="t01.q01",
                kb_content_hash="old",
            ),
        )
        text = format_report(conversation_report(conn, "s1"))

    assert "evidence: FAQ > Can I walk the stage? (t01.q01)" in text
    assert "[!] the knowledge base changed since this verdict" in text


def test_migration_0003_downgrades_and_upgrades_cleanly(db_url: str) -> None:
    downgrade_to("0002", db_url)
    try:
        with store.connect(db_url) as conn:
            columns = {
                row["column_name"]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name IN ('kb_chunks', 'verdicts')"
                ).fetchall()
            }
        assert "kind" not in columns
        assert "kb_content_hash" not in columns
    finally:
        upgrade_to_head(db_url)

    with store.connect(db_url) as conn:
        _seed(conn)
        assert store.get_claim_result(conn, "c0")["evidence_kind"] is None

"""The step 6 retry policy, and the prompt loader.

The retry rule is the single most consequential piece of logic in the pipeline,
because it decides who gets told to fix something: ``kb_gap`` sends work to
TTU's content team, ``retrieval_miss`` sends it to whoever tunes this evaluator,
and a CONTRADICTED that got retried away sends it to nobody at all.

These tests drive the policy with a scripted judge so each branch is forced
rather than hoped for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ava_eval import config
from ava_eval.db import store
from ava_eval.llm import load_prompt
from ava_eval.models import (
    Claim,
    ConversationStatus,
    JudgeResponse,
    KbChunk,
    Turn,
    Verdict,
    VerdictTag,
)
from ava_eval.steps import s6_judge
from ava_eval.steps.s6_judge import judge_claims, render_excerpts
from ava_eval.steps.s5_retrieve import load_index


@pytest.fixture
def seeded(db_path: Path) -> Path:
    """A database with one conversation, one claim, and a small KB."""
    with store.connect(db_path) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        store.replace_turns(
            conn, "s1", [Turn(session_id="s1", pair_index=0, caller_asked="q", agent_said="a")]
        )
        store.replace_claims_for_session(
            conn,
            "s1",
            [
                Claim(
                    claim_id="c0",
                    session_id="s1",
                    pair_index=0,
                    subject_tag="S",
                    text="the minimum gpa is 2.0",
                )
            ],
        )
        store.upsert_document(conn, "d", "d.docx", "d.docx", "h", "2026-01-01")
        chunks = [
            KbChunk(chunk_id=f"k{i}", doc_id="d", section="S", ordinal=i, text=f"policy text {i}")
            for i in range(8)
        ]
        # Same dimensionality the (fake) embedder produces, because the retry
        # path re-embeds the claim for real and then compares against these.
        dim = config.settings.embed_dim
        vectors = [[1.0 if j == i else 0.1 for j in range(dim)] for i in range(8)]
        store.insert_chunks(conn, chunks, vectors, "fake")
    return db_path


def _query() -> list[float]:
    """A query vector matching the configured embedding dimension."""
    return [1.0] * config.settings.embed_dim


def _claim() -> Claim:
    return Claim(
        claim_id="c0", session_id="s1", pair_index=0, subject_tag="S", text="the minimum gpa is 2.0"
    )


def _script(monkeypatch: pytest.MonkeyPatch, verdicts: list[Verdict]) -> list[int]:
    """Replace the judge with one that returns ``verdicts`` in order."""
    calls: list[int] = []

    def fake_judge(claim, chunks):
        index = len(calls)
        calls.append(len(chunks))
        return JudgeResponse(
            verdict=verdicts[min(index, len(verdicts) - 1)],
            rationale="scripted",
            evidence_chunk_id="k0",
        )

    monkeypatch.setattr(s6_judge, "judge_claim", fake_judge)
    return calls


def test_contradicted_is_never_retried(seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying a contradiction would let the judge shop for a friendlier chunk."""
    calls = _script(monkeypatch, [Verdict.CONTRADICTED])
    claim = _claim()

    with store.connect(seeded) as conn:
        index = load_index(conn)
        verdicts = judge_claims(conn, [claim], {"c0": index.search(_query(), 5)}, index=index)

    assert len(calls) == 1, "the judge was called a second time for a CONTRADICTED claim"
    assert verdicts[0].verdict == Verdict.CONTRADICTED
    assert verdicts[0].tag is None
    assert verdicts[0].attempts == 1


def test_supported_is_never_retried(seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _script(monkeypatch, [Verdict.SUPPORTED])
    with store.connect(seeded) as conn:
        index = load_index(conn)
        verdicts = judge_claims(conn, [_claim()], {"c0": index.search(_query(), 5)}, index=index)

    assert len(calls) == 1
    assert verdicts[0].attempts == 1


def test_not_found_retries_and_tags_kb_gap_when_still_not_found(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wider search found more, judged it, still nothing: the KB genuinely lacks it."""
    calls = _script(monkeypatch, [Verdict.NOT_FOUND, Verdict.NOT_FOUND])
    with store.connect(seeded) as conn:
        index = load_index(conn)
        verdicts = judge_claims(conn, [_claim()], {"c0": index.search(_query(), 3)}, index=index)

    assert len(calls) == 2, "the NOT_FOUND claim was not retried"
    assert calls[1] > calls[0], "the retry did not widen the search"
    assert verdicts[0].verdict == Verdict.NOT_FOUND
    assert verdicts[0].tag == VerdictTag.KB_GAP
    assert verdicts[0].attempts == 2


def test_not_found_that_the_retry_rescues_is_tagged_retrieval_miss(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The content was there all along; our first search missed it."""
    _script(monkeypatch, [Verdict.NOT_FOUND, Verdict.SUPPORTED])
    with store.connect(seeded) as conn:
        index = load_index(conn)
        verdicts = judge_claims(conn, [_claim()], {"c0": index.search(_query(), 3)}, index=index)

    assert verdicts[0].verdict == Verdict.SUPPORTED
    assert verdicts[0].tag == VerdictTag.RETRIEVAL_MISS
    assert verdicts[0].attempts == 2


def test_a_contradiction_found_on_retry_still_counts_as_a_retrieval_miss(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tag describes our retrieval, the verdict describes Ava. Both are kept."""
    _script(monkeypatch, [Verdict.NOT_FOUND, Verdict.CONTRADICTED])
    with store.connect(seeded) as conn:
        index = load_index(conn)
        verdicts = judge_claims(conn, [_claim()], {"c0": index.search(_query(), 3)}, index=index)

    assert verdicts[0].verdict == Verdict.CONTRADICTED
    assert verdicts[0].tag == VerdictTag.RETRIEVAL_MISS


def test_retry_is_recorded_as_attempt_two(seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The retrieval history has to be auditable per attempt."""
    _script(monkeypatch, [Verdict.NOT_FOUND, Verdict.NOT_FOUND])
    with store.connect(seeded) as conn:
        index = load_index(conn)
        judge_claims(conn, [_claim()], {"c0": index.search(_query(), 3)}, index=index)

    with store.connect(seeded) as conn:
        assert store.get_retrievals(conn, "c0", attempt=2)


def test_no_claims_is_not_an_error(seeded: Path) -> None:
    with store.connect(seeded) as conn:
        assert judge_claims(conn, [], {}) == []


# --- rendering and prompts ------------------------------------------------


def test_excerpts_carry_chunk_ids(seeded: Path) -> None:
    """Without the id, a flag is an assertion with no way to check it."""
    with store.connect(seeded) as conn:
        chunks = load_index(conn).search(_query(), 2)
    rendered = render_excerpts(chunks)
    assert all(c.chunk_id in rendered for c in chunks)


def test_dimension_mismatch_explains_itself(seeded: Path) -> None:
    """Swapping embedding model or dimension without re-indexing must say so.

    The raw numpy failure here is a matmul shape error naming neither the cause
    nor the fix, and this is a mistake that happens the first time anyone moves
    from the fake embedder to a real one.
    """
    with store.connect(seeded) as conn:
        index = load_index(conn)
    with pytest.raises(RuntimeError, match="index-kb --force"):
        index.search([1.0, 1.0], 2)


def test_excerpts_handle_the_empty_case() -> None:
    assert "No excerpts" in render_excerpts([])


def test_prompt_loader_fills_placeholders() -> None:
    text = load_prompt("classify_turn", caller_asked="Q?", agent_said="A.")
    assert "Q?" in text and "A." in text
    assert "{{" not in text


def test_prompt_loader_rejects_an_unknown_field() -> None:
    with pytest.raises(KeyError, match="placeholder"):
        load_prompt("classify_turn", caller_asked="q", agent_said="a", nonsense="x")


def test_prompt_loader_rejects_unfilled_placeholders() -> None:
    """A prompt shipped with a literal {{TRANSCRIPT}} still gets an answer -- a useless one."""
    with pytest.raises(KeyError, match="unfilled"):
        load_prompt("classify_turn", caller_asked="q")


def test_prompt_loader_reports_a_missing_template() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("does_not_exist")


def test_every_prompt_template_is_loadable() -> None:
    """Guards against a template being renamed without its caller being updated."""
    assert load_prompt("classify_turn", caller_asked="q", agent_said="a")
    assert load_prompt("decompose_claims", session_id="s", transcript="t")
    assert load_prompt("judge_claim", subject_tag="s", claim_text="c", excerpts="e")

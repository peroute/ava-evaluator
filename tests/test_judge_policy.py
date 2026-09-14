"""The step 6 retry policy, and the prompt loader.

The retry rule is the single most consequential piece of logic in the pipeline,
because it decides who gets told to fix something: ``kb_gap`` sends work to
TTU's content team, ``retrieval_miss`` sends it to whoever tunes this evaluator,
and a CONTRADICTED that got retried away sends it to nobody at all.

These tests drive the policy with a scripted judge so each branch is forced
rather than hoped for.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from ava_eval import config
from ava_eval.db import store
from ava_eval.llm import load_prompt
from ava_eval.llm.client import LlmError
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


@pytest.fixture
def seeded(db_url: str) -> str:
    """A database with one conversation, one claim, and a small KB."""
    with store.connect(db_url) as conn:
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
        store.upsert_document(conn, "d", "d.docx", "d.docx", "h", date(2026, 1, 1))
        chunks = [
            KbChunk(chunk_id=f"k{i}", doc_id="d", section="S", ordinal=i, text=f"policy text {i}")
            for i in range(8)
        ]
        # Same dimensionality the (fake) embedder produces, because the retry
        # path re-embeds the claim for real and then compares against these.
        dim = config.settings.embed_dim
        vectors = [[1.0 if j == i else 0.1 for j in range(dim)] for i in range(8)]
        store.insert_chunks(conn, chunks, vectors, "fake")
    return db_url


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


def test_contradicted_is_never_retried(seeded: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying a contradiction would let the judge shop for a friendlier chunk."""
    calls = _script(monkeypatch, [Verdict.CONTRADICTED])
    claim = _claim()

    with store.connect(seeded) as conn:
        verdicts = judge_claims(
            conn, [claim], {"c0": store.search_chunks(conn, _query(), 5)}
        ).verdicts

    assert len(calls) == 1, "the judge was called a second time for a CONTRADICTED claim"
    assert verdicts[0].verdict == Verdict.CONTRADICTED
    assert verdicts[0].tag is None
    assert verdicts[0].attempts == 1


def test_supported_is_never_retried(seeded: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _script(monkeypatch, [Verdict.SUPPORTED])
    with store.connect(seeded) as conn:
        verdicts = judge_claims(
            conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 5)}
        ).verdicts

    assert len(calls) == 1
    assert verdicts[0].attempts == 1


def test_not_found_retries_and_tags_kb_gap_when_still_not_found(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wider search found more, judged it, still nothing: the KB genuinely lacks it."""
    calls = _script(monkeypatch, [Verdict.NOT_FOUND, Verdict.NOT_FOUND])
    with store.connect(seeded) as conn:
        verdicts = judge_claims(
            conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 3)}
        ).verdicts

    assert len(calls) == 2, "the NOT_FOUND claim was not retried"
    assert calls[1] > calls[0], "the retry did not widen the search"
    assert verdicts[0].verdict == Verdict.NOT_FOUND
    assert verdicts[0].tag == VerdictTag.KB_GAP
    assert verdicts[0].attempts == 2


def test_not_found_that_the_retry_rescues_is_tagged_retrieval_miss(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The content was there all along; our first search missed it."""
    _script(monkeypatch, [Verdict.NOT_FOUND, Verdict.SUPPORTED])
    with store.connect(seeded) as conn:
        verdicts = judge_claims(
            conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 3)}
        ).verdicts

    assert verdicts[0].verdict == Verdict.SUPPORTED
    assert verdicts[0].tag == VerdictTag.RETRIEVAL_MISS
    assert verdicts[0].attempts == 2


def test_a_contradiction_found_on_retry_still_counts_as_a_retrieval_miss(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tag describes our retrieval, the verdict describes Ava. Both are kept."""
    _script(monkeypatch, [Verdict.NOT_FOUND, Verdict.CONTRADICTED])
    with store.connect(seeded) as conn:
        verdicts = judge_claims(
            conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 3)}
        ).verdicts

    assert verdicts[0].verdict == Verdict.CONTRADICTED
    assert verdicts[0].tag == VerdictTag.RETRIEVAL_MISS


def test_retry_is_recorded_as_attempt_two(seeded: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The retrieval history has to be auditable per attempt."""
    _script(monkeypatch, [Verdict.NOT_FOUND, Verdict.NOT_FOUND])
    with store.connect(seeded) as conn:
        judge_claims(conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 3)})

    with store.connect(seeded) as conn:
        assert store.get_retrievals(conn, "c0", attempt=2)


def test_no_claims_is_not_an_error(seeded: str) -> None:
    with store.connect(seeded) as conn:
        batch = judge_claims(conn, [], {})
    assert batch.verdicts == [] and batch.failures == {}


# --- rendering and prompts ------------------------------------------------


def test_excerpts_carry_chunk_ids(seeded: str) -> None:
    """Without the id, a flag is an assertion with no way to check it."""
    with store.connect(seeded) as conn:
        chunks = store.search_chunks(conn, _query(), 2)
    rendered = render_excerpts(chunks)
    assert all(c.chunk_id in rendered for c in chunks)


def test_excerpts_cite_the_chunk_header_as_source(seeded: str) -> None:
    """The header is how the judge knows which program an excerpt belongs to."""
    with store.connect(seeded) as conn:
        chunks = store.search_chunks(conn, _query(), 1)
    rendered = render_excerpts(chunks)
    assert "    source: S\n" in rendered
    assert "d.docx" not in rendered


def test_dimension_mismatch_explains_itself(seeded: str) -> None:
    """Swapping embedding model or dimension without re-indexing must say so.

    pgvector's own failure here is "different vector dimensions", naming neither
    the cause nor the fix, and this is a mistake that happens the first time
    anyone moves from the fake embedder to a real one.
    """
    with pytest.raises(RuntimeError, match="index-kb --force"):
        with store.connect(seeded) as conn:
            store.search_chunks(conn, [1.0, 1.0], 2)


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


# --- live judge and per-claim failures ------------------------------------

_CHUNK_ID_RE = re.compile(r"chunk_id: (\S+)")


def _use_live_judge(monkeypatch: pytest.MonkeyPatch, answer) -> list[str]:
    """Switch step 6 to the live path with a fake model. Returns every prompt sent.

    ``answer(prompt, call_number)`` returns a JudgeResponse or raises. Answers are
    chosen from the prompt, not call order, because the first pass is threaded.
    conftest's force_offline restores stub mode for the next test.
    """
    object.__setattr__(config.settings, "stub_llm", False)
    prompts: list[str] = []

    def fake_generate_structured(prompt, schema, **kwargs):
        assert schema is JudgeResponse
        prompts.append(prompt)
        return answer(prompt, len(prompts))

    monkeypatch.setattr(s6_judge, "generate_structured", fake_generate_structured)
    return prompts


def _first_shown_chunk(prompt: str) -> str:
    """The first chunk id rendered into a judge prompt."""
    return _CHUNK_ID_RE.search(prompt).group(1)


def _verdict_count(conn: store.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM verdicts").fetchone()["n"]


def test_live_prompt_carries_claim_subject_and_every_chunk_id(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = _use_live_judge(
        monkeypatch,
        lambda p, n: JudgeResponse(
            verdict=Verdict.SUPPORTED, rationale="r", evidence_chunk_id=_first_shown_chunk(p)
        ),
    )
    with store.connect(seeded) as conn:
        chunks = store.search_chunks(conn, _query(), 3)
        judge_claims(conn, [_claim()], {"c0": chunks})

    prompt = prompts[0]
    assert "the minimum gpa is 2.0" in prompt
    assert "Subject: S" in prompt
    assert all(f"chunk_id: {c.chunk_id}" in prompt for c in chunks)
    assert "{{" not in prompt


def test_live_verdict_citing_a_shown_chunk_is_saved(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_live_judge(
        monkeypatch,
        lambda p, n: JudgeResponse(
            verdict=Verdict.CONTRADICTED, rationale="r", evidence_chunk_id=_first_shown_chunk(p)
        ),
    )
    with store.connect(seeded) as conn:
        chunks = store.search_chunks(conn, _query(), 3)
        batch = judge_claims(conn, [_claim()], {"c0": chunks})

    assert batch.failures == {}
    assert batch.verdicts[0].evidence_chunk_id == chunks[0].chunk_id


def test_made_up_evidence_id_fails_the_claim_and_saves_nothing(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hallucinated id would silently break the join to kb_chunks."""
    _use_live_judge(
        monkeypatch,
        lambda p, n: JudgeResponse(
            verdict=Verdict.SUPPORTED, rationale="r", evidence_chunk_id="k999"
        ),
    )
    with store.connect(seeded) as conn:
        batch = judge_claims(conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 3)})
        assert _verdict_count(conn) == 0

    assert batch.verdicts == []
    assert "k999" in batch.failures["c0"]


@pytest.mark.parametrize("verdict", [Verdict.SUPPORTED, Verdict.CONTRADICTED])
def test_a_flag_without_evidence_fails_the_claim(
    seeded: str, monkeypatch: pytest.MonkeyPatch, verdict: Verdict
) -> None:
    _use_live_judge(
        monkeypatch,
        lambda p, n: JudgeResponse(verdict=verdict, rationale="r", evidence_chunk_id=""),
    )
    with store.connect(seeded) as conn:
        batch = judge_claims(conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 3)})

    assert batch.verdicts == []
    assert "c0" in batch.failures


def test_not_found_without_evidence_is_accepted(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_live_judge(
        monkeypatch,
        lambda p, n: JudgeResponse(verdict=Verdict.NOT_FOUND, rationale="r", evidence_chunk_id=""),
    )
    with store.connect(seeded) as conn:
        batch = judge_claims(conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 3)})

    assert batch.failures == {}
    assert batch.verdicts[0].tag == VerdictTag.KB_GAP


def test_one_failed_claim_does_not_lose_the_others(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(prompt: str, n: int) -> JudgeResponse:
        if "FAIL-ME" in prompt:
            raise LlmError("quota exhausted")
        return JudgeResponse(
            verdict=Verdict.SUPPORTED, rationale="r", evidence_chunk_id=_first_shown_chunk(prompt)
        )

    _use_live_judge(monkeypatch, answer)
    claims = [
        Claim(claim_id=cid, session_id="s1", pair_index=0, subject_tag="S", text=text)
        for cid, text in [("c0", "claim zero"), ("c1", "FAIL-ME claim"), ("c2", "claim two")]
    ]
    with store.connect(seeded) as conn:
        store.replace_claims_for_session(conn, "s1", claims)
        chunks = store.search_chunks(conn, _query(), 3)
        batch = judge_claims(conn, claims, {c.claim_id: chunks for c in claims})
        assert _verdict_count(conn) == 2

    assert sorted(v.claim_id for v in batch.verdicts) == ["c0", "c2"]
    assert batch.failures == {"c1": "quota exhausted"}


def test_failed_retry_keeps_the_first_pass_not_found(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry never ran, so no kb_gap/retrieval_miss tag can be claimed."""

    def answer(prompt: str, n: int) -> JudgeResponse:
        if n == 1:
            return JudgeResponse(
                verdict=Verdict.NOT_FOUND, rationale="nothing", evidence_chunk_id=""
            )
        raise LlmError("retry blew up")

    _use_live_judge(monkeypatch, answer)
    with store.connect(seeded) as conn:
        batch = judge_claims(conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 3)})

    assert batch.failures == {}
    verdict = batch.verdicts[0]
    assert (verdict.verdict, verdict.attempts, verdict.tag) == (Verdict.NOT_FOUND, 1, None)
    assert "retry failed: retry blew up" in verdict.rationale


def test_failed_wider_search_keeps_the_first_pass_not_found(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, [Verdict.NOT_FOUND])

    def broken_retrieval(*args, **kwargs):
        raise LlmError("embedding failed")

    monkeypatch.setattr(s6_judge, "retrieve_for_claims", broken_retrieval)
    with store.connect(seeded) as conn:
        batch = judge_claims(conn, [_claim()], {"c0": store.search_chunks(conn, _query(), 3)})

    verdict = batch.verdicts[0]
    assert (verdict.verdict, verdict.attempts, verdict.tag) == (Verdict.NOT_FOUND, 1, None)
    assert "embedding failed" in verdict.rationale

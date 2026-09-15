"""Step 6 — judge each claim against its retrieved evidence.  [STUB verdict, real retry]

One pass, three possible answers: SUPPORTED, CONTRADICTED, NOT_FOUND. Not two
sequential passes (gap check, then contradiction check) — that reads the same
evidence twice to answer one question in two halves, at double the cost and
latency for no accuracy gain.

The retry rule is the part with teeth, and it is asymmetric on purpose:

* **NOT_FOUND retries** with a wider search. "The knowledge base does not cover
  this" and "our search failed to find it" look identical at this point but
  point at completely different people — TTU's content team versus our own
  retrieval tuning. Widening the search is how we tell them apart, and the
  answer decides whether the claim is tagged ``kb_gap`` or ``retrieval_miss``.

* **CONTRADICTED never retries.** A contradiction against a genuinely relevant
  chunk is final. Retrying it would let the judge go shopping for a friendlier
  chunk somewhere else in the knowledge base, which is precisely how a real
  hallucination gets quietly buried.

A claim whose judge call fails (``LlmError``: quota, bad JSON, or a made-up
evidence id) is skipped, not fatal: the rest of the conversation's verdicts are
still saved, and the failed claim stays unjudged so the report can say so. A
failed *retry* falls back to the first-pass NOT_FOUND, untagged.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Sequence

from ..config import settings
from ..db import store
from ..db.store import Connection
from ..llm import load_prompt
from ..llm.client import LlmError, generate_structured
from ..models import Claim, ClaimVerdict, JudgeResponse, RetrievedChunk, Verdict, VerdictTag
from .s5_retrieve import retrieve_for_claims

_WORD_RE = re.compile(r"[a-z0-9.]+")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "for",
    "from",
    "of",
    "to",
    "in",
    "on",
    "at",
    "and",
    "or",
    "you",
    "your",
    "all",
    "any",
    "must",
    "can",
    "will",
    "with",
    "that",
    "this",
    "it",
    "as",
    "by",
}


def render_excerpts(chunks: Sequence[RetrievedChunk]) -> str:
    """Format retrieved chunks for the judge prompt.

    Each excerpt carries its ``chunk_id`` because the judge has to name the one
    that decided the verdict. Without that, a flag would be an assertion with no
    way to check it — and checkability is the entire product here.

    ``source`` is the chunk's ``Group > Topic > Label`` header. It is how the
    judge knows which program an excerpt belongs to; the file name is the same
    for every chunk and would tell it nothing.
    """
    if not chunks:
        return "(No excerpts were retrieved for this claim.)"

    parts: list[str] = []
    for chunk in chunks:
        parts.append(
            f"--- chunk_id: {chunk.chunk_id}\n"
            f"    source: {chunk.section}\n"
            f"    similarity: {chunk.score:.3f}\n\n"
            f"{chunk.text}"
        )
    return "\n\n".join(parts)


def _tokens(text: str) -> set[str]:
    """Content words of a string, for the stub's overlap heuristic."""
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}


def _judge_stub(claim: Claim, chunks: Sequence[RetrievedChunk]) -> JudgeResponse:
    """Offline stand-in: lexical overlap plus a numeric disagreement check.

    Crude, but deliberately capable of producing all three verdicts so the retry
    path and the severity rollup both get exercised offline. The rule is: strong
    word overlap and no conflicting number means SUPPORTED; strong overlap but a
    different number for the same topic means CONTRADICTED; weak overlap means
    NOT_FOUND.

    What it cannot do is the actual job — judge entailment, apply the
    general-policy-covers-specific-claim rule, or notice that "fully
    asynchronous" and "no scheduled class meetings" say the same thing. That is
    what ``_judge_live`` is for.
    """
    if not chunks:
        return JudgeResponse(
            verdict=Verdict.NOT_FOUND,
            rationale="[stub] No excerpts were retrieved.",
            evidence_chunk_id="",
        )

    claim_tokens = _tokens(claim.text)
    best = chunks[0]
    best_overlap = 0.0
    best_chunk = best

    for chunk in chunks:
        chunk_tokens = _tokens(chunk.text)
        if not claim_tokens:
            continue
        overlap = len(claim_tokens & chunk_tokens) / len(claim_tokens)
        if overlap > best_overlap:
            best_overlap, best_chunk = overlap, chunk

    if best_overlap < 0.35:
        return JudgeResponse(
            verdict=Verdict.NOT_FOUND,
            rationale=f"[stub] Best excerpt overlapped only {best_overlap:.0%} of the claim.",
            evidence_chunk_id="",
        )

    claim_numbers = set(_NUMBER_RE.findall(claim.text))
    chunk_numbers = set(_NUMBER_RE.findall(best_chunk.text))
    if claim_numbers and chunk_numbers and not (claim_numbers & chunk_numbers):
        return JudgeResponse(
            verdict=Verdict.CONTRADICTED,
            rationale=(
                f"[stub] Claim states {sorted(claim_numbers)} but the excerpt "
                f"states {sorted(chunk_numbers)}."
            ),
            evidence_chunk_id=best_chunk.chunk_id,
        )

    return JudgeResponse(
        verdict=Verdict.SUPPORTED,
        rationale=f"[stub] Excerpt covers {best_overlap:.0%} of the claim's content words.",
        evidence_chunk_id=best_chunk.chunk_id,
    )


def _check_evidence(
    claim: Claim, response: JudgeResponse, chunks: Sequence[RetrievedChunk]
) -> JudgeResponse:
    """Reject a verdict whose evidence id cannot be traced to a shown excerpt.

    A made-up id breaks the join to ``kb_chunks`` in ``v_claim_results`` and the
    flag silently loses its evidence; a SUPPORTED or CONTRADICTED with no id at
    all is a flag nobody can check. Both fail the claim rather than being saved.
    NOT_FOUND may cite nothing, or a real chunk it found nearby.

    Raises:
        LlmError: if the id is unknown, or missing on a non-NOT_FOUND verdict.
    """
    allowed = [chunk.chunk_id for chunk in chunks]
    cited = response.evidence_chunk_id.strip()

    if cited and cited not in allowed:
        raise LlmError(
            f"judge cited evidence_chunk_id {cited!r} for claim {claim.claim_id}, "
            f"which was not among the excerpts shown: {allowed}"
        )
    if not cited and response.verdict != Verdict.NOT_FOUND:
        raise LlmError(
            f"judge returned {response.verdict.value} for claim {claim.claim_id} "
            f"without an evidence_chunk_id"
        )
    return response.model_copy(update={"evidence_chunk_id": cited})


def _judge_live(claim: Claim, chunks: Sequence[RetrievedChunk]) -> JudgeResponse:
    """Ask Gemini for a verdict on one claim against its excerpts.

    The prompt at ``llm/prompts/judge_claim.md`` carries both rubric rules:
    excerpts only, no outside knowledge; and general policy satisfies a
    program-specific claim unless an override appears. The "excerpts only" rule
    is the one to guard -- without it verdicts become an invisible mix of
    "grounded in TTU documentation" and "sounded plausible to a model".

    Raises:
        LlmError: if the call fails, the reply does not match the schema, or the
            cited evidence id is not one of ``chunks`` (see ``_check_evidence``).
    """
    prompt = load_prompt(
        "judge_claim",
        subject_tag=claim.subject_tag or "(not specified)",
        claim_text=claim.text,
        excerpts=render_excerpts(chunks),
    )
    response = generate_structured(prompt, JudgeResponse)
    return _check_evidence(claim, response, chunks)


def judge_claim(claim: Claim, chunks: Sequence[RetrievedChunk]) -> JudgeResponse:
    """Judge one claim, via the stub or the live call."""
    if settings.stub_llm:
        return _judge_stub(claim, chunks)
    return _judge_live(claim, chunks)


def _to_verdict(
    claim_id: str,
    response: JudgeResponse,
    attempts: int,
    tag: VerdictTag | None,
    kb_content_hash: str | None,
) -> ClaimVerdict:
    """Convert a judge response into a persistable verdict.

    ``kb_content_hash`` records which KB version the verdict was judged against.
    Chunk ids are positional, so after a re-index the same id can name different
    text; the hash is what lets the report warn about that.
    """
    return ClaimVerdict(
        claim_id=claim_id,
        verdict=response.verdict,
        tag=tag,
        rationale=response.rationale,
        evidence_chunk_id=response.evidence_chunk_id or None,
        attempts=attempts,
        kb_content_hash=kb_content_hash,
    )


@dataclass
class JudgeBatch:
    """What judging one batch of claims produced.

    ``failures`` maps claim id to error message for claims that got no verdict.
    """

    verdicts: list[ClaimVerdict] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)


def _first_pass_fallback(
    claim_id: str, response: JudgeResponse, error: Exception, kb_hash: str | None
) -> ClaimVerdict:
    """Keep the first-pass NOT_FOUND when the retry could not run.

    Untagged and ``attempts=1``, because the retry that decides kb_gap versus
    retrieval_miss never happened -- and the rationale says so, for the audit.
    """
    noted = response.model_copy(
        update={"rationale": f"{response.rationale} [retry failed: {error}]"}
    )
    return _to_verdict(claim_id, noted, attempts=1, tag=None, kb_content_hash=kb_hash)


def judge_claims(
    conn: Connection,
    claims: Sequence[Claim],
    retrievals: dict[str, list[RetrievedChunk]],
) -> JudgeBatch:
    """Judge a batch of claims, applying the NOT_FOUND retry policy.

    Args:
        conn: An open database connection.
        claims: Claims to judge.
        retrievals: First-attempt chunks per claim id, from step 5.

    Returns:
        The saved verdicts (persisted as a side effect) and the claims that
        could not be judged, with their errors.
    """
    batch = JudgeBatch()
    if not claims:
        return batch

    by_id = {claim.claim_id: claim for claim in claims}
    first_pass: dict[str, JudgeResponse] = {}
    # Read once so every verdict in this batch records the same KB version.
    kb_hash = store.current_kb_hash(conn)

    # Judge calls are independent per claim -- same justification as step 5.
    with ThreadPoolExecutor(max_workers=settings.max_workers) as pool:
        futures = {
            pool.submit(judge_claim, claim, retrievals.get(claim.claim_id, [])): claim.claim_id
            for claim in claims
        }
        for future in as_completed(futures):
            claim_id = futures[future]
            try:
                first_pass[claim_id] = future.result()
            except LlmError as exc:
                # Only LlmError: a TypeError or DB error is a bug, not a flaky call.
                batch.failures[claim_id] = str(exc)

    verdicts = batch.verdicts
    judged = [claim for claim in claims if claim.claim_id in first_pass]
    needs_retry = [
        claim for claim in judged if first_pass[claim.claim_id].verdict == Verdict.NOT_FOUND
    ]

    # Everything that was not NOT_FOUND is final, including every CONTRADICTED.
    for claim in judged:
        response = first_pass[claim.claim_id]
        if response.verdict != Verdict.NOT_FOUND:
            verdicts.append(
                _to_verdict(claim.claim_id, response, attempts=1, tag=None, kb_content_hash=kb_hash)
            )

    if needs_retry:
        wider_k = settings.top_k * settings.retry_k_multiplier
        try:
            wider = retrieve_for_claims(conn, needs_retry, k=wider_k, attempt=2)
        except LlmError as exc:
            # The wider search itself failed (embedding call): no retry can run.
            for claim in needs_retry:
                verdicts.append(
                    _first_pass_fallback(claim.claim_id, first_pass[claim.claim_id], exc, kb_hash)
                )
            needs_retry = []
            wider = {}

        # Every NOT_FOUND gets the second look, unconditionally. Search is exact
        # and has no score floor, so the wider top-k always contains the first
        # top-k plus new chunks; a "did we find anything new?" shortcut could
        # never fire on a real KB. Skipping the retry would also risk blaming
        # TTU's docs (kb_gap) for what was really our search missing.
        for claim in needs_retry:
            try:
                retry_response = judge_claim(claim, wider.get(claim.claim_id, []))
            except LlmError as exc:
                verdicts.append(
                    _first_pass_fallback(claim.claim_id, first_pass[claim.claim_id], exc, kb_hash)
                )
                continue
            if retry_response.verdict == Verdict.NOT_FOUND:
                # Even with the wider set, nothing covers this claim.
                verdicts.append(
                    _to_verdict(claim.claim_id, retry_response, 2, VerdictTag.KB_GAP, kb_hash)
                )
            else:
                # The content existed all along; our first search missed it.
                # That is a fact about our retrieval quality, not about Ava or
                # about TTU's knowledge base, and it is tagged as such.
                verdicts.append(
                    _to_verdict(
                        claim.claim_id, retry_response, 2, VerdictTag.RETRIEVAL_MISS, kb_hash
                    )
                )

    for verdict in verdicts:
        store.save_verdict(conn, verdict)

    return batch

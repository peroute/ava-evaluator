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

IMPLEMENTATION STATUS: the verdict call is stubbed (``_judge_stub`` /
``_judge_live``). Everything around it — parallelism, the retry pass, tagging —
is real and is what the stub exists to let you verify.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Sequence

from ..config import settings
from ..db import store
from ..db.store import Connection
from ..llm import load_prompt
from ..llm.client import generate_structured
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
    """
    if not chunks:
        return "(No excerpts were retrieved for this claim.)"

    parts: list[str] = []
    for chunk in chunks:
        parts.append(
            f"--- chunk_id: {chunk.chunk_id}\n"
            f"    source: {chunk.source_doc} > {chunk.section}\n"
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


def _judge_live(claim: Claim, chunks: Sequence[RetrievedChunk]) -> JudgeResponse:
    """Ask Gemini for a verdict on one claim against its excerpts.

    TODO(hedi): implement. Roughly::

        prompt = load_prompt(
            "judge_claim",
            subject_tag=claim.subject_tag or "(not specified)",
            claim_text=claim.text,
            excerpts=render_excerpts(chunks),
        )
        return generate_structured(prompt, JudgeResponse)

    Things worth knowing before you write it:

    * The prompt at ``llm/prompts/judge_claim.md`` already encodes both rubric
      rules — excerpts only, no outside knowledge; and general policy satisfies
      a program-specific claim unless an override appears.
    * The "excerpts only" instruction is the one to guard. A judge that leans on
      what it already knows about universities produces a silent mix of
      "grounded in TTU documentation" and "sounded plausible to a language
      model", and nothing downstream can separate them. If you change one thing
      in that prompt, do not change that.
    * Consider verifying that ``evidence_chunk_id`` is actually one of the
      chunks you passed in. A hallucinated id breaks the join to ``kb_chunks``
      in ``v_claim_results`` and the flag silently loses its evidence.
    * Temperature is already 0 in ``generate_structured``. An evaluator that
      gives different verdicts on identical evidence is not an evaluator.
    """
    raise NotImplementedError(
        "Step 6 is not implemented yet. Run with AVA_STUB_LLM=1, or implement "
        "_judge_live in ava_eval/steps/s6_judge.py."
    )


def judge_claim(claim: Claim, chunks: Sequence[RetrievedChunk]) -> JudgeResponse:
    """Judge one claim, via the stub or the live call."""
    if settings.stub_llm:
        return _judge_stub(claim, chunks)
    return _judge_live(claim, chunks)


def _to_verdict(
    claim_id: str, response: JudgeResponse, attempts: int, tag: VerdictTag | None
) -> ClaimVerdict:
    """Convert a judge response into a persistable verdict."""
    return ClaimVerdict(
        claim_id=claim_id,
        verdict=response.verdict,
        tag=tag,
        rationale=response.rationale,
        evidence_chunk_id=response.evidence_chunk_id or None,
        attempts=attempts,
    )


def judge_claims(
    conn: Connection,
    claims: Sequence[Claim],
    retrievals: dict[str, list[RetrievedChunk]],
) -> list[ClaimVerdict]:
    """Judge a batch of claims, applying the NOT_FOUND retry policy.

    Args:
        conn: An open database connection.
        claims: Claims to judge.
        retrievals: First-attempt chunks per claim id, from step 5.

    Returns:
        One verdict per claim, persisted as a side effect.
    """
    if not claims:
        return []

    by_id = {claim.claim_id: claim for claim in claims}
    first_pass: dict[str, JudgeResponse] = {}

    # Judge calls are independent per claim -- same justification as step 5.
    with ThreadPoolExecutor(max_workers=settings.max_workers) as pool:
        futures = {
            pool.submit(judge_claim, claim, retrievals.get(claim.claim_id, [])): claim.claim_id
            for claim in claims
        }
        for future in as_completed(futures):
            first_pass[futures[future]] = future.result()

    verdicts: list[ClaimVerdict] = []
    needs_retry = [
        by_id[claim_id]
        for claim_id, response in first_pass.items()
        if response.verdict == Verdict.NOT_FOUND
    ]

    # Everything that was not NOT_FOUND is final, including every CONTRADICTED.
    for claim in claims:
        response = first_pass[claim.claim_id]
        if response.verdict != Verdict.NOT_FOUND:
            verdicts.append(_to_verdict(claim.claim_id, response, attempts=1, tag=None))

    if needs_retry:
        wider_k = settings.top_k * settings.retry_k_multiplier
        wider = retrieve_for_claims(conn, needs_retry, k=wider_k, attempt=2)

        for claim in needs_retry:
            original_ids = {c.chunk_id for c in retrievals.get(claim.claim_id, [])}
            widened = wider.get(claim.claim_id, [])
            fresh = [c for c in widened if c.chunk_id not in original_ids]

            if not fresh:
                # The wider net caught nothing new, so the first search was not
                # the limiting factor. The knowledge base genuinely lacks this.
                verdicts.append(
                    _to_verdict(claim.claim_id, first_pass[claim.claim_id], 1, VerdictTag.KB_GAP)
                )
                continue

            retry_response = judge_claim(claim, widened)
            if retry_response.verdict == Verdict.NOT_FOUND:
                verdicts.append(_to_verdict(claim.claim_id, retry_response, 2, VerdictTag.KB_GAP))
            else:
                # The content existed all along; our first search missed it.
                # That is a fact about our retrieval quality, not about Ava or
                # about TTU's knowledge base, and it is tagged as such.
                verdicts.append(
                    _to_verdict(claim.claim_id, retry_response, 2, VerdictTag.RETRIEVAL_MISS)
                )

    for verdict in verdicts:
        store.save_verdict(conn, verdict)

    return verdicts

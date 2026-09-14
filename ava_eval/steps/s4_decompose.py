"""Step 4 — break flagged replies into self-contained claims.

The hardest step, and the one that makes everything after it possible. Step 5
searches the knowledge base using nothing but a claim's own text, so a claim
that reads "the minimum GPA is 2.0" cannot be checked at all — 2.0 for which
program? This step resolves each claim against the conversation and puts the
scope *inside* the claim.

Two locked design decisions are implemented in the transcript rendering below:

* **One call per conversation, not per turn.** Sending a growing history with
  each turn re-sends turn 1 in every later call, so cost grows roughly with the
  square of conversation length while the model learns nothing new from the
  repetition. One call also gives one coherent read of the conversation rather
  than several independent scoping judgements that can quietly disagree.
* **Conversations are processed linearly**, not fanned out in parallel.
  Throughput is handled in steps 5 and 6 where the calls are genuinely
  independent; here, keeping it sequential keeps failures easy to attribute.

``_decompose_stub`` runs offline (``AVA_STUB_LLM=1``); ``_decompose_live``
calls Gemini.
"""

from __future__ import annotations

import re

from ..config import settings
from ..llm import load_prompt
from ..llm.client import generate_structured
from ..models import Claim, ClassifiedTurn, ConversationClaims, ExtractedClaim, Turn, TurnClaims


def render_transcript(turns: list[Turn], classifications: dict[int, ClassifiedTurn]) -> str:
    """Render a conversation for the decomposition prompt.

    Both sides of every turn are included, including turns with nothing to
    check. That is deliberate: the caller's own words are the clearest signal
    that the topic just changed, and a turn that contributes no claims of its
    own may still be the turn that establishes which program the next three
    claims are about.
    """
    lines: list[str] = []
    for turn in turns:
        flag = classifications.get(turn.pair_index)
        contains = bool(flag and flag.contains_claims)
        lines.append(f"[pair {turn.pair_index}] caller: {turn.caller_asked}")
        lines.append(f"{' ' * 11}agent:  {turn.agent_said}")
        lines.append(f"{' ' * 11}contains_claims: {str(contains).lower()}")
        lines.append("")
    return "\n".join(lines).strip()


def _sentences(text: str) -> list[str]:
    """Split a reply into sentences for the stub's crude decomposition."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _decompose_stub(
    session_id: str, turns: list[Turn], classifications: dict[int, ClassifiedTurn]
) -> ConversationClaims:
    """Offline stand-in: one claim per fact-bearing sentence, no scope resolution.

    Note what this stub cannot do, because it is the whole reason step 4 needs a
    language model: it cannot tell that "you need a 2.0 GPA" is about the Online
    BBA discussed two turns earlier. It emits the sentence as-is, unscoped. The
    live implementation's job is exactly the part the stub skips.

    The canned output is shaped to exercise every downstream branch — some turns
    yield several claims, some yield none despite being flagged.
    """
    result: list[TurnClaims] = []
    subject = "[stub] unresolved subject"

    for turn in turns:
        flag = classifications.get(turn.pair_index)
        if not flag or not flag.contains_claims:
            continue

        claims = [
            ExtractedClaim(text=sentence, scope_derived_from_pair_index=[])
            for sentence in _sentences(turn.agent_said)
            if len(sentence.split()) >= 4 and not sentence.endswith("?")
        ]
        result.append(TurnClaims(pair_index=turn.pair_index, subject_tag=subject, claims=claims))

    return ConversationClaims(turns=result)


def _decompose_live(
    session_id: str, turns: list[Turn], classifications: dict[int, ClassifiedTurn]
) -> ConversationClaims:
    """Ask Gemini to decompose a whole conversation into self-contained claims.

    Things worth knowing:

    * ``render_transcript`` above already produces the format the prompt's
      worked example uses. Keep them in sync — the example is what teaches the
      model to read the format.
    * The prompt at ``llm/prompts/decompose_claims.md`` carries all four
      anti-scope-drift devices: never inherit subject by default, read
      ``caller_asked``, emit a per-turn ``subject_tag``, and record
      ``scope_derived_from_pair_index`` per claim.
    * A returned turn with an empty ``claims`` list is a normal outcome, not an
      error. Step 3 is over-inclusive on purpose, so some flagged turns will
      correctly yield nothing. ``to_claims`` below already handles it.
    * The model's ``pair_index`` values are not trusted here; ``to_claims``
      drops any that are not in this conversation.
    """
    # Nothing flagged means nothing to decompose -- don't pay for a call.
    if not any(c.contains_claims for c in classifications.values()):
        return ConversationClaims(turns=[])

    prompt = load_prompt(
        "decompose_claims",
        session_id=session_id,
        transcript=render_transcript(turns, classifications),
    )
    return generate_structured(prompt, ConversationClaims)


def decompose_conversation(
    session_id: str, turns: list[Turn], classifications: dict[int, ClassifiedTurn]
) -> ConversationClaims:
    """Decompose one conversation, via the stub or the live call."""
    if settings.stub_llm:
        return _decompose_stub(session_id, turns, classifications)
    return _decompose_live(session_id, turns, classifications)


def to_claims(
    session_id: str, response: ConversationClaims, valid_indices: set[int]
) -> list[Claim]:
    """Flatten a decomposition response into persistable claims.

    Claims attributed to a ``pair_index`` that is not in this conversation are
    dropped rather than stored against the wrong turn.

    Claim ids are positional (``{session}__p{pair}__c{n}``) rather than random,
    so re-running step 4 on an unchanged conversation produces the same ids and
    the audit trail stays comparable between runs.

    The position counter is kept per ``pair_index`` rather than per entry: if
    the model returns the same turn twice, restarting at ``c0`` would produce
    duplicate claim ids and the insert would fail for the whole conversation.
    """
    claims: list[Claim] = []
    next_position: dict[int, int] = {}
    for turn_claims in response.turns:
        if turn_claims.pair_index not in valid_indices:
            continue
        for extracted in turn_claims.claims:
            text = extracted.text.strip()
            if not text:
                continue
            position = next_position.get(turn_claims.pair_index, 0)
            next_position[turn_claims.pair_index] = position + 1
            claims.append(
                Claim(
                    claim_id=f"{session_id}__p{turn_claims.pair_index}__c{position}",
                    session_id=session_id,
                    pair_index=turn_claims.pair_index,
                    subject_tag=turn_claims.subject_tag.strip(),
                    text=text,
                    scope_derived_from=[
                        i for i in extracted.scope_derived_from_pair_index if i in valid_indices
                    ],
                )
            )
    return claims

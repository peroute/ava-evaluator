"""Step 3 — decide which turns contain anything worth checking.

A cheap gate in front of the expensive steps. Without it, every greeting and
"let me connect you" burns a decomposition and a judge call, and — worse —
pollutes the gap-rate metrics with meaningless NOT_FOUND verdicts on turns that
never asserted anything.

The gate is deliberately coarse: a reply that states a fact *and* then asks a
follow-up question is still `true`. Separating claim from filler precisely is
step 4's job, and step 4 can only do it for turns this step lets through.

This step only asks *whether* a checkable claim is present. It does not decide
whether the claim is true, look anything up in the knowledge base, or pull the
individual claims out; those are steps 4-6.

``_classify_stub`` runs offline (``AVA_STUB_LLM=1``); ``_classify_live`` asks
Gemini.
"""

from __future__ import annotations

import re

from ..config import settings
from ..llm import load_prompt
from ..llm.client import generate_structured
from ..models import ClassifiedTurn, Turn, TurnClassification

# Openers that make a reply purely conversational. Used ONLY by the stub.
_FILLER_PATTERNS = [
    r"^(hi|hello|hey|good (morning|afternoon|evening))\b",
    r"^(thanks|thank you|you'?re welcome|no problem|of course)\b",
    r"^(sure|great question|absolutely|happy to help)[.! ]*$",
    r"^(let me|one moment|hold on|i'?ll connect|i can transfer)\b",
    r"^(is there anything else|can i help you with anything else)\b",
    r"^(have a great|take care|goodbye|bye)\b",
]

_FILLER_RE = re.compile("|".join(_FILLER_PATTERNS), re.IGNORECASE)

# Signals that a sentence asserts something checkable. Used ONLY by the stub.
_FACT_RE = re.compile(
    r"\b(\d|gpa|credit|deadline|require|must|tuition|cost|fee|semester|"
    r"asynchronous|synchronous|online|accredited|transcript|apply|application|"
    r"admission|scholarship|financial aid|program|degree|course)\b",
    re.IGNORECASE,
)


def _classify_stub(turn: Turn) -> TurnClassification:
    """Offline stand-in: a keyword heuristic.

    This heuristic is exactly the approach the design rejected for production,
    and it is worth understanding why, because the stub demonstrates the failure
    itself. "Feel free to call back any time" and "You can defer enrollment for
    one term" are structurally identical — same mood, same length, same shape —
    and only one asserts a fact. No keyword list separates them reliably.

    It is fine here because its only job is to produce varied, deterministic
    output so the rest of the pipeline can be exercised offline.
    """
    text = turn.agent_said.strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    substantive = [s for s in sentences if not _FILLER_RE.match(s)]

    if not substantive:
        return TurnClassification(
            contains_claims=False, reason="[stub] Reply is entirely conversational filler."
        )

    if any(_FACT_RE.search(s) for s in substantive):
        return TurnClassification(
            contains_claims=True, reason="[stub] Contains fact-bearing vocabulary."
        )

    return TurnClassification(
        contains_claims=False, reason="[stub] No fact-bearing vocabulary found."
    )


def _classify_live(turn: Turn) -> TurnClassification:
    """Ask Gemini whether this turn contains a checkable claim.

    * ``generate_structured`` handles retries, temperature 0, and validating the
      reply against ``TurnClassification``. If it returns, the object is
      well-formed and ``contains_claims`` is a real bool.
    * The prompt at ``llm/prompts/classify_turn.md`` carries the over-inclusive
      rule and its worked examples.
    * Only this turn is sent. Deciding whether a sentence is a greeting does not
      depend on earlier turns, so history would cost tokens and buy nothing. The
      caller's question is included as context for reading the reply; the
      decision is about the reply.
    * It runs once per turn, which makes it the highest-volume call in the
      pipeline; it uses the configured model.
    """
    prompt = load_prompt(
        "classify_turn",
        caller_asked=turn.caller_asked,
        agent_said=turn.agent_said,
    )
    return generate_structured(prompt, TurnClassification)


def classify_turn(turn: Turn) -> TurnClassification:
    """Classify one turn, using the stub or the live call depending on config."""
    if settings.stub_llm:
        return _classify_stub(turn)
    return _classify_live(turn)


def classify_turns(turns: list[Turn]) -> list[ClassifiedTurn]:
    """Classify a batch of turns, preserving order."""
    results: list[ClassifiedTurn] = []
    for turn in turns:
        outcome = classify_turn(turn)
        results.append(
            ClassifiedTurn(
                session_id=turn.session_id,
                pair_index=turn.pair_index,
                contains_claims=outcome.contains_claims,
                reason=outcome.reason,
            )
        )
    return results

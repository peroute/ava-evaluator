"""Step 3: flag which turns contain a checkable claim. See docs/steps/s3-classify.md."""

from __future__ import annotations

import re

from ..config import settings
from ..llm import load_prompt
from ..llm.client import generate_structured
from ..models import ClassifiedTurn, Turn, TurnClassification

# Stub only: openers that make a sentence conversational filler.
_FILLER_PATTERNS = [
    r"^(hi|hello|hey|good (morning|afternoon|evening))\b",
    r"^(thanks|thank you|you'?re welcome|no problem|of course)\b",
    r"^(sure|great question|absolutely|happy to help)[.! ]*$",
    r"^(let me|one moment|hold on|i'?ll connect|i can transfer)\b",
    r"^(is there anything else|can i help you with anything else)\b",
    r"^(have a great|take care|goodbye|bye)\b",
]

_FILLER_RE = re.compile("|".join(_FILLER_PATTERNS), re.IGNORECASE)

# Stub only: words that suggest a sentence asserts something checkable.
_FACT_RE = re.compile(
    r"\b(\d|gpa|credit|deadline|require|must|tuition|cost|fee|semester|"
    r"asynchronous|synchronous|online|accredited|transcript|apply|application|"
    r"admission|scholarship|financial aid|program|degree|course)\b",
    re.IGNORECASE,
)


def _classify_stub(turn: Turn) -> TurnClassification:
    """Offline keyword heuristic so the pipeline runs without Gemini. Not production logic."""
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
    """Ask Gemini whether this one turn contains a checkable claim (no history sent)."""
    # TODO: runs on settings.judge_model; generate_structured has no per-step model override.
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

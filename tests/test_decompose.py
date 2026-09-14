"""Step 4 -- the live decomposition call and the claim flattening around it.

No database and no network: Gemini is replaced with a fake, so these tests
check what we send and how we treat what comes back.
"""

from __future__ import annotations

import pytest

from ava_eval.models import ClassifiedTurn, ConversationClaims, ExtractedClaim, Turn, TurnClaims
from ava_eval.steps import s4_decompose
from ava_eval.steps.s4_decompose import _decompose_live, render_transcript, to_claims


def _turns() -> list[Turn]:
    return [
        Turn(session_id="s1", pair_index=0, caller_asked="Business degree?", agent_said="Which one?"),
        Turn(
            session_id="s1",
            pair_index=1,
            caller_asked="General business.",
            agent_said="The Online BBA is fully asynchronous.",
        ),
    ]


def _classifications(flag_pair_1: bool) -> dict[int, ClassifiedTurn]:
    return {
        0: ClassifiedTurn(session_id="s1", pair_index=0, contains_claims=False, reason="filler"),
        1: ClassifiedTurn(session_id="s1", pair_index=1, contains_claims=flag_pair_1, reason="r"),
    }


def test_live_sends_the_rendered_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prompt must carry the whole conversation, in render_transcript's format."""
    seen: dict[str, object] = {}

    def fake_generate(prompt, schema):
        seen["prompt"] = prompt
        seen["schema"] = schema
        return ConversationClaims(turns=[])

    monkeypatch.setattr(s4_decompose, "generate_structured", fake_generate)
    turns, classifications = _turns(), _classifications(flag_pair_1=True)

    _decompose_live("s1", turns, classifications)

    assert render_transcript(turns, classifications) in seen["prompt"]
    assert "Session: s1" in seen["prompt"]
    assert seen["schema"] is ConversationClaims


def test_live_skips_the_call_when_nothing_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """No flagged turns means nothing to decompose, so no quota is spent."""

    def fail_generate(prompt, schema):
        raise AssertionError("Gemini was called for a conversation with no flagged turns")

    monkeypatch.setattr(s4_decompose, "generate_structured", fail_generate)

    result = _decompose_live("s1", _turns(), _classifications(flag_pair_1=False))

    assert result.turns == []


def test_to_claims_drops_a_pair_index_not_in_the_conversation() -> None:
    """A hallucinated turn number must not attach claims to a turn that doesn't exist."""
    response = ConversationClaims(
        turns=[
            TurnClaims(pair_index=1, subject_tag="BBA", claims=[ExtractedClaim(text="Real claim.")]),
            TurnClaims(pair_index=9, subject_tag="BBA", claims=[ExtractedClaim(text="Invented.")]),
        ]
    )

    claims = to_claims("s1", response, valid_indices={0, 1})

    assert [c.pair_index for c in claims] == [1]


def test_to_claims_keeps_ids_unique_when_a_turn_is_repeated() -> None:
    """Restarting at c0 for a repeated turn would collide on the claims primary key."""
    response = ConversationClaims(
        turns=[
            TurnClaims(pair_index=1, subject_tag="BBA", claims=[ExtractedClaim(text="First.")]),
            TurnClaims(pair_index=1, subject_tag="BBA", claims=[ExtractedClaim(text="Second.")]),
        ]
    )

    claims = to_claims("s1", response, valid_indices={0, 1})

    assert [c.claim_id for c in claims] == ["s1__p1__c0", "s1__p1__c1"]

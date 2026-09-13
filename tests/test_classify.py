"""Step 3: does a turn contain ANY checkable factual claim?

The contract is narrow on purpose. ``contains_claims`` is a real boolean and it
answers only "is something checkable *present*", never "is it correct". A reply
with one fact buried in filler is ``true``; a reply that is all greeting,
closing or questions back to the caller is ``false``. A ``false`` turn is still
stored, but step 4 extracts nothing from it.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ava_eval import config
from ava_eval.config import PROJECT_ROOT
from ava_eval.db import store
from ava_eval.models import (
    ClassifiedTurn,
    ConversationStatus,
    KbChunk,
    Turn,
    TurnClassification,
)
from ava_eval.pipeline import evaluate
from ava_eval.steps import s3_classify
from ava_eval.steps.s1_ingest import ingest_directory
from ava_eval.steps.s3_classify import classify_turn, classify_turns
from ava_eval.steps.s4_decompose import decompose_conversation

# (label, agent_said, expected contains_claims)
CONTRACT_CASES = [
    ("greeting", "Hello! How can I help you?", False),
    ("factual claim", "The minimum GPA is 2.0.", True),
    ("fact plus filler", "The minimum GPA is 2.0. Would you like more information?", True),
    ("clarifying question", "Could you repeat your question?", False),
    ("incorrect fact", "The minimum GPA is 9.5.", True),
    ("hedged fact", "I believe the deadline is March 1.", True),
    ("closing", "Have a great day!", False),
]


def _turn(agent_said: str, pair_index: int = 0, caller_asked: str = "q") -> Turn:
    return Turn(
        session_id="s1", pair_index=pair_index, caller_asked=caller_asked, agent_said=agent_said
    )


def _use_live_classifier(monkeypatch: pytest.MonkeyPatch, answer_for) -> list[str]:
    """Switch to the live path with a scripted model. Returns the prompts sent.

    ``answer_for(prompt)`` returns the TurnClassification the fake model gives.
    force_offline (conftest) turns stub mode back on before the next test.
    """
    object.__setattr__(config.settings, "stub_llm", False)
    prompts: list[str] = []

    def fake_generate_structured(prompt, schema, **kwargs):
        assert schema is TurnClassification
        prompts.append(prompt)
        return answer_for(prompt)

    monkeypatch.setattr(s3_classify, "generate_structured", fake_generate_structured)
    return prompts


# ---------------------------------------------------------------------------
# The contract cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, agent_said, expected", CONTRACT_CASES, ids=[c[0] for c in CONTRACT_CASES]
)
def test_offline_classifier_follows_the_contract(
    label: str, agent_said: str, expected: bool
) -> None:
    """The default (stub) mode gives the locked answer for each case, as a bool."""
    result = classify_turn(_turn(agent_said))
    assert result.contains_claims is expected
    assert isinstance(result.reason, str) and result.reason


def test_classify_turns_keeps_order_and_ids() -> None:
    turns = [_turn(text, pair_index=i) for i, (_, text, _) in enumerate(CONTRACT_CASES)]
    results = classify_turns(turns)

    assert [(r.session_id, r.pair_index) for r in results] == [("s1", i) for i in range(len(turns))]
    assert [r.contains_claims for r in results] == [expected for _, _, expected in CONTRACT_CASES]
    assert all(type(r.contains_claims) is bool for r in results)


# ---------------------------------------------------------------------------
# Live path (scripted model, no network)
# ---------------------------------------------------------------------------


def test_live_classifier_sends_one_turn_and_returns_the_models_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _use_live_classifier(
        monkeypatch,
        lambda prompt: TurnClassification(
            contains_claims=True, reason="States a checkable requirement."
        ),
    )

    result = classify_turn(_turn("The minimum GPA is 2.0.", caller_asked="What GPA do I need?"))

    assert result.contains_claims is True
    assert result.reason == "States a checkable requirement."
    assert len(prompts) == 1
    assert "The minimum GPA is 2.0." in prompts[0]
    assert "What GPA do I need?" in prompts[0]
    assert "{{" not in prompts[0]


def test_live_classifier_sends_no_conversation_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each prompt carries only its own turn -- never earlier or later ones."""
    prompts = _use_live_classifier(
        monkeypatch, lambda prompt: TurnClassification(contains_claims=False, reason="r")
    )
    turns = [
        _turn("FIRST-REPLY", pair_index=0, caller_asked="FIRST-QUESTION"),
        _turn("SECOND-REPLY", pair_index=1, caller_asked="SECOND-QUESTION"),
    ]

    classify_turns(turns)

    assert len(prompts) == 2
    assert "FIRST-REPLY" in prompts[0] and "SECOND" not in prompts[0]
    assert "SECOND-REPLY" in prompts[1] and "FIRST" not in prompts[1]


def test_live_classifier_does_not_touch_the_knowledge_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3 decides presence only; retrieval and embeddings belong to steps 2 and 5."""

    def forbidden(*args, **kwargs):
        raise AssertionError("step 3 must not use the knowledge base")

    monkeypatch.setattr(store, "search_chunks", forbidden)
    monkeypatch.setattr("ava_eval.llm.client.embed_texts", forbidden)
    _use_live_classifier(
        monkeypatch, lambda prompt: TurnClassification(contains_claims=True, reason="r")
    )

    assert classify_turns([_turn("The minimum GPA is 9.5.")])[0].contains_claims is True


def test_prompt_states_the_locked_rules() -> None:
    """The rules the model is graded on must be in the prompt it actually receives."""
    from ava_eval.llm import load_prompt

    text = load_prompt("classify_turn", caller_asked="q", agent_said="a")
    assert "**any part**" in text
    assert "not whether it is" in text  # presence, not correctness
    assert "The minimum GPA is 9.5." in text  # wrong fact is still true
    assert "I believe the application deadline is March 1." in text  # hedged fact is still true


# ---------------------------------------------------------------------------
# The boolean contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ['"true"', '"false"', '"yes"', "1", "0", "null"])
def test_non_boolean_contains_claims_from_the_model_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        TurnClassification.model_validate_json(f'{{"contains_claims": {raw}, "reason": "r"}}')


@pytest.mark.parametrize("raw, expected", [("true", True), ("false", False)])
def test_json_boolean_contains_claims_is_accepted(raw: str, expected: bool) -> None:
    parsed = TurnClassification.model_validate_json(f'{{"contains_claims": {raw}, "reason": "r"}}')
    assert parsed.contains_claims is expected


def test_schema_sent_to_the_model_declares_a_boolean() -> None:
    schema = TurnClassification.model_json_schema()
    assert schema["properties"]["contains_claims"]["type"] == "boolean"
    assert set(schema["required"]) == {"contains_claims", "reason"}


# ---------------------------------------------------------------------------
# Step 4 only works on true turns
# ---------------------------------------------------------------------------


def test_step4_extracts_nothing_from_a_turn_marked_false() -> None:
    """Gating is by the stored flag, not by re-reading the text."""
    turns = [
        _turn("The minimum GPA is 2.0 for the program.", pair_index=0),
        _turn("Tuition is 300 dollars per credit hour.", pair_index=1),
    ]
    flags = {
        0: ClassifiedTurn(session_id="s1", pair_index=0, contains_claims=True, reason="r"),
        1: ClassifiedTurn(session_id="s1", pair_index=1, contains_claims=False, reason="r"),
    }

    response = decompose_conversation("s1", turns, flags)

    assert [t.pair_index for t in response.turns] == [0]


# ---------------------------------------------------------------------------
# Through the pipeline, on PostgreSQL
# ---------------------------------------------------------------------------


def _seed_kb(conn: store.Connection) -> None:
    """evaluate() refuses to run against an empty index, so give it one chunk."""
    store.upsert_document(conn, "d", "d.docx", "d.docx", "h", date(2026, 1, 1))
    dim = config.settings.embed_dim
    store.insert_chunks(
        conn,
        [KbChunk(chunk_id="k0", doc_id="d", section="S", ordinal=0, text="policy text")],
        [[1.0] * dim],
        "fake",
    )


def test_every_turn_is_classified_and_stored_as_boolean(db_url: str) -> None:
    turns = [_turn(text, pair_index=i) for i, (_, text, _) in enumerate(CONTRACT_CASES)]
    with store.connect(db_url) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        store.replace_turns(conn, "s1", turns)
        _seed_kb(conn)

    report = evaluate(database_url=db_url)

    expected = {i: flag for i, (_, _, flag) in enumerate(CONTRACT_CASES)}
    result = report.sessions[0]
    assert result.error is None
    assert result.flagged_turns == sum(expected.values())

    with store.connect(db_url) as conn:
        stored = store.get_classifications(conn, "s1")
        column_types = conn.execute(
            "SELECT DISTINCT pg_typeof(contains_claims)::text AS t FROM turn_classifications"
        ).fetchall()
        claim_turns = {c.pair_index for c in store.get_claims(conn, "s1")}
        turn_rows = store.turn_results(conn, "s1")

    # False turns are stored too, not dropped.
    assert {c.pair_index: c.contains_claims for c in stored} == expected
    assert all(type(c.contains_claims) is bool and c.reason for c in stored)
    assert column_types == [{"t": "boolean"}]

    # Only true turns reach step 4.
    assert claim_turns
    assert claim_turns <= {i for i, flag in expected.items() if flag}

    # What the API returns for turns: a real bool, and false turns still listed.
    assert len(turn_rows) == len(CONTRACT_CASES)
    for row in turn_rows:
        assert type(row["contains_claims"]) is bool
        if not expected[row["pair_index"]]:
            assert row["turn_verdict"] == "NOT_EVALUATED"


def test_reclassifying_overwrites_instead_of_duplicating(db_url: str) -> None:
    with store.connect(db_url) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        store.replace_turns(conn, "s1", [_turn("Hello! How can I help you?")])
        store.save_classifications(
            conn,
            [ClassifiedTurn(session_id="s1", pair_index=0, contains_claims=True, reason="first")],
        )
        store.save_classifications(
            conn,
            [ClassifiedTurn(session_id="s1", pair_index=0, contains_claims=False, reason="second")],
        )

    with store.connect(db_url) as conn:
        stored = store.get_classifications(conn, "s1")

    assert [(c.contains_claims, c.reason) for c in stored] == [(False, "second")]


def test_step1_ingest_then_step3_on_sample_transcripts(db_url: str) -> None:
    """Step 1 output feeds step 3 unchanged; rejected conversations are never classified."""
    ingest = ingest_directory(PROJECT_ROOT / "sample_data" / "transcripts", database_url=db_url)
    with store.connect(db_url) as conn:
        _seed_kb(conn)

    report = evaluate(database_url=db_url)

    accepted = {c.session_id: len(c.turns) for c in ingest.accepted}
    assert sorted(s.session_id for s in report.sessions) == sorted(accepted)
    assert all(s.error is None for s in report.sessions)

    with store.connect(db_url) as conn:
        for session_id, turn_count in accepted.items():
            stored = store.get_classifications(conn, session_id)
            assert len(stored) == turn_count
            assert all(type(c.contains_claims) is bool for c in stored)
        for rejected in ingest.rejected:
            assert store.get_classifications(conn, rejected.session_id) == []

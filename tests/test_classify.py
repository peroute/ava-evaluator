"""Step 3: turn classification."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ava_eval import config
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
from ava_eval.steps.s3_classify import classify_turn, classify_turns
from ava_eval.steps.s4_decompose import decompose_conversation

# (label, agent_said, expected contains_claims)
STUB_CASES = [
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


def _use_live_classifier(monkeypatch: pytest.MonkeyPatch, answer: TurnClassification) -> list[str]:
    """Switch to the live path with a fake model that always gives ``answer``.

    Returns the prompts sent. conftest's force_offline restores stub mode for the next test.
    """
    object.__setattr__(config.settings, "stub_llm", False)
    prompts: list[str] = []

    def fake_generate_structured(prompt, schema, **kwargs):
        assert schema is TurnClassification
        prompts.append(prompt)
        return answer

    monkeypatch.setattr(s3_classify, "generate_structured", fake_generate_structured)
    return prompts


@pytest.mark.parametrize("label, agent_said, expected", STUB_CASES, ids=[c[0] for c in STUB_CASES])
def test_stub_classifier(label: str, agent_said: str, expected: bool) -> None:
    result = classify_turn(_turn(agent_said))
    assert result.contains_claims is expected
    assert result.reason


def test_classify_turns_keeps_order_and_ids() -> None:
    turns = [_turn(text, pair_index=i) for i, (_, text, _) in enumerate(STUB_CASES)]
    results = classify_turns(turns)

    assert [(r.session_id, r.pair_index) for r in results] == [("s1", i) for i in range(len(turns))]
    assert [r.contains_claims for r in results] == [expected for _, _, expected in STUB_CASES]


def test_live_classifier_sends_each_turn_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = TurnClassification(contains_claims=True, reason="States a requirement.")
    prompts = _use_live_classifier(monkeypatch, answer)
    turns = [
        _turn("FIRST-REPLY", pair_index=0, caller_asked="FIRST-QUESTION"),
        _turn("SECOND-REPLY", pair_index=1, caller_asked="SECOND-QUESTION"),
    ]

    results = classify_turns(turns)

    assert [(r.contains_claims, r.reason) for r in results] == [(True, "States a requirement.")] * 2
    assert len(prompts) == 2
    for prompt, own, other in zip(prompts, ["FIRST", "SECOND"], ["SECOND", "FIRST"]):
        assert f"{own}-REPLY" in prompt and f"{own}-QUESTION" in prompt
        assert other not in prompt
        assert "{{" not in prompt


@pytest.mark.parametrize("raw", ['"true"', '"false"', '"yes"', "1", "0", "null"])
def test_non_boolean_contains_claims_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        TurnClassification.model_validate_json(f'{{"contains_claims": {raw}, "reason": "r"}}')


def test_schema_sent_to_the_model_declares_a_boolean() -> None:
    schema = TurnClassification.model_json_schema()
    assert schema["properties"]["contains_claims"]["type"] == "boolean"
    assert set(schema["required"]) == {"contains_claims", "reason"}


def test_step4_extracts_nothing_from_a_turn_marked_false() -> None:
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


def _seed_kb(conn: store.Connection) -> None:
    """evaluate() refuses to run against an empty index, so give it one chunk."""
    store.upsert_document(conn, "d", "d.docx", "d.docx", "h", date(2026, 1, 1))
    store.insert_chunks(
        conn,
        [KbChunk(chunk_id="k0", doc_id="d", section="S", ordinal=0, text="policy text")],
        [[1.0] * config.settings.embed_dim],
        "fake",
    )


def test_every_turn_is_classified_and_stored_as_boolean(db_url: str) -> None:
    turns = [_turn(text, pair_index=i) for i, (_, text, _) in enumerate(STUB_CASES)]
    with store.connect(db_url) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        store.replace_turns(conn, "s1", turns)
        _seed_kb(conn)

    report = evaluate(database_url=db_url)

    expected = {i: flag for i, (_, _, flag) in enumerate(STUB_CASES)}
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

    # False turns are stored, not dropped.
    assert {c.pair_index: c.contains_claims for c in stored} == expected
    assert column_types == [{"t": "boolean"}]

    # Only true turns get claims.
    assert claim_turns
    assert claim_turns <= {i for i, flag in expected.items() if flag}

    assert len(turn_rows) == len(STUB_CASES)
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

"""The thin transcript-to-results orchestration service."""

from __future__ import annotations

from contextlib import nullcontext

import pytest

from ava_eval import evaluation_service
from ava_eval.models import Conversation, RejectedConversation, Turn
from ava_eval.pipeline import EvaluationReport, SessionResult
from ava_eval.steps.s1_ingest import IngestReport


def _payload() -> dict:
    return {
        "provider_session_id": "submitted_1",
        "raw_pairs": [{"pair_index": 0, "caller_asked": "Question?", "agent_said": "Answer."}],
    }


def _accepted_report() -> IngestReport:
    return IngestReport(
        accepted=[
            Conversation(
                session_id="submitted_1",
                source_file="api-submission.json",
                turns=[
                    Turn(
                        session_id="submitted_1",
                        pair_index=0,
                        caller_asked="Question?",
                        agent_said="Answer.",
                    )
                ],
            )
        ]
    )


def _install_result_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation_service.store, "connect", lambda _: nullcontext(object()))
    monkeypatch.setattr(
        evaluation_service.store,
        "conversation_summary",
        lambda conn, session_id: [{"session_id": session_id, "contradicted_turns": 1}],
    )
    monkeypatch.setattr(
        evaluation_service.store,
        "turn_results",
        lambda conn, session_id: [
            {
                "pair_index": 0,
                "caller_asked": "Question?",
                "agent_said": "Answer.",
                "contains_claims": True,
                "claim_count": 1,
                "turn_verdict": "CONTRADICTED",
            }
        ],
    )
    monkeypatch.setattr(
        evaluation_service.store,
        "claim_results",
        lambda conn, **kwargs: [
            {
                "claim_id": "submitted_1__p0__c0",
                "pair_index": 0,
                "claim_text": "Claim.",
                "verdict": "CONTRADICTED",
                "tag": None,
                "rationale": "The KB states otherwise.",
                "evidence_text": "Decisive evidence.",
                "evidence_source_doc": "policy.docx",
                "evidence_section": "Requirements",
                "evidence_chunk_id": "chunk-1",
            }
        ],
    )


def test_submit_and_evaluate_returns_frontend_ready_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evaluation_service, "ingest_payload", lambda *args, **kwargs: _accepted_report()
    )
    calls = []

    def fake_evaluate(session_ids, database_url=None):
        calls.append((session_ids, database_url))
        return EvaluationReport(sessions=[SessionResult(session_id="submitted_1")])

    monkeypatch.setattr(evaluation_service.pipeline, "evaluate", fake_evaluate)
    _install_result_readers(monkeypatch)

    result = evaluation_service.submit_and_evaluate(_payload(), database_url="postgresql://test")

    assert calls == [(["submitted_1"], "postgresql://test")]
    assert result["session_id"] == "submitted_1"
    assert result["turns"][0] == {
        "pair_index": 0,
        "caller_asked": "Question?",
        "agent_said": "Answer.",
        "contains_claims": True,
        "claim_count": 1,
        "turn_verdict": "CONTRADICTED",
    }
    assert result["claims"][0]["rationale"] == "The KB states otherwise."
    assert result["claims"][0]["evidence_text"] == "Decisive evidence."


def test_rejected_transcript_stops_before_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = RejectedConversation(
        session_id="bad_1", source_file="api-submission.json", reason="raw_pairs is empty"
    )
    monkeypatch.setattr(
        evaluation_service,
        "ingest_payload",
        lambda *args, **kwargs: IngestReport(rejected=[rejected]),
    )
    called = False

    def fake_evaluate(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(evaluation_service.pipeline, "evaluate", fake_evaluate)

    with pytest.raises(evaluation_service.TranscriptRejectedError) as caught:
        evaluation_service.submit_and_evaluate(_payload())

    assert caught.value.session_id == "bad_1"
    assert called is False


def test_session_error_becomes_controlled_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evaluation_service, "ingest_payload", lambda *args, **kwargs: _accepted_report()
    )
    monkeypatch.setattr(
        evaluation_service.pipeline,
        "evaluate",
        lambda *args, **kwargs: EvaluationReport(
            sessions=[SessionResult(session_id="submitted_1", error="provider failed")]
        ),
    )

    with pytest.raises(evaluation_service.EvaluationFailedError) as caught:
        evaluation_service.submit_and_evaluate(_payload())

    assert caught.value.session_id == "submitted_1"
    assert caught.value.reason == "provider failed"
    assert "provider failed" not in str(caught.value)


def test_empty_kb_becomes_unavailable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evaluation_service, "ingest_payload", lambda *args, **kwargs: _accepted_report()
    )
    monkeypatch.setattr(
        evaluation_service.pipeline,
        "evaluate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("knowledge base index is empty")
        ),
    )

    with pytest.raises(evaluation_service.EvaluationUnavailableError, match="index is empty"):
        evaluation_service.submit_and_evaluate(_payload())

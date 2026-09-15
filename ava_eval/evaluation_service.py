"""Submit one transcript, run the evaluator, and return its stored results."""

from __future__ import annotations

from typing import Any

from . import pipeline
from .db import store
from .steps.s1_ingest import ingest_payload


class TranscriptRejectedError(ValueError):
    """Step 1 rejected a submitted transcript."""

    def __init__(self, session_id: str, reason: str) -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(reason)


class EvaluationUnavailableError(RuntimeError):
    """Evaluation cannot start because a required service or KB is unavailable."""


class EvaluationFailedError(RuntimeError):
    """The pipeline started but failed for the submitted conversation."""

    def __init__(self, session_id: str, reason: str) -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__("Evaluation failed while processing this transcript.")


def submit_and_evaluate(payload: dict[str, Any], database_url: str | None = None) -> dict[str, Any]:
    """Persist one transcript, run Steps 3-6, then return existing result views."""
    try:
        ingest_report = ingest_payload(payload, database_url=database_url)
    except RuntimeError as exc:
        raise EvaluationUnavailableError(str(exc)) from exc

    if ingest_report.rejected:
        rejected = ingest_report.rejected[0]
        raise TranscriptRejectedError(rejected.session_id, rejected.reason)

    if not ingest_report.accepted:
        raise EvaluationFailedError("api-submission.json#0", "Step 1 produced no result")

    session_id = ingest_report.accepted[0].session_id

    try:
        evaluation = pipeline.evaluate([session_id], database_url=database_url)
    except RuntimeError as exc:
        raise EvaluationUnavailableError(str(exc)) from exc

    session_result = next(
        (result for result in evaluation.sessions if result.session_id == session_id), None
    )
    if session_result is None:
        raise EvaluationFailedError(session_id, "Pipeline returned no session result")
    if session_result.error:
        raise EvaluationFailedError(session_id, session_result.error)

    try:
        with store.connect(database_url) as conn:
            summaries = store.conversation_summary(conn, session_id)
            if not summaries:
                raise EvaluationFailedError(session_id, "Stored conversation result was not found")
            return {
                "session_id": session_id,
                "summary": summaries[0],
                "turns": store.turn_results(conn, session_id),
                "claims": store.claim_results(conn, session_id=session_id, limit=1000),
            }
    except EvaluationFailedError:
        raise
    except RuntimeError as exc:
        raise EvaluationUnavailableError(str(exc)) from exc

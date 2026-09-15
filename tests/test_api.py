"""HTTP contract for transcript submission and existing result browsing."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext

import pytest
from fastapi import HTTPException

from ava_eval import evaluation_service
from ava_eval.api import main as api
from ava_eval.api.main import app, create_evaluation
from ava_eval.db import store
from ava_eval.models import Claim, ClaimVerdict, ConversationStatus, Turn, Verdict


def _request(method: str, path: str, body: bytes = b"") -> tuple[int, bytes]:
    headers = [(b"host", b"testserver")]
    if body:
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return start["status"], response_body


def _payload() -> dict:
    return {
        "provider_session_id": "submitted_1",
        "raw_pairs": [{"pair_index": 0, "caller_asked": "Question?", "agent_said": "Answer."}],
    }


def test_post_evaluations_returns_service_result(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"session_id": "submitted_1", "summary": {}, "turns": [], "claims": []}
    monkeypatch.setattr(evaluation_service, "submit_and_evaluate", lambda payload: expected)

    assert create_evaluation(_payload()) == expected


def test_malformed_json_uses_fastapi_validation() -> None:
    status, body = _request("POST", "/evaluations", b"{not-json")

    assert status == 422
    assert json.loads(body)["detail"]


def test_rejected_transcript_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(payload):
        raise evaluation_service.TranscriptRejectedError("bad_1", "raw_pairs is empty")

    monkeypatch.setattr(evaluation_service, "submit_and_evaluate", reject)

    with pytest.raises(HTTPException) as caught:
        create_evaluation(_payload())

    assert caught.value.status_code == 422
    assert caught.value.detail == {
        "session_id": "bad_1",
        "error": "raw_pairs is empty",
    }


def test_empty_kb_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(payload):
        raise evaluation_service.EvaluationUnavailableError("knowledge base index is empty")

    monkeypatch.setattr(evaluation_service, "submit_and_evaluate", unavailable)

    with pytest.raises(HTTPException) as caught:
        create_evaluation(_payload())

    assert caught.value.status_code == 503
    assert "index is empty" in caught.value.detail


def test_pipeline_session_error_returns_safe_500(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(payload):
        raise evaluation_service.EvaluationFailedError("submitted_1", "secret provider detail")

    monkeypatch.setattr(evaluation_service, "submit_and_evaluate", fail)

    with pytest.raises(HTTPException) as caught:
        create_evaluation(_payload())

    assert caught.value.status_code == 500
    assert caught.value.detail["session_id"] == "submitted_1"
    assert caught.value.detail["error"] == "Evaluation failed while processing this transcript."
    assert "secret provider detail" not in str(caught.value.detail)


def test_existing_get_routes_are_unchanged() -> None:
    paths = app.openapi()["paths"]

    assert "get" in paths["/health"]
    assert "get" in paths["/conversations"]
    assert "get" in paths["/conversations/{session_id}"]
    assert "get" in paths["/claims"]
    assert "get" in paths["/claims/{claim_id}"]
    assert set(paths["/evaluations"]) == {"post"}


def test_existing_conversation_detail_still_uses_result_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api.store, "connect", lambda: nullcontext(object()))
    monkeypatch.setattr(
        api.store,
        "conversation_summary",
        lambda conn, session_id: [{"session_id": session_id}],
    )
    monkeypatch.setattr(api.store, "turn_results", lambda conn, session_id: [{"pair_index": 0}])
    monkeypatch.setattr(
        api.store,
        "claim_results",
        lambda conn, **kwargs: [{"claim_id": "claim-1"}],
    )

    assert api.get_conversation("existing_1") == {
        "summary": {"session_id": "existing_1"},
        "turns": [{"pair_index": 0}],
        "claims": [{"claim_id": "claim-1"}],
    }


def test_conversation_detail_reports_unjudged_claims(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UI marks claims and turns "Not judged" from exactly these fields.

    One turn, two claims, only one verdict saved -- the shape step 6 leaves when a
    judge call fails. The turn still reads SUPPORTED (its worst real verdict), so
    ``unjudged_count`` is the only thing telling the UI the turn is incomplete.
    """
    with store.connect(db_url) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
        store.replace_turns(
            conn, "s1", [Turn(session_id="s1", pair_index=0, caller_asked="q", agent_said="a")]
        )
        store.replace_claims_for_session(
            conn,
            "s1",
            [
                Claim(claim_id=cid, session_id="s1", pair_index=0, subject_tag="S", text="t")
                for cid in ("c0", "c1")
            ],
        )
        store.save_verdict(conn, ClaimVerdict(claim_id="c0", verdict=Verdict.SUPPORTED))

    real_connect = store.connect
    monkeypatch.setattr(api.store, "connect", lambda: real_connect(db_url))

    status, body = _request("GET", "/conversations/s1")

    assert status == 200
    data = json.loads(body)
    assert data["summary"]["unjudged_turns"] == 1
    assert data["turns"][0]["turn_verdict"] == "SUPPORTED"
    assert data["turns"][0]["unjudged_count"] == 1
    claims = {claim["claim_id"]: claim for claim in data["claims"]}
    assert claims["c0"]["verdict"] == "SUPPORTED"
    assert claims["c1"]["verdict"] is None
    assert claims["c1"]["kb_changed_since_judged"] is None

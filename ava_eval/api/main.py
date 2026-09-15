"""Read-only HTTP layer for the TTU staff frontend.

A browser cannot talk to PostgreSQL directly (and must never hold database
credentials), so results need HTTP regardless of what the frontend ends up
being built with. Keeping that layer thin is the point: the
filtering and the rollups are already SQL views (``db/migrations``), so these handlers
mostly translate query parameters into arguments and hand back rows. If the
frontend ever needs to reimplement the severity rollup in JavaScript, something
has gone wrong here.

Every endpoint is a read. Nothing in this API can change a verdict — evaluation
happens through the CLI, and results are evidence, not an editable document.

A minimal read-only dashboard (plain HTML/JS in ``api/static``) is served at
``/ui`` from this same app, so it needs no build step and no extra container.
It only calls the GET endpoints below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..config import settings
from ..db import store

STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(
    title="Ava Evaluator",
    description="Faithfulness verdicts for Ava's answers, grounded in TTU's knowledge base.",
    version="0.1.0",
)

# Wide open for local frontend development. Before this is exposed anywhere
# real, restrict allow_origins to the frontend's actual origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness check, plus whether this instance is running on stubbed data.

    The stub flags are surfaced deliberately: a frontend showing verdicts
    produced by the offline stub must be able to say so, or someone will act on
    a keyword heuristic believing it was a language model.
    """
    with store.connect() as conn:
        chunk_count = store.count_chunks(conn)
    return {
        "status": "ok",
        "kb_chunks": chunk_count,
        "stub_llm": settings.stub_llm,
        "stub_embed": settings.stub_embed,
        "judge_model": settings.judge_model,
    }


@app.get("/stats")
def stats() -> dict[str, Any]:
    """Totals and counts by verdict and tag, for a dashboard header."""
    with store.connect() as conn:
        return store.verdict_stats(conn)


@app.get("/conversations")
def list_conversations() -> list[dict[str, Any]]:
    """Every conversation with its counts of flagged vs supported turns."""
    with store.connect() as conn:
        return store.conversation_summary(conn)


@app.get("/conversations/{session_id}")
def get_conversation(session_id: str) -> dict[str, Any]:
    """One conversation: its summary plus every turn with its rolled-up verdict."""
    with store.connect() as conn:
        summary = store.conversation_summary(conn, session_id)
        if not summary:
            raise HTTPException(status_code=404, detail=f"No conversation {session_id!r}")
        return {
            "summary": summary[0],
            "turns": store.turn_results(conn, session_id),
            "claims": store.claim_results(conn, session_id=session_id, limit=1000),
        }


@app.get("/claims")
def list_claims(
    verdict: str | None = Query(None, description="SUPPORTED | CONTRADICTED | NOT_FOUND"),
    tag: str | None = Query(None, description="kb_gap | retrieval_miss"),
    subject: str | None = Query(None, description="exact subject_tag match"),
    session: str | None = Query(None, description="restrict to one session id"),
    unjudged: bool = Query(False, description="only claims step 6 could not judge"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    """Filterable claim-level results.

    This is the endpoint the frontend lives on. CONTRADICTED and NOT_FOUND are
    separate filter values rather than one "flagged" flag, because they send
    work to different people: a contradiction is an Ava behaviour fix, a gap is
    usually a TTU content fix.
    """
    with store.connect() as conn:
        return store.claim_results(
            conn,
            session_id=session,
            verdict=verdict,
            tag=tag,
            subject_tag=subject,
            limit=limit,
            offset=offset,
            unjudged=unjudged,
        )


@app.get("/claims/{claim_id}")
def get_claim(claim_id: str) -> dict[str, Any]:
    """One claim in full, including the retrieved evidence behind its verdict.

    The retrieval history is included per attempt, so a reviewer can see not
    only what the judge decided but what it was looking at — and, for a retried
    claim, what the wider second search turned up.
    """
    with store.connect() as conn:
        claim = store.get_claim_result(conn, claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail=f"No claim {claim_id!r}")
        return {
            "claim": claim,
            "retrieved": {
                "attempt_1": store.get_retrievals(conn, claim_id, attempt=1),
                "attempt_2": store.get_retrievals(conn, claim_id, attempt=2),
            },
        }


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Send a browser opening the bare API address to the dashboard."""
    return RedirectResponse(url="/ui/")


# Mounted last so it can never shadow an API route. StaticFiles only answers
# GET and HEAD, so the dashboard adds no way to write anything.
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

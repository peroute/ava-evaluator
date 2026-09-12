"""Read-only FastAPI backend for inspecting Step 1 results.

Local development only: no auth, no writes, no pipeline logic. Start it with
`python run_dashboard.py` and open http://127.0.0.1:8000/.
"""

import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from web import queries

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def create_app(db_path=None):
    """Build the app. Tests pass their own db_path; the real run uses the default."""
    resolved_db_path = queries.resolve_db_path(db_path)
    app = FastAPI(title="Step 1 Inspector", version="1.0.0")

    def read(query_function, *args):
        """Open a read-only connection, run one query, always close it."""
        try:
            connection = queries.connect_read_only(resolved_db_path)
        except queries.DatabaseMissing as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        try:
            return query_function(connection, *args)
        finally:
            connection.close()

    @app.get("/api/summary")
    def summary():
        return read(queries.get_summary)

    @app.get("/api/conversations")
    def conversations():
        return {"conversations": read(queries.list_conversations)}

    @app.get("/api/conversations/{session_id}")
    def conversation(session_id: str):
        turns = read(queries.get_conversation_turns, session_id)
        if not turns:
            raise HTTPException(status_code=404, detail="unknown session_id: %s" % session_id)
        return {"session_id": session_id, "turn_count": len(turns), "turns": turns}

    @app.get("/api/quarantine")
    def quarantine():
        return {"records": read(queries.list_quarantine)}

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()

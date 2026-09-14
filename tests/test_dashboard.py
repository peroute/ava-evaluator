"""The read-only dashboard served at /ui.

These tests call the FastAPI app directly over ASGI rather than through
``fastapi.testclient``: they need no database, no network and no extra test
dependency. They check that the static files are served, and -- the part that
matters -- that adding the dashboard gave the API no way to write anything.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.routing import APIRoute

from ava_eval.api.main import STATIC_DIR, app


def _request(method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    """Send one HTTP request to the app and return (status, headers, body)."""
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
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))

    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], headers, body


def test_static_files_exist() -> None:
    for name in ("index.html", "app.js", "styles.css"):
        assert (STATIC_DIR / name).is_file()


@pytest.mark.parametrize(
    "path, content_type",
    [
        ("/ui/", "text/html"),
        ("/ui/index.html", "text/html"),
        ("/ui/app.js", "javascript"),
        ("/ui/styles.css", "text/css"),
    ],
)
def test_dashboard_files_are_served(path: str, content_type: str) -> None:
    status, headers, body = _request("GET", path)
    assert status == 200
    assert content_type in headers["content-type"]
    assert body


def test_index_loads_its_assets() -> None:
    _, _, body = _request("GET", "/ui/")
    html = body.decode()
    assert "<title>Ava Evaluator</title>" in html
    assert 'src="app.js"' in html and 'href="styles.css"' in html


def test_root_redirects_to_dashboard() -> None:
    status, headers, _ = _request("GET", "/")
    assert status in (302, 307)
    assert headers["location"] == "/ui/"


def test_unknown_static_file_is_404() -> None:
    status, _, _ = _request("GET", "/ui/does-not-exist.js")
    assert status == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/ui/", "/ui/index.html", "/ui/app.js"])
def test_dashboard_refuses_writes(method: str, path: str) -> None:
    status, _, _ = _request(method, path)
    assert status == 405


def test_api_routes_are_still_read_only() -> None:
    methods = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            methods |= route.methods
    assert methods <= {"GET", "HEAD"}


def test_dashboard_is_not_part_of_the_api_schema() -> None:
    """The redirect and the static mount must not show up as API endpoints."""
    paths = set(app.openapi()["paths"])
    assert paths == {
        "/health",
        "/stats",
        "/conversations",
        "/conversations/{session_id}",
        "/claims",
        "/claims/{claim_id}",
    }


def test_dashboard_only_calls_existing_get_endpoints() -> None:
    """The script must not reach for endpoints or methods the API does not have."""
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'getJson("/health")' in script
    assert 'getJson("/conversations")' in script
    assert "getJson(`/conversations/${encodeURIComponent(sessionId)}`)" in script
    for forbidden in ("method:", "POST", "PUT", "DELETE", "PATCH", "insertAdjacentHTML"):
        assert forbidden not in script
    # Transcript text is untrusted: it is only ever written with textContent.
    assert not re.search(r"\.(inner|outer)HTML\s*=", script)

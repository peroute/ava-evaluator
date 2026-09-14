"""Static frontend and container wiring checks without a JavaScript test framework."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def test_frontend_container_files_exist() -> None:
    for name in ("Dockerfile", "nginx.conf", "index.html", "app.js", "styles.css"):
        assert (FRONTEND / name).is_file()


def test_nginx_proxies_api_prefix_to_backend_service() -> None:
    config = (FRONTEND / "nginx.conf").read_text(encoding="utf-8")

    assert "location /api/" in config
    assert "proxy_pass http://api:8000/;" in config


def test_compose_defines_a_separate_frontend_service() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "  frontend:" in compose
    assert "context: ./frontend" in compose
    assert '"127.0.0.1:8080:80"' in compose
    assert "  api:" in compose


def test_submission_controls_and_loading_state_exist() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert 'id="transcript-file"' in html
    assert 'type="file"' in html
    assert 'id="transcript-json"' in html
    assert 'id="evaluate-button"' in html
    assert 'id="loading-state"' in html


def test_frontend_uses_relative_api_routes_and_safe_dom() -> None:
    script = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert 'fetch("/api/evaluations"' in script
    assert 'fetch("/api/health"' in script
    assert "http://localhost" not in script
    assert "http://127.0.0.1" not in script
    for unsafe_api in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert unsafe_api not in script
    assert "textContent" in script


def test_claims_are_grouped_by_pair_index() -> None:
    script = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "claimsByTurn" in script
    assert "claim.pair_index" in script
    assert "turn.pair_index" in script

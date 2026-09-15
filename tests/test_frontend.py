"""Static frontend and container wiring checks.

Rendering logic is tested behaviourally in ``frontend/lib.test.js`` (``node --test``);
these checks pin the wiring Python can see: files, container, routes, safe DOM use.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_frontend_container_files_exist() -> None:
    for name in ("Dockerfile", "nginx.conf", "index.html", "app.js", "lib.js", "styles.css"):
        assert (FRONTEND / name).is_file()


def test_dockerfile_ships_the_logic_module_but_not_tests() -> None:
    dockerfile = _read("Dockerfile")

    assert "lib.js" in dockerfile
    assert "lib.test.js" not in dockerfile
    assert "package.json" not in dockerfile


def test_nginx_proxies_api_prefix_to_backend_service() -> None:
    config = _read("nginx.conf")

    assert "location /api/" in config
    assert "proxy_pass http://api:8000/;" in config


def test_compose_defines_a_separate_frontend_service() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "  frontend:" in compose
    assert "context: ./frontend" in compose
    assert '"127.0.0.1:8080:80"' in compose
    assert "  api:" in compose


def test_page_has_submit_browse_and_result_controls() -> None:
    html = _read("index.html")

    for element_id in (
        "transcript-file",
        "transcript-json",
        "evaluate-button",
        "loading-state",
        "list-view",
        "conversation-rows",
        "results",
        "turn-order",
        "turn-filter",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert 'type="file"' in html


def test_app_loads_as_a_module_importing_the_logic() -> None:
    assert '<script type="module" src="app.js">' in _read("index.html")
    assert 'from "./lib.js"' in _read("app.js")


def test_frontend_uses_relative_api_routes_and_safe_dom() -> None:
    script = _read("app.js")

    assert 'fetch("/api/evaluations"' in script
    assert 'fetch("/api/health"' in script
    assert 'fetch("/api/conversations"' in script
    assert "textContent" in script
    for source in (script, _read("lib.js")):
        assert "http://localhost" not in source
        assert "http://127.0.0.1" not in source
        for unsafe_api in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            assert unsafe_api not in source


def test_frontend_tests_need_no_dependencies() -> None:
    package = json.loads(_read("package.json"))

    assert package["type"] == "module"
    assert "dependencies" not in package
    assert "devDependencies" not in package

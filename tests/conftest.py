"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from ava_eval import config
from ava_eval.db import store


@pytest.fixture(autouse=True)
def force_offline() -> None:
    """Guarantee no test ever reaches the network.

    Without this, the suite would inherit whatever is in the developer's .env —
    so a colleague with live credentials configured would have ``pytest`` start
    spending their quota, and CI would fail for lack of a key.

    Every module does ``from ..config import settings``, so they all share one
    ``Settings`` instance. Mutating that instance therefore reaches all of them.
    ``object.__setattr__`` is needed only because the dataclass is frozen, which
    is the right default for production and simply inconvenient here.
    """
    object.__setattr__(config.settings, "stub_llm", True)
    object.__setattr__(config.settings, "stub_embed", True)
    object.__setattr__(config.settings, "gemini_api_key", None)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A freshly created database, isolated per test."""
    path = tmp_path / "test.db"
    store.init_db(path)
    return path

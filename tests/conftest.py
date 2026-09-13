"""Shared test fixtures.

Database tests run against a real PostgreSQL database -- the ``ava_eval_test``
database inside the docker compose Postgres -- rather than a fake. The SQL is
the thing under test (views, constraints, pgvector search), so a stand-in
database would test the stand-in.

Isolation is by TRUNCATE before each test rather than a fresh database per test:
the schema is migrated once per session, and emptying eight tables takes
milliseconds.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.exc import OperationalError

from ava_eval import config
from ava_eval.db.migrate import upgrade_to_head

# Every table the pipeline writes. CASCADE follows the foreign keys anyway, but
# naming them all keeps the reset independent of how the keys are wired.
_TABLES = (
    "conversations",
    "turns",
    "kb_documents",
    "kb_chunks",
    "turn_classifications",
    "claims",
    "claim_retrievals",
    "verdicts",
)


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


@pytest.fixture(scope="session")
def migrated_test_db() -> str:
    """Migrate the test database once per test session and return its URL.

    Refuses to run against any database whose name does not end in ``_test``:
    the per-test fixture empties every table, and pointing it at the dev
    database by mistake would silently delete real pipeline results.
    """
    url = config.settings.test_database_url
    dbname = conninfo_to_dict(url).get("dbname", "")
    if not dbname.endswith("_test"):
        pytest.exit(
            f"Refusing to run tests against database {dbname!r}: the test database name "
            f"must end in '_test'. Check AVA_TEST_DATABASE_URL.",
            returncode=2,
        )

    try:
        upgrade_to_head(url)
    except OperationalError as exc:
        pytest.exit(
            f"Could not connect to the test database ({dbname}). Is Postgres running "
            f"('docker compose up -d db')? If the error below says 'password authentication "
            f"failed', a different PostgreSQL is answering on that port -- check "
            f"AVA_TEST_DATABASE_URL points at the compose port (5433).\n{exc.orig}",
            returncode=2,
        )
    return url


@pytest.fixture
def db_url(migrated_test_db: str) -> str:
    """An empty, fully migrated database for one test. Returns its URL."""
    with psycopg.connect(migrated_test_db) as conn:
        conn.execute(f"TRUNCATE {', '.join(_TABLES)} CASCADE")
    return migrated_test_db

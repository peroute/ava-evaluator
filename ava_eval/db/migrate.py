"""Run Alembic migrations from Python.

The CLI's ``migrate`` command and the test suite both need "bring this database
to the latest schema". Doing it through Alembic's Python API rather than
shelling out to the ``alembic`` executable means it works from any working
directory and inside the container without PATH assumptions.
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from ..config import PROJECT_ROOT, settings

ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def _alembic_config(database_url: str | None) -> Config:
    """Build an Alembic config pointing at ``database_url`` (default: configured URL)."""
    if not ALEMBIC_INI.exists():
        raise FileNotFoundError(f"Alembic config not found at {ALEMBIC_INI}")
    cfg = Config(str(ALEMBIC_INI))
    # Read by migrations/env.py; takes precedence over settings.database_url.
    cfg.attributes["database_url"] = database_url or settings.database_url
    return cfg


def upgrade_to_head(database_url: str | None = None) -> None:
    """Apply every migration the database has not seen yet. Safe to run repeatedly."""
    command.upgrade(_alembic_config(database_url), "head")


def downgrade_to(revision: str, database_url: str | None = None) -> None:
    """Roll the schema back to ``revision`` ("base" removes everything)."""
    command.downgrade(_alembic_config(database_url), revision)

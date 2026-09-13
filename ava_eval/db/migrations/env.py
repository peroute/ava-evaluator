"""Alembic's entry point: how to connect, then run the migration scripts.

Two deliberate choices here:

* The URL comes from ``ava_eval.config`` rather than ``alembic.ini``, so there is
  one place credentials are configured. A caller can override it through
  ``config.attributes["database_url"]`` -- that is how the test suite migrates
  the test database without touching the dev one.
* ``target_metadata`` is None. Migrations are hand-written SQL, not generated
  from SQLAlchemy models, because the app itself never uses SQLAlchemy. That
  means ``alembic revision --autogenerate`` has nothing to compare against;
  write each migration by hand.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from ava_eval.config import settings

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: otherwise running migrations from inside
    # the CLI or pytest would silently mute every logger configured before it.
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _sqlalchemy_url(url: str) -> str:
    """Point SQLAlchemy at the psycopg 3 driver.

    A plain ``postgresql://`` URL makes SQLAlchemy look for psycopg2, which is
    not installed. Hosted providers also hand out ``postgres://``, which
    SQLAlchemy rejects outright, so both spellings are normalised.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def _database_url() -> str:
    """The URL to migrate: an explicit override, else the configured one."""
    return config.attributes.get("database_url") or settings.database_url


def run_migrations_offline() -> None:
    """Emit the migration SQL to stdout instead of executing it (``--sql``)."""
    context.configure(
        url=_sqlalchemy_url(_database_url()),
        target_metadata=None,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations inside one transaction.

    PostgreSQL supports transactional DDL, so a migration that fails halfway
    rolls back completely instead of leaving a half-built schema.
    """
    engine = create_engine(_sqlalchemy_url(_database_url()), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""Show timestamps in Lubbock time by default.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-12

No stored data changes. TIMESTAMPTZ columns hold an absolute instant, not a
wall-clock reading with a zone attached; the zone only decides how PostgreSQL
*displays* that instant. This sets the default display zone for any client that
does not pick its own -- pgAdmin, psql -- so they read in Lubbock time, not UTC.

* America/Chicago rather than a fixed UTC-5: it follows daylight saving time
  (-05:00 CDT, -06:00 CST), so a displayed time always matches a Lubbock clock.
* The app's own connections set the zone from AVA_DISPLAY_TIMEZONE in
  ``store.connect()``. The value here is deliberately a literal: a migration
  must do the same thing every time it runs, so it cannot read configuration.
  To change the default later, add a new migration.
* ALTER DATABASE ... SET applies to NEW connections only. Reconnect pgAdmin.
* ``current_database()`` makes the same migration work on ava_eval,
  ava_eval_test and a hosted database. Requires owning the database, which the
  compose user and a hosted provider's main user both do.
* Built with ``||`` rather than ``format('%I')``: Alembic hands this string to
  SQLAlchemy, which treats ``%`` and ``:name`` specially.
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UPGRADE: list[str] = [
    """
    DO $$
    BEGIN
        EXECUTE 'ALTER DATABASE ' || quote_ident(current_database())
             || ' SET timezone = ' || quote_literal('America/Chicago');
    END
    $$
    """,
]

_DOWNGRADE: list[str] = [
    """
    DO $$
    BEGIN
        EXECUTE 'ALTER DATABASE ' || quote_ident(current_database()) || ' RESET timezone';
    END
    $$
    """,
]


def upgrade() -> None:
    """Make America/Chicago the database's default display time zone."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Restore the server's default display time zone."""
    for statement in _DOWNGRADE:
        op.execute(statement)

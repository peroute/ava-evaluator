"""Record which transcript source produced each conversation.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-13

Step 1 reads transcripts through a ``TranscriptSource`` (today the JSON export;
later, for example, a provider API). ``source_type`` stores that source's name
so a conversation can be traced back to where it came from.

* The default ``'json_export'`` backfills every existing row, which is correct:
  before this migration exported JSON was the only source.
* No view changes. ``v_conversation_summary`` lists its columns explicitly, so
  it neither needs nor exposes the new one.
* The provider's full raw payload is deliberately NOT stored.
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UPGRADE: list[str] = [
    """
    ALTER TABLE conversations
        ADD COLUMN source_type TEXT NOT NULL DEFAULT 'json_export'
    """,
]

_DOWNGRADE: list[str] = [
    "ALTER TABLE conversations DROP COLUMN source_type",
]


def upgrade() -> None:
    """Add conversations.source_type, backfilling existing rows as JSON exports."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop conversations.source_type."""
    for statement in _DOWNGRADE:
        op.execute(statement)

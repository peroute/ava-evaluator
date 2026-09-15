"""Migration 0004 (unjudged claims) must undo and redo cleanly.

Its downgrade rebuilds three views by hand; a typo there would only surface the
day someone actually needs to roll back.
"""

from __future__ import annotations

from ava_eval.db import store
from ava_eval.db.migrate import downgrade_to, upgrade_to_head


def _view_columns(db_url: str) -> set[str]:
    with store.connect(db_url) as conn:
        return {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name IN ('v_turn_severity', 'v_turn_results', 'v_conversation_summary')"
            ).fetchall()
        }


def test_migration_0004_downgrades_and_upgrades_cleanly(db_url: str) -> None:
    downgrade_to("0003", db_url)
    try:
        columns = _view_columns(db_url)
        assert "unjudged_count" not in columns
        assert "unjudged_turns" not in columns
        assert "turn_verdict" in columns, "downgrade dropped the views without rebuilding them"
    finally:
        upgrade_to_head(db_url)

    columns = _view_columns(db_url)
    assert {"unjudged_count", "unjudged_turns"} <= columns

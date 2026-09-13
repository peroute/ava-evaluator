"""Display time zone.

Timestamps are stored as instants and shown in Lubbock time. Both ways this can
break are silent: the zone not being applied (everything quietly reads as UTC
again), and -- far worse -- the conversion shifting the stored instant itself,
which would move every audit timestamp by five or six hours.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import psycopg
import pytest

from ava_eval import config
from ava_eval.db import store
from ava_eval.models import ConversationStatus


def test_app_connections_use_the_configured_zone(db_url: str) -> None:
    with store.connect(db_url) as conn:
        row = conn.execute("SELECT current_setting('TimeZone') AS tz").fetchone()
    assert row["tz"] == config.settings.display_timezone


def test_database_default_applies_to_other_clients(db_url: str) -> None:
    """What pgAdmin and psql see: a plain connection that sets no zone itself."""
    with psycopg.connect(db_url) as conn:
        tz = conn.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    assert tz == "America/Chicago"


def test_stored_instant_is_unchanged_and_returned_in_local_time(db_url: str) -> None:
    before = store.utcnow()
    with store.connect(db_url) as conn:
        store.upsert_conversation(conn, "s1", "f.json", ConversationStatus.ACCEPTED)
    after = store.utcnow()

    with store.connect(db_url) as conn:
        ingested = conn.execute(
            "SELECT ingested_at FROM conversations WHERE session_id = 's1'"
        ).fetchone()["ingested_at"]

    # Same moment in time: aware datetimes compare by instant, whatever their zone.
    assert before <= ingested <= after
    # Carries the local offset. If psycopg could not resolve the zone it falls
    # back to UTC, and this is the assertion that notices.
    expected = ingested.astimezone(ZoneInfo(config.settings.display_timezone)).utcoffset()
    assert ingested.utcoffset() == expected


def test_unknown_zone_explains_itself(db_url: str) -> None:
    original = config.settings.display_timezone
    # The dataclass is frozen; see force_offline in conftest.py.
    object.__setattr__(config.settings, "display_timezone", "Mars/Olympus_Mons")
    try:
        with pytest.raises(RuntimeError, match="AVA_DISPLAY_TIMEZONE"):
            with store.connect(db_url):
                pass
    finally:
        object.__setattr__(config.settings, "display_timezone", original)

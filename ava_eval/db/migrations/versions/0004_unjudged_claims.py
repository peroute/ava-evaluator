"""Count claims that step 6 could not judge, instead of hiding them.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-14

Step 6 now skips a claim whose judge call fails and saves the rest of the
conversation. Such a claim has a ``claims`` row but no ``verdicts`` row, and the
0001 rollups used an inner ``JOIN verdicts``, so it simply vanished: a turn whose
only claim failed read ``NO_CLAIMS`` ("nothing checkable"), which is false.

* ``v_turn_severity`` switches to ``LEFT JOIN verdicts`` and gains
  ``unjudged_count``. ``claim_count`` now counts every claim, judged or not;
  ``severity`` is 0 for a turn with no judged claims.
* ``v_turn_results`` gains ``unjudged_count`` and a new ``UNJUDGED`` label,
  ranked below real verdicts and above the "nothing to judge" labels. A turn
  with some verdicts and some failures shows its worst verdict, and
  ``unjudged_count > 0`` says it is incomplete.
* ``v_conversation_summary`` gains ``unjudged_turns``.

CREATE OR REPLACE VIEW may only append columns, so every new column goes at the
end. UNJUDGED is a turn label, not a claim verdict, so ``SEVERITY`` in
``models.py`` is unchanged.

Each statement is its own ``op.execute`` call, as in 0001.
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UPGRADE: list[str] = [
    """
    CREATE OR REPLACE VIEW v_turn_severity AS
    SELECT
        cl.session_id,
        cl.pair_index,
        COUNT(*) AS claim_count,
        SUM(CASE WHEN v.verdict = 'CONTRADICTED' THEN 1 ELSE 0 END) AS contradicted_count,
        SUM(CASE WHEN v.verdict = 'NOT_FOUND'    THEN 1 ELSE 0 END) AS not_found_count,
        SUM(CASE WHEN v.verdict = 'SUPPORTED'    THEN 1 ELSE 0 END) AS supported_count,
        MAX(CASE v.verdict
                WHEN 'CONTRADICTED' THEN 3
                WHEN 'NOT_FOUND'    THEN 2
                WHEN 'SUPPORTED'    THEN 1
                ELSE 0
            END) AS severity,
        SUM(CASE WHEN v.claim_id IS NULL THEN 1 ELSE 0 END) AS unjudged_count
    FROM claims cl
    LEFT JOIN verdicts v ON v.claim_id = cl.claim_id
    GROUP BY cl.session_id, cl.pair_index
    """,
    """
    CREATE OR REPLACE VIEW v_turn_results AS
    SELECT
        t.session_id,
        t.pair_index,
        t.caller_asked,
        t.agent_said,
        COALESCE(tc.contains_claims, FALSE) AS contains_claims,
        COALESCE(s.claim_count, 0)          AS claim_count,
        COALESCE(s.contradicted_count, 0)   AS contradicted_count,
        COALESCE(s.not_found_count, 0)      AS not_found_count,
        COALESCE(s.supported_count, 0)      AS supported_count,
        CASE
            WHEN s.severity = 3 THEN 'CONTRADICTED'
            WHEN s.severity = 2 THEN 'NOT_FOUND'
            WHEN s.severity = 1 THEN 'SUPPORTED'
            WHEN COALESCE(s.unjudged_count, 0) > 0 THEN 'UNJUDGED'
            WHEN tc.session_id IS NULL THEN 'UNCLASSIFIED'
            WHEN NOT tc.contains_claims THEN 'NOT_EVALUATED'
            ELSE 'NO_CLAIMS'
        END AS turn_verdict,
        COALESCE(s.unjudged_count, 0)       AS unjudged_count
    FROM turns t
    LEFT JOIN turn_classifications tc
           ON tc.session_id = t.session_id AND tc.pair_index = t.pair_index
    LEFT JOIN v_turn_severity s
           ON s.session_id = t.session_id AND s.pair_index = t.pair_index
    """,
    """
    CREATE OR REPLACE VIEW v_conversation_summary AS
    SELECT
        c.session_id,
        c.source_file,
        c.status,
        c.note,
        c.ingested_at,
        COUNT(tr.pair_index) AS turn_count,
        SUM(CASE WHEN tr.turn_verdict = 'CONTRADICTED' THEN 1 ELSE 0 END) AS contradicted_turns,
        SUM(CASE WHEN tr.turn_verdict = 'NOT_FOUND'    THEN 1 ELSE 0 END) AS not_found_turns,
        SUM(CASE WHEN tr.turn_verdict = 'SUPPORTED'    THEN 1 ELSE 0 END) AS supported_turns,
        SUM(CASE WHEN tr.turn_verdict IN ('NOT_EVALUATED', 'NO_CLAIMS', 'UNCLASSIFIED')
                 THEN 1 ELSE 0 END) AS skipped_turns,
        SUM(CASE WHEN tr.unjudged_count > 0 THEN 1 ELSE 0 END) AS unjudged_turns
    FROM conversations c
    LEFT JOIN v_turn_results tr ON tr.session_id = c.session_id
    GROUP BY c.session_id, c.source_file, c.status, c.note, c.ingested_at
    """,
]

# Views cannot shed columns through CREATE OR REPLACE, so they are dropped
# (dependents first) and rebuilt exactly as in 0001. 0002 and 0003 do not touch
# these three views.
_DOWNGRADE: list[str] = [
    "DROP VIEW IF EXISTS v_conversation_summary",
    "DROP VIEW IF EXISTS v_turn_results",
    "DROP VIEW IF EXISTS v_turn_severity",
    """
    CREATE VIEW v_turn_severity AS
    SELECT
        cl.session_id,
        cl.pair_index,
        COUNT(*) AS claim_count,
        SUM(CASE WHEN v.verdict = 'CONTRADICTED' THEN 1 ELSE 0 END) AS contradicted_count,
        SUM(CASE WHEN v.verdict = 'NOT_FOUND'    THEN 1 ELSE 0 END) AS not_found_count,
        SUM(CASE WHEN v.verdict = 'SUPPORTED'    THEN 1 ELSE 0 END) AS supported_count,
        MAX(CASE v.verdict
                WHEN 'CONTRADICTED' THEN 3
                WHEN 'NOT_FOUND'    THEN 2
                WHEN 'SUPPORTED'    THEN 1
                ELSE 0
            END) AS severity
    FROM claims cl
    JOIN verdicts v ON v.claim_id = cl.claim_id
    GROUP BY cl.session_id, cl.pair_index
    """,
    """
    CREATE VIEW v_turn_results AS
    SELECT
        t.session_id,
        t.pair_index,
        t.caller_asked,
        t.agent_said,
        COALESCE(tc.contains_claims, FALSE) AS contains_claims,
        COALESCE(s.claim_count, 0)          AS claim_count,
        COALESCE(s.contradicted_count, 0)   AS contradicted_count,
        COALESCE(s.not_found_count, 0)      AS not_found_count,
        COALESCE(s.supported_count, 0)      AS supported_count,
        CASE
            WHEN s.severity = 3 THEN 'CONTRADICTED'
            WHEN s.severity = 2 THEN 'NOT_FOUND'
            WHEN s.severity = 1 THEN 'SUPPORTED'
            WHEN tc.session_id IS NULL THEN 'UNCLASSIFIED'
            WHEN NOT tc.contains_claims THEN 'NOT_EVALUATED'
            ELSE 'NO_CLAIMS'
        END AS turn_verdict
    FROM turns t
    LEFT JOIN turn_classifications tc
           ON tc.session_id = t.session_id AND tc.pair_index = t.pair_index
    LEFT JOIN v_turn_severity s
           ON s.session_id = t.session_id AND s.pair_index = t.pair_index
    """,
    """
    CREATE VIEW v_conversation_summary AS
    SELECT
        c.session_id,
        c.source_file,
        c.status,
        c.note,
        c.ingested_at,
        COUNT(tr.pair_index) AS turn_count,
        SUM(CASE WHEN tr.turn_verdict = 'CONTRADICTED' THEN 1 ELSE 0 END) AS contradicted_turns,
        SUM(CASE WHEN tr.turn_verdict = 'NOT_FOUND'    THEN 1 ELSE 0 END) AS not_found_turns,
        SUM(CASE WHEN tr.turn_verdict = 'SUPPORTED'    THEN 1 ELSE 0 END) AS supported_turns,
        SUM(CASE WHEN tr.turn_verdict IN ('NOT_EVALUATED', 'NO_CLAIMS', 'UNCLASSIFIED')
                 THEN 1 ELSE 0 END) AS skipped_turns
    FROM conversations c
    LEFT JOIN v_turn_results tr ON tr.session_id = c.session_id
    GROUP BY c.session_id, c.source_file, c.status, c.note, c.ingested_at
    """,
]


def upgrade() -> None:
    """Make the turn and conversation rollups count unjudged claims."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Restore the 0003 rollup views."""
    for statement in _DOWNGRADE:
        op.execute(statement)

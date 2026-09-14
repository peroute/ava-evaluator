"""Chunk kinds, and flag verdicts judged against an older knowledge base.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-14

* ``kb_chunks.kind`` records whether a chunk is one question and its answer
  (``qa``) or packed descriptive content (``reference``). Retrieval ignores it;
  it exists so reports can say where flags come from.
* ``verdicts.kb_content_hash`` records which version of the knowledge base a
  verdict was judged against. Chunk ids are positional (``t11.r02``), so after
  TTU edits the document the same id can name a different passage. The view
  cannot fix that, but it can say so: ``kb_changed_since_judged`` is true when
  the verdict's hash differs from the current one. Verdicts saved before this
  migration have no hash and are flagged, since their KB version is unknown.
* ``v_claim_results`` gains ``evidence_kind`` and ``kb_changed_since_judged``.
  CREATE OR REPLACE VIEW may only append columns, so both go at the end. The
  "current KB hash" expression is mirrored in ``store.current_kb_hash`` --
  change both together.

Each statement is its own ``op.execute`` call, as in 0001.
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
    ALTER TABLE kb_chunks
        ADD COLUMN kind TEXT NOT NULL DEFAULT 'reference'
        CHECK (kind IN ('qa', 'reference'))
    """,
    "ALTER TABLE verdicts ADD COLUMN kb_content_hash TEXT",
    """
    CREATE OR REPLACE VIEW v_claim_results AS
    SELECT
        cl.claim_id,
        cl.session_id,
        cl.pair_index,
        cl.subject_tag,
        cl.claim_text,
        cl.scope_derived_from,
        v.verdict,
        v.tag,
        v.rationale,
        v.evidence_chunk_id,
        v.attempts,
        v.judged_at,
        ch.section          AS evidence_section,
        ch.text             AS evidence_text,
        d.source_doc        AS evidence_source_doc,
        d.doc_version_date  AS evidence_doc_version_date,
        ch.kind             AS evidence_kind,
        CASE
            WHEN v.claim_id IS NULL THEN NULL
            ELSE v.kb_content_hash IS DISTINCT FROM (
                SELECT string_agg(content_hash, ',' ORDER BY doc_id) FROM kb_documents
            )
        END AS kb_changed_since_judged
    FROM claims cl
    LEFT JOIN verdicts     v  ON v.claim_id  = cl.claim_id
    LEFT JOIN kb_chunks    ch ON ch.chunk_id = v.evidence_chunk_id
    LEFT JOIN kb_documents d  ON d.doc_id    = ch.doc_id
    """,
]

# The view has to go before the column it reads, and it cannot shed columns
# through CREATE OR REPLACE, so it is dropped and rebuilt exactly as in 0001.
_DOWNGRADE: list[str] = [
    "DROP VIEW IF EXISTS v_claim_results",
    """
    CREATE VIEW v_claim_results AS
    SELECT
        cl.claim_id,
        cl.session_id,
        cl.pair_index,
        cl.subject_tag,
        cl.claim_text,
        cl.scope_derived_from,
        v.verdict,
        v.tag,
        v.rationale,
        v.evidence_chunk_id,
        v.attempts,
        v.judged_at,
        ch.section          AS evidence_section,
        ch.text             AS evidence_text,
        d.source_doc        AS evidence_source_doc,
        d.doc_version_date  AS evidence_doc_version_date
    FROM claims cl
    LEFT JOIN verdicts     v  ON v.claim_id  = cl.claim_id
    LEFT JOIN kb_chunks    ch ON ch.chunk_id = v.evidence_chunk_id
    LEFT JOIN kb_documents d  ON d.doc_id    = ch.doc_id
    """,
    "ALTER TABLE verdicts DROP COLUMN IF EXISTS kb_content_hash",
    "ALTER TABLE kb_chunks DROP COLUMN IF EXISTS kind",
]


def upgrade() -> None:
    """Add chunk kinds and verdict KB versions, and extend the claim results view."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Restore the 0002 schema."""
    for statement in _DOWNGRADE:
        op.execute(statement)

"""Initial schema: one table per pipeline stage, plus the rollup views.

Revision ID: 0001
Revises:
Create Date: 2026-09-12

Re-running a pipeline step deletes and rewrites only that stage's rows, which is
what makes "fix step 6 and re-run step 6" cheap.

Types are native PostgreSQL, and constraints mirror the enums in ``models.py``
so a bad value is rejected by the database rather than discovered in a report:

* timestamps are TIMESTAMPTZ, document versions are DATE
* ``contains_claims`` is BOOLEAN, ``scope_derived_from`` is a JSONB array
* ``embedding`` is pgvector's ``vector`` with NO fixed dimension. The embedding
  dimension is configurable (AVA_EMBED_DIM) and tests use tiny vectors, so the
  column accepts any size; the CHECK ties it to ``embedding_dim`` and the
  search refuses to compare vectors of different sizes.
* there is deliberately NO index on ``embedding``. Without one, similarity
  search is an exact scan. An approximate (HNSW/IVFFlat) index could silently
  skip the chunk that supports a claim, and the judge would then report Ava as
  unsupported when the fault was ours.

Each statement is its own ``op.execute`` call: executing several statements in
one call is not reliable through the psycopg driver.
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UPGRADE: list[str] = [
    # Already created by docker/postgres/init.sql locally; repeated here so a
    # hosted database (where no init script runs) gets it too.
    "CREATE EXTENSION IF NOT EXISTS vector",
    # === Step 1: ingest ======================================================
    """
    CREATE TABLE conversations (
        session_id   TEXT PRIMARY KEY,
        source_file  TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('accepted', 'rejected')),
        note         TEXT NOT NULL DEFAULT '',
        ingested_at  TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE turns (
        session_id   TEXT NOT NULL
                     REFERENCES conversations (session_id) ON DELETE CASCADE,
        pair_index   INTEGER NOT NULL CHECK (pair_index >= 0),
        caller_asked TEXT NOT NULL,
        agent_said   TEXT NOT NULL,
        PRIMARY KEY (session_id, pair_index)
    )
    """,
    # === Step 2: knowledge base ==============================================
    """
    CREATE TABLE kb_documents (
        doc_id            TEXT PRIMARY KEY,
        source_path       TEXT NOT NULL,
        source_doc        TEXT NOT NULL,     -- bare filename, for citation display
        content_hash      TEXT NOT NULL,     -- sha256 of extracted text; drives re-indexing
        doc_version_date  DATE NOT NULL,
        indexed_at        TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE kb_chunks (
        chunk_id        TEXT PRIMARY KEY,
        doc_id          TEXT NOT NULL
                        REFERENCES kb_documents (doc_id) ON DELETE CASCADE,
        section         TEXT NOT NULL,
        ordinal         INTEGER NOT NULL,
        text            TEXT NOT NULL,
        embedding       vector NOT NULL,     -- unit-normalised; no ANN index on purpose
        embedding_dim   INTEGER NOT NULL,
        embedding_model TEXT NOT NULL,
        CHECK (embedding_dim = vector_dims(embedding))
    )
    """,
    "CREATE INDEX idx_kb_chunks_doc ON kb_chunks (doc_id)",
    # === Step 3: turn classification =========================================
    """
    CREATE TABLE turn_classifications (
        session_id      TEXT NOT NULL
                        REFERENCES conversations (session_id) ON DELETE CASCADE,
        pair_index      INTEGER NOT NULL,
        contains_claims BOOLEAN NOT NULL,
        reason          TEXT NOT NULL DEFAULT '',
        classified_at   TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (session_id, pair_index)
    )
    """,
    # === Step 4: claim decomposition =========================================
    """
    CREATE TABLE claims (
        claim_id           TEXT PRIMARY KEY,
        session_id         TEXT NOT NULL
                           REFERENCES conversations (session_id) ON DELETE CASCADE,
        pair_index         INTEGER NOT NULL,
        subject_tag        TEXT NOT NULL DEFAULT '',
        claim_text         TEXT NOT NULL,
        scope_derived_from JSONB NOT NULL DEFAULT '[]'::jsonb
                           CHECK (jsonb_typeof(scope_derived_from) = 'array'),
        created_at         TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX idx_claims_turn ON claims (session_id, pair_index)",
    # === Step 5: retrieval ===================================================
    # `attempt` makes the step-6 NOT_FOUND retry visible in the data: attempt 1
    # is the normal top-k, attempt 2 the widened search. chunk_id has no foreign
    # key so re-indexing a document never fails on old retrieval history; reads
    # join to kb_chunks, so stale rows simply stop appearing.
    """
    CREATE TABLE claim_retrievals (
        claim_id TEXT NOT NULL REFERENCES claims (claim_id) ON DELETE CASCADE,
        chunk_id TEXT NOT NULL,
        attempt  INTEGER NOT NULL CHECK (attempt >= 1),
        rank     INTEGER NOT NULL,
        score    DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (claim_id, chunk_id, attempt)
    )
    """,
    "CREATE INDEX idx_retrievals_claim ON claim_retrievals (claim_id)",
    # === Step 6: verdicts ====================================================
    """
    CREATE TABLE verdicts (
        claim_id          TEXT PRIMARY KEY REFERENCES claims (claim_id) ON DELETE CASCADE,
        verdict           TEXT NOT NULL
                          CHECK (verdict IN ('SUPPORTED', 'CONTRADICTED', 'NOT_FOUND')),
        tag               TEXT
                          CHECK (tag IS NULL OR tag IN ('kb_gap', 'retrieval_miss')),
        rationale         TEXT NOT NULL DEFAULT '',
        evidence_chunk_id TEXT,
        attempts          INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
        judged_at         TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX idx_verdicts_verdict ON verdicts (verdict)",
    # === Step 7: derived views ===============================================
    # Turn- and conversation-level results are VIEWS, never stored tables. A
    # stored summary can silently go stale when claims are re-judged; a view
    # cannot. To change a view later, write a new migration using
    # CREATE OR REPLACE VIEW -- never edit this file once it has been applied.
    #
    # Claim level: the base truth, joined to its evidence so a flag can always
    # be traced back to the exact KB wording it was based on.
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
    # The severity rollup. Mirrors SEVERITY in models.py -- change both together.
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
    # Turn level: worst verdict wins. Turns with nothing to check are labelled
    # rather than dropped, so turn counts in reports stay honest.
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
    # Conversation level: counts of flagged vs supported turns per session.
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

# Dependents first: views before the tables they read, children before parents.
# The vector extension is left in place -- other databases may rely on it.
_DOWNGRADE: list[str] = [
    "DROP VIEW IF EXISTS v_conversation_summary",
    "DROP VIEW IF EXISTS v_turn_results",
    "DROP VIEW IF EXISTS v_turn_severity",
    "DROP VIEW IF EXISTS v_claim_results",
    "DROP TABLE IF EXISTS verdicts",
    "DROP TABLE IF EXISTS claim_retrievals",
    "DROP TABLE IF EXISTS claims",
    "DROP TABLE IF EXISTS turn_classifications",
    "DROP TABLE IF EXISTS kb_chunks",
    "DROP TABLE IF EXISTS kb_documents",
    "DROP TABLE IF EXISTS turns",
    "DROP TABLE IF EXISTS conversations",
]


def upgrade() -> None:
    """Create every table, index and view."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop everything upgrade() created, in dependency order."""
    for statement in _DOWNGRADE:
        op.execute(statement)

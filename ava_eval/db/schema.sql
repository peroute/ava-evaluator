-- ---------------------------------------------------------------------------
-- Ava evaluator schema.
--
-- One table per pipeline stage. Re-running a step deletes and rewrites only
-- that stage's rows, which is what makes "fix step 6 and re-run step 6" cheap.
--
-- PORTABILITY: this file must stay runnable on PostgreSQL with only trivial
-- edits, so it deliberately avoids:
--   * AUTOINCREMENT / implicit rowid  -> every id is an explicit TEXT value
--   * datetime('now')                 -> timestamps are ISO-8601 written by Python
--   * JSON1 functions                 -> JSON is TEXT, parsed in Python
--   * INSERT OR REPLACE               -> upserts use ON CONFLICT ... DO UPDATE
--   * BOOLEAN                         -> INTEGER 0/1, cast at the Python boundary
-- ---------------------------------------------------------------------------

-- === Step 1: ingest ========================================================

-- raw_payload keeps the provider's complete original conversation as JSON text,
-- for auditing and reprocessing; it is NULL only for rows ingested before it was
-- captured. Databases created before these two columns existed get them from
-- store.init_db, since CREATE TABLE IF NOT EXISTS cannot add columns.
CREATE TABLE IF NOT EXISTS conversations (
    session_id   TEXT PRIMARY KEY,
    source_file  TEXT NOT NULL,
    status       TEXT NOT NULL,          -- accepted | rejected
    note         TEXT NOT NULL DEFAULT '',
    ingested_at  TEXT NOT NULL,
    source_type  TEXT NOT NULL DEFAULT 'json_export',   -- which TranscriptSource produced it
    raw_payload  TEXT                                   -- JSON text, not a BLOB
);

CREATE TABLE IF NOT EXISTS turns (
    session_id   TEXT NOT NULL,
    pair_index   INTEGER NOT NULL,
    caller_asked TEXT NOT NULL,
    agent_said   TEXT NOT NULL,
    PRIMARY KEY (session_id, pair_index),
    FOREIGN KEY (session_id) REFERENCES conversations (session_id) ON DELETE CASCADE
);

-- === Step 2: knowledge base ================================================

CREATE TABLE IF NOT EXISTS kb_documents (
    doc_id            TEXT PRIMARY KEY,
    source_path       TEXT NOT NULL,
    source_doc        TEXT NOT NULL,     -- bare filename, for citation display
    content_hash      TEXT NOT NULL,     -- sha256 of extracted text; drives re-indexing
    doc_version_date  TEXT NOT NULL,
    indexed_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id        TEXT PRIMARY KEY,
    doc_id          TEXT NOT NULL,
    section         TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    text            TEXT NOT NULL,
    embedding       BLOB NOT NULL,       -- float32 little-endian, unit-normalised
    embedding_dim   INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES kb_documents (doc_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks (doc_id);

-- === Step 3: turn classification ===========================================

CREATE TABLE IF NOT EXISTS turn_classifications (
    session_id      TEXT NOT NULL,
    pair_index      INTEGER NOT NULL,
    contains_claims INTEGER NOT NULL,    -- 0/1
    reason          TEXT NOT NULL DEFAULT '',
    classified_at   TEXT NOT NULL,
    PRIMARY KEY (session_id, pair_index),
    FOREIGN KEY (session_id) REFERENCES conversations (session_id) ON DELETE CASCADE
);

-- === Step 4: claim decomposition ===========================================

CREATE TABLE IF NOT EXISTS claims (
    claim_id           TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    pair_index         INTEGER NOT NULL,
    subject_tag        TEXT NOT NULL DEFAULT '',
    claim_text         TEXT NOT NULL,
    scope_derived_from TEXT NOT NULL DEFAULT '[]',   -- JSON array of pair_index
    created_at         TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES conversations (session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_claims_turn ON claims (session_id, pair_index);

-- === Step 5: retrieval =====================================================
-- `attempt` makes the step-6 NOT_FOUND retry visible in the data: attempt 1 is
-- the normal top-k, attempt 2 is the widened search. Keeping both lets us ask
-- later how often widening rescued a claim -- the retrieval_miss signal.

CREATE TABLE IF NOT EXISTS claim_retrievals (
    claim_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    attempt  INTEGER NOT NULL,
    rank     INTEGER NOT NULL,
    score    REAL NOT NULL,
    PRIMARY KEY (claim_id, chunk_id, attempt),
    FOREIGN KEY (claim_id) REFERENCES claims (claim_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_retrievals_claim ON claim_retrievals (claim_id);

-- === Step 6: verdicts ======================================================

CREATE TABLE IF NOT EXISTS verdicts (
    claim_id          TEXT PRIMARY KEY,
    verdict           TEXT NOT NULL,     -- SUPPORTED | CONTRADICTED | NOT_FOUND
    tag               TEXT,              -- kb_gap | retrieval_miss | NULL
    rationale         TEXT NOT NULL DEFAULT '',
    evidence_chunk_id TEXT,
    attempts          INTEGER NOT NULL DEFAULT 1,
    judged_at         TEXT NOT NULL,
    FOREIGN KEY (claim_id) REFERENCES claims (claim_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_verdicts_verdict ON verdicts (verdict);

-- === Step 7: derived views =================================================
-- Turn- and conversation-level results are VIEWS, never stored tables. A
-- stored summary can silently go stale when claim rows are re-judged; a view
-- cannot. The three granularities are therefore incapable of disagreeing.

DROP VIEW IF EXISTS v_conversation_summary;
DROP VIEW IF EXISTS v_turn_results;
DROP VIEW IF EXISTS v_turn_severity;
DROP VIEW IF EXISTS v_claim_results;

-- Claim level: the base truth, joined to its evidence so a flag can always be
-- traced back to the exact KB wording it was based on.
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
LEFT JOIN kb_documents d  ON d.doc_id    = ch.doc_id;

-- The severity rollup. Mirrors SEVERITY in models.py -- change both together.
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
GROUP BY cl.session_id, cl.pair_index;

-- Turn level: worst verdict wins. Turns with nothing to check are labelled
-- rather than dropped, so turn counts in reports stay honest.
CREATE VIEW v_turn_results AS
SELECT
    t.session_id,
    t.pair_index,
    t.caller_asked,
    t.agent_said,
    COALESCE(tc.contains_claims, 0)   AS contains_claims,
    COALESCE(s.claim_count, 0)        AS claim_count,
    COALESCE(s.contradicted_count, 0) AS contradicted_count,
    COALESCE(s.not_found_count, 0)    AS not_found_count,
    COALESCE(s.supported_count, 0)    AS supported_count,
    CASE
        WHEN s.severity = 3 THEN 'CONTRADICTED'
        WHEN s.severity = 2 THEN 'NOT_FOUND'
        WHEN s.severity = 1 THEN 'SUPPORTED'
        WHEN tc.session_id IS NULL THEN 'UNCLASSIFIED'
        WHEN COALESCE(tc.contains_claims, 0) = 0 THEN 'NOT_EVALUATED'
        ELSE 'NO_CLAIMS'
    END AS turn_verdict
FROM turns t
LEFT JOIN turn_classifications tc
       ON tc.session_id = t.session_id AND tc.pair_index = t.pair_index
LEFT JOIN v_turn_severity s
       ON s.session_id = t.session_id AND s.pair_index = t.pair_index;

-- Conversation level: counts of flagged vs supported turns per session.
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
GROUP BY c.session_id, c.source_file, c.status, c.note, c.ingested_at;

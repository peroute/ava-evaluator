"""The only module in the project that speaks SQL.

Every read and write goes through a named function here. Steps never build
queries themselves. That rule is what turns "migrate to PostgreSQL" from a
hunt through the codebase into a contained job on one file.

Three things carry the portability promise:

* ``_sql()`` rewrites the parameter placeholder. SQLite wants ``?``, psycopg
  wants ``%s``. The queries below are written with ``?`` and translated once.
* Timestamps are generated in Python as ISO-8601 strings, never by the database.
* Embeddings are stored as raw ``float32`` bytes rather than a vendor vector
  type, so the only thing PostgreSQL changes is how similarity is computed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import numpy as np

from ..config import settings
from ..models import (
    Claim,
    ClaimVerdict,
    ClassifiedTurn,
    ConversationStatus,
    KbChunk,
    RetrievedChunk,
    Turn,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# SQLite uses "?"; psycopg uses "%s". Change this one constant when porting.
PLACEHOLDER = "?"


def _sql(query: str) -> str:
    """Adapt a ``?``-style query to the active driver's placeholder style."""
    if PLACEHOLDER == "?":
        return query
    return query.replace("?", PLACEHOLDER)


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string.

    Generated here rather than by the database so the value does not depend on
    which engine is underneath.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on failure, always close.

    ``foreign_keys`` is off by default in SQLite, which would silently let
    orphaned rows accumulate. Turning it on makes ON DELETE CASCADE real, so
    deleting a conversation actually removes its turns, claims and verdicts.
    """
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> Path:
    """Create the tables and views. Safe to run repeatedly."""
    path = db_path or settings.db_path
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(path) as conn:
        conn.executescript(schema)
        _add_missing_columns(conn)
    return path


# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an
# existing table untouched, so older databases are brought up to date here.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "conversations": [
        ("source_type", "TEXT NOT NULL DEFAULT 'json_export'"),
    ],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add any column in ``_ADDED_COLUMNS`` that an older database lacks.

    PORTABILITY: ``PRAGMA table_info`` is SQLite-only. On PostgreSQL this becomes
    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` and the lookup goes away.
    """
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


@contextmanager
def savepoint(conn: sqlite3.Connection, name: str = "unit_of_work") -> Iterator[None]:
    """Make a block of writes atomic without committing the surrounding transaction.

    On an exception everything written inside the block is rolled back and the
    exception re-raised; earlier work on the same connection is kept. SAVEPOINT
    is standard SQL, so this ports to PostgreSQL unchanged.
    """
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    """Convert a cursor's rows into plain dicts, so callers never see sqlite3.Row."""
    return [dict(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Embedding (de)serialisation
# ---------------------------------------------------------------------------


def pack_embedding(vector: Sequence[float]) -> bytes:
    """Normalise a vector to unit length and pack it as float32 bytes.

    Normalising at write time is what lets retrieval use a plain dot product:
    for unit vectors, the dot product *is* the cosine similarity. That keeps
    the search itself to a single matrix multiply with no per-query division.
    """
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm > 0.0:
        arr = arr / norm
    return arr.astype(np.float32).tobytes()


def unpack_embedding(blob: bytes) -> np.ndarray:
    """Read float32 bytes back into a 1-D array."""
    return np.frombuffer(blob, dtype=np.float32)


# ---------------------------------------------------------------------------
# Step 1 -- conversations and turns
# ---------------------------------------------------------------------------


def upsert_conversation(
    conn: sqlite3.Connection,
    session_id: str,
    source_file: str,
    status: ConversationStatus,
    note: str = "",
    *,
    source_type: str = "json_export",
) -> None:
    """Record a conversation, including rejected ones so counts stay honest.

    One row per session: re-ingesting overwrites it with the latest version.
    """
    conn.execute(
        _sql("""
            INSERT INTO conversations
                (session_id, source_file, status, note, ingested_at, source_type)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id) DO UPDATE SET
                source_file = excluded.source_file,
                status      = excluded.status,
                note        = excluded.note,
                ingested_at = excluded.ingested_at,
                source_type = excluded.source_type
            """),
        (session_id, source_file, status.value, note, utcnow_iso(), source_type),
    )


def get_conversation_record(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    """The stored conversation row as a dict, or None if the session is unknown."""
    cur = conn.execute(_sql("SELECT * FROM conversations WHERE session_id = ?"), (session_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def delete_turns(conn: sqlite3.Connection, session_id: str) -> int:
    """Remove every turn for a session. Returns the number of rows deleted."""
    cur = conn.execute(_sql("DELETE FROM turns WHERE session_id = ?"), (session_id,))
    return cur.rowcount


def replace_turns(conn: sqlite3.Connection, session_id: str, turns: Iterable[Turn]) -> int:
    """Delete and rewrite this conversation's turns. Returns the count written."""
    delete_turns(conn, session_id)
    payload = [(t.session_id, t.pair_index, t.caller_asked, t.agent_said) for t in turns]
    conn.executemany(
        _sql("""
            INSERT INTO turns (session_id, pair_index, caller_asked, agent_said)
            VALUES (?, ?, ?, ?)
            """),
        payload,
    )
    return len(payload)


def get_turns(conn: sqlite3.Connection, session_id: str | None = None) -> list[Turn]:
    """Fetch turns in order, for one session or for every accepted session."""
    if session_id:
        cur = conn.execute(
            _sql("SELECT * FROM turns WHERE session_id = ? ORDER BY pair_index"),
            (session_id,),
        )
    else:
        cur = conn.execute("SELECT * FROM turns ORDER BY session_id, pair_index")
    return [Turn(**row) for row in _rows(cur)]


def list_sessions(
    conn: sqlite3.Connection, status: ConversationStatus | None = ConversationStatus.ACCEPTED
) -> list[str]:
    """List session ids, optionally filtered by ingest status."""
    if status is None:
        cur = conn.execute("SELECT session_id FROM conversations ORDER BY session_id")
    else:
        cur = conn.execute(
            _sql("SELECT session_id FROM conversations WHERE status = ? ORDER BY session_id"),
            (status.value,),
        )
    return [row["session_id"] for row in _rows(cur)]


# ---------------------------------------------------------------------------
# Step 2 -- knowledge base
# ---------------------------------------------------------------------------


def get_document_hash(conn: sqlite3.Connection, doc_id: str) -> str | None:
    """Return the stored content hash for a document, or None if never indexed."""
    cur = conn.execute(_sql("SELECT content_hash FROM kb_documents WHERE doc_id = ?"), (doc_id,))
    row = cur.fetchone()
    return row["content_hash"] if row else None


def upsert_document(
    conn: sqlite3.Connection,
    doc_id: str,
    source_path: str,
    source_doc: str,
    content_hash: str,
    doc_version_date: str,
) -> None:
    """Record a KB document and the hash that decides whether to re-embed it."""
    conn.execute(
        _sql("""
            INSERT INTO kb_documents
                (doc_id, source_path, source_doc, content_hash, doc_version_date, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (doc_id) DO UPDATE SET
                source_path      = excluded.source_path,
                source_doc       = excluded.source_doc,
                content_hash     = excluded.content_hash,
                doc_version_date = excluded.doc_version_date,
                indexed_at       = excluded.indexed_at
            """),
        (doc_id, source_path, source_doc, content_hash, doc_version_date, utcnow_iso()),
    )


def delete_chunks_for_doc(conn: sqlite3.Connection, doc_id: str) -> None:
    """Drop a document's chunks ahead of re-indexing it."""
    conn.execute(_sql("DELETE FROM kb_chunks WHERE doc_id = ?"), (doc_id,))


def insert_chunks(
    conn: sqlite3.Connection,
    chunks: Sequence[KbChunk],
    embeddings: Sequence[Sequence[float]],
    embedding_model: str,
) -> int:
    """Write chunks with their embeddings. Lengths must match."""
    if len(chunks) != len(embeddings):
        raise ValueError(f"{len(chunks)} chunks but {len(embeddings)} embeddings")
    payload = []
    for chunk, vector in zip(chunks, embeddings):
        blob = pack_embedding(vector)
        payload.append(
            (
                chunk.chunk_id,
                chunk.doc_id,
                chunk.section,
                chunk.ordinal,
                chunk.text,
                blob,
                len(vector),
                embedding_model,
            )
        )
    conn.executemany(
        _sql("""
            INSERT INTO kb_chunks
                (chunk_id, doc_id, section, ordinal, text,
                 embedding, embedding_dim, embedding_model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chunk_id) DO UPDATE SET
                doc_id          = excluded.doc_id,
                section         = excluded.section,
                ordinal         = excluded.ordinal,
                text            = excluded.text,
                embedding       = excluded.embedding,
                embedding_dim   = excluded.embedding_dim,
                embedding_model = excluded.embedding_model
            """),
        payload,
    )
    return len(payload)


def load_chunk_matrix(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Load every chunk's metadata and stack the embeddings into one matrix.

    Returns ``(metadata, matrix)`` where matrix has shape ``(n_chunks, dim)``.
    At KB scale this comfortably fits in memory, and holding it as one array is
    what makes the whole search a single matrix multiply.
    """
    cur = conn.execute("""
        SELECT c.chunk_id, c.doc_id, c.section, c.text, c.embedding, c.embedding_dim,
               d.source_doc, d.doc_version_date
        FROM kb_chunks c
        JOIN kb_documents d ON d.doc_id = c.doc_id
        ORDER BY c.chunk_id
        """)
    meta: list[dict[str, Any]] = []
    vectors: list[np.ndarray] = []
    for row in cur.fetchall():
        record = dict(row)
        blob = record.pop("embedding")
        vectors.append(unpack_embedding(blob))
        meta.append(record)

    if not vectors:
        return [], np.zeros((0, 0), dtype=np.float32)
    return meta, np.vstack(vectors).astype(np.float32)


def count_chunks(conn: sqlite3.Connection) -> int:
    """How many chunks are currently indexed."""
    cur = conn.execute("SELECT COUNT(*) AS n FROM kb_chunks")
    return int(cur.fetchone()["n"])


def get_chunk(conn: sqlite3.Connection, chunk_id: str) -> dict[str, Any] | None:
    """Fetch a single chunk with its document provenance."""
    cur = conn.execute(
        _sql("""
            SELECT c.chunk_id, c.section, c.text, d.source_doc, d.doc_version_date
            FROM kb_chunks c
            JOIN kb_documents d ON d.doc_id = c.doc_id
            WHERE c.chunk_id = ?
            """),
        (chunk_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Step 3 -- classifications
# ---------------------------------------------------------------------------


def save_classifications(conn: sqlite3.Connection, rows: Iterable[ClassifiedTurn]) -> int:
    """Persist turn classifications, overwriting any previous run."""
    now = utcnow_iso()
    payload = [
        (r.session_id, r.pair_index, 1 if r.contains_claims else 0, r.reason, now) for r in rows
    ]
    conn.executemany(
        _sql("""
            INSERT INTO turn_classifications
                (session_id, pair_index, contains_claims, reason, classified_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (session_id, pair_index) DO UPDATE SET
                contains_claims = excluded.contains_claims,
                reason          = excluded.reason,
                classified_at   = excluded.classified_at
            """),
        payload,
    )
    return len(payload)


def get_classifications(
    conn: sqlite3.Connection, session_id: str | None = None
) -> list[ClassifiedTurn]:
    """Read back classifications, for one session or all of them."""
    base = """
        SELECT session_id, pair_index, contains_claims, reason
        FROM turn_classifications
    """
    if session_id:
        cur = conn.execute(_sql(base + " WHERE session_id = ? ORDER BY pair_index"), (session_id,))
    else:
        cur = conn.execute(base + " ORDER BY session_id, pair_index")
    return [
        ClassifiedTurn(
            session_id=row["session_id"],
            pair_index=row["pair_index"],
            contains_claims=bool(row["contains_claims"]),
            reason=row["reason"],
        )
        for row in _rows(cur)
    ]


# ---------------------------------------------------------------------------
# Step 4 -- claims
# ---------------------------------------------------------------------------


def replace_claims_for_session(
    conn: sqlite3.Connection, session_id: str, claims: Sequence[Claim]
) -> int:
    """Delete and rewrite a session's claims.

    The cascade also clears that session's retrievals and verdicts, which is
    correct: a claim whose wording changed is a different claim, and keeping an
    old verdict attached to new text is exactly the kind of quiet inconsistency
    an audit trail is supposed to prevent.
    """
    conn.execute(_sql("DELETE FROM claims WHERE session_id = ?"), (session_id,))
    now = utcnow_iso()
    payload = [
        (
            c.claim_id,
            c.session_id,
            c.pair_index,
            c.subject_tag,
            c.text,
            json.dumps(c.scope_derived_from),
            now,
        )
        for c in claims
    ]
    conn.executemany(
        _sql("""
            INSERT INTO claims
                (claim_id, session_id, pair_index, subject_tag,
                 claim_text, scope_derived_from, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """),
        payload,
    )
    return len(payload)


def get_claims(conn: sqlite3.Connection, session_id: str | None = None) -> list[Claim]:
    """Read claims back, for one session or all of them."""
    base = """
        SELECT claim_id, session_id, pair_index, subject_tag, claim_text, scope_derived_from
        FROM claims
    """
    if session_id:
        cur = conn.execute(
            _sql(base + " WHERE session_id = ? ORDER BY pair_index, claim_id"), (session_id,)
        )
    else:
        cur = conn.execute(base + " ORDER BY session_id, pair_index, claim_id")
    return [
        Claim(
            claim_id=row["claim_id"],
            session_id=row["session_id"],
            pair_index=row["pair_index"],
            subject_tag=row["subject_tag"],
            text=row["claim_text"],
            scope_derived_from=json.loads(row["scope_derived_from"]),
        )
        for row in _rows(cur)
    ]


# ---------------------------------------------------------------------------
# Step 5 -- retrievals
# ---------------------------------------------------------------------------


def save_retrievals(
    conn: sqlite3.Connection, claim_id: str, attempt: int, chunks: Sequence[RetrievedChunk]
) -> int:
    """Persist what retrieval returned for one claim on one attempt."""
    payload = [(claim_id, c.chunk_id, attempt, c.rank, c.score) for c in chunks]
    conn.executemany(
        _sql("""
            INSERT INTO claim_retrievals (claim_id, chunk_id, attempt, rank, score)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (claim_id, chunk_id, attempt) DO UPDATE SET
                rank  = excluded.rank,
                score = excluded.score
            """),
        payload,
    )
    return len(payload)


def clear_retrievals(conn: sqlite3.Connection, claim_id: str) -> None:
    """Drop a claim's retrievals ahead of re-running step 5."""
    conn.execute(_sql("DELETE FROM claim_retrievals WHERE claim_id = ?"), (claim_id,))


def get_retrievals(
    conn: sqlite3.Connection, claim_id: str, attempt: int | None = None
) -> list[RetrievedChunk]:
    """Read a claim's retrieved chunks, optionally for one attempt only."""
    query = """
        SELECT r.chunk_id, r.rank, r.score, c.text, c.section, d.source_doc
        FROM claim_retrievals r
        JOIN kb_chunks c    ON c.chunk_id = r.chunk_id
        JOIN kb_documents d ON d.doc_id   = c.doc_id
        WHERE r.claim_id = ?
    """
    params: list[Any] = [claim_id]
    if attempt is not None:
        query += " AND r.attempt = ?"
        params.append(attempt)
    query += " ORDER BY r.rank"
    cur = conn.execute(_sql(query), tuple(params))
    return [
        RetrievedChunk(
            chunk_id=row["chunk_id"],
            text=row["text"],
            score=row["score"],
            source_doc=row["source_doc"],
            section=row["section"],
            rank=row["rank"],
        )
        for row in _rows(cur)
    ]


# ---------------------------------------------------------------------------
# Step 6 -- verdicts
# ---------------------------------------------------------------------------


def save_verdict(conn: sqlite3.Connection, verdict: ClaimVerdict) -> None:
    """Persist one claim's verdict, replacing any earlier judgement of it."""
    conn.execute(
        _sql("""
            INSERT INTO verdicts
                (claim_id, verdict, tag, rationale, evidence_chunk_id, attempts, judged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (claim_id) DO UPDATE SET
                verdict           = excluded.verdict,
                tag               = excluded.tag,
                rationale         = excluded.rationale,
                evidence_chunk_id = excluded.evidence_chunk_id,
                attempts          = excluded.attempts,
                judged_at         = excluded.judged_at
            """),
        (
            verdict.claim_id,
            verdict.verdict.value,
            verdict.tag.value if verdict.tag else None,
            verdict.rationale,
            verdict.evidence_chunk_id or None,
            verdict.attempts,
            utcnow_iso(),
        ),
    )


# ---------------------------------------------------------------------------
# Step 7 -- reads over the derived views
# ---------------------------------------------------------------------------


def conversation_summary(
    conn: sqlite3.Connection, session_id: str | None = None
) -> list[dict[str, Any]]:
    """Per-conversation counts of flagged vs supported turns."""
    base = "SELECT * FROM v_conversation_summary"
    if session_id:
        cur = conn.execute(_sql(base + " WHERE session_id = ?"), (session_id,))
    else:
        cur = conn.execute(base + " ORDER BY session_id")
    return _rows(cur)


def turn_results(conn: sqlite3.Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    """Turn-level rollups, worst verdict per turn."""
    base = "SELECT * FROM v_turn_results"
    if session_id:
        cur = conn.execute(_sql(base + " WHERE session_id = ? ORDER BY pair_index"), (session_id,))
    else:
        cur = conn.execute(base + " ORDER BY session_id, pair_index")
    return _rows(cur)


def claim_results(
    conn: sqlite3.Connection,
    session_id: str | None = None,
    verdict: str | None = None,
    tag: str | None = None,
    subject_tag: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Filterable claim-level results -- what the frontend actually queries."""
    query = "SELECT * FROM v_claim_results WHERE 1 = 1"
    params: list[Any] = []
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    if verdict:
        query += " AND verdict = ?"
        params.append(verdict)
    if tag:
        query += " AND tag = ?"
        params.append(tag)
    if subject_tag:
        query += " AND subject_tag = ?"
        params.append(subject_tag)
    query += " ORDER BY session_id, pair_index, claim_id LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    cur = conn.execute(_sql(query), tuple(params))
    return _rows(cur)


def get_claim_result(conn: sqlite3.Connection, claim_id: str) -> dict[str, Any] | None:
    """One claim with its verdict and the evidence text behind it."""
    cur = conn.execute(_sql("SELECT * FROM v_claim_results WHERE claim_id = ?"), (claim_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def verdict_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts by verdict and by tag, for a dashboard header."""
    by_verdict = _rows(conn.execute("""
            SELECT verdict, COUNT(*) AS n
            FROM verdicts
            GROUP BY verdict
            ORDER BY verdict
            """))
    by_tag = _rows(conn.execute("""
            SELECT tag, COUNT(*) AS n
            FROM verdicts
            WHERE tag IS NOT NULL
            GROUP BY tag
            ORDER BY tag
            """))
    totals = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM conversations WHERE status = 'accepted') AS conversations,
            (SELECT COUNT(*) FROM turns)      AS turns,
            (SELECT COUNT(*) FROM claims)     AS claims,
            (SELECT COUNT(*) FROM kb_chunks)  AS kb_chunks
        """).fetchone()
    return {
        "totals": dict(totals),
        "by_verdict": {row["verdict"]: row["n"] for row in by_verdict},
        "by_tag": {row["tag"]: row["n"] for row in by_tag},
    }

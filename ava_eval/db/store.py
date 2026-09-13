"""The only module in the project that speaks SQL.

Every read and write goes through a named function here. Steps never build
queries themselves, so the shape of the database is visible in one file and a
schema change is a contained job rather than a hunt through the codebase.

The schema itself lives in Alembic migrations under ``db/migrations``. Things
worth knowing before editing a query:

* Parameters use psycopg placeholders (``%s`` / ``%(name)s``), never string
  formatting. A consequence: a literal ``%`` inside SQL, such as a ``LIKE``
  pattern, must be written ``%%``.
* psycopg 3 connections have ``execute`` but no ``executemany``; batch writes
  open a cursor for that.
* In PostgreSQL a failed statement poisons the rest of its transaction -- every
  later statement is refused until a rollback. Code that wants to survive one
  failure and carry on must wrap the risky part in ``atomic()``.
* Similarity search runs inside PostgreSQL with pgvector and is exact. See
  ``search_chunks``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator, Sequence

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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

# What every function here receives. Steps import this alias instead of psycopg,
# so the driver stays an implementation detail of this module.
Connection = psycopg.Connection[dict[str, Any]]


def utcnow() -> datetime:
    """Current UTC time, timezone-aware, for TIMESTAMPTZ columns.

    Generated in Python rather than with ``now()`` so every row written in one
    call carries the same timestamp and tests can reason about it.
    """
    return datetime.now(timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


def _describe(url: str) -> str:
    """``host:port/dbname`` for error messages -- never the password."""
    try:
        parts = conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return "<unparseable DATABASE_URL>"
    host = parts.get("host", "localhost")
    port = parts.get("port", "5432")
    return f"{host}:{port}/{parts.get('dbname', '?')}"


@contextmanager
def connect(database_url: str | None = None) -> Iterator[Connection]:
    """Open a connection, commit on success, roll back on failure, always close.

    Rows come back as plain dicts (``dict_row``), so callers never see a driver
    type. ``register_vector`` teaches this connection to send and receive numpy
    arrays as pgvector's ``vector`` type; it fails if the extension is missing,
    which in practice means migrations were never run.
    """
    url = database_url or settings.database_url
    try:
        conn = psycopg.connect(url, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        raise RuntimeError(
            f"Could not connect to PostgreSQL at {_describe(url)}. Is it running? "
            f"Locally: 'docker compose up -d db'. Driver said: {exc}"
        ) from exc

    try:
        register_vector(conn)
    except psycopg.ProgrammingError as exc:
        conn.close()
        raise RuntimeError(
            f"The pgvector 'vector' type is missing in {_describe(url)}. "
            f"Run 'python -m ava_eval migrate' first."
        ) from exc

    # Only changes how timestamps are displayed and returned (psycopg hands back
    # datetimes in the session zone); the stored instants are untouched. Set here
    # as well as by migration 0002 so the app never depends on the database
    # default. set_config rather than SET, because SET cannot take a parameter.
    try:
        conn.execute("SELECT set_config('TimeZone', %s, false)", (settings.display_timezone,))
    except psycopg.errors.InvalidParameterValue as exc:
        conn.close()
        raise RuntimeError(
            f"AVA_DISPLAY_TIMEZONE={settings.display_timezone!r} is not a time zone "
            f"PostgreSQL recognises. Use an IANA name such as 'America/Chicago'."
        ) from exc

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def atomic(conn: Connection) -> Iterator[None]:
    """Make a block all-or-nothing without ending the surrounding transaction.

    Inside an open transaction psycopg implements this as a SAVEPOINT: if the
    block raises, only its own writes are rolled back and the connection stays
    usable. Without it, one failed statement would make PostgreSQL refuse every
    later statement in the transaction ("current transaction is aborted").
    """
    with conn.transaction():
        yield


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


def normalise(vector: Sequence[float]) -> np.ndarray:
    """Return ``vector`` as unit-length float32.

    Normalising at write time *and* at query time is what lets search use a
    plain inner product: for unit vectors, the inner product is the cosine
    similarity.
    """
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm > 0.0:
        arr = arr / norm
    return arr.astype(np.float32)


# ---------------------------------------------------------------------------
# Step 1 -- conversations and turns
# ---------------------------------------------------------------------------


def upsert_conversation(
    conn: Connection,
    session_id: str,
    source_file: str,
    status: ConversationStatus,
    note: str = "",
    *,
    source_type: str = "json_export",
) -> None:
    """Record a conversation, including rejected ones so counts stay honest.

    One row per session: re-ingesting overwrites it with the latest version.
    ``source_type`` names the transcript source that produced it.
    """
    conn.execute(
        """
        INSERT INTO conversations (session_id, source_file, status, note, ingested_at, source_type)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (session_id) DO UPDATE SET
            source_file = excluded.source_file,
            status      = excluded.status,
            note        = excluded.note,
            ingested_at = excluded.ingested_at,
            source_type = excluded.source_type
        """,
        (session_id, source_file, status.value, note, utcnow(), source_type),
    )


def get_conversation_record(conn: Connection, session_id: str) -> dict[str, Any] | None:
    """The stored conversation row as a dict, or None if the session is unknown."""
    return conn.execute(
        "SELECT * FROM conversations WHERE session_id = %s", (session_id,)
    ).fetchone()


def delete_turns(conn: Connection, session_id: str) -> int:
    """Remove every turn for a session. Returns the number of rows deleted."""
    return conn.execute("DELETE FROM turns WHERE session_id = %s", (session_id,)).rowcount


def replace_turns(conn: Connection, session_id: str, turns: Iterable[Turn]) -> int:
    """Delete and rewrite this conversation's turns. Returns the count written."""
    delete_turns(conn, session_id)
    payload = [(t.session_id, t.pair_index, t.caller_asked, t.agent_said) for t in turns]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO turns (session_id, pair_index, caller_asked, agent_said)
            VALUES (%s, %s, %s, %s)
            """,
            payload,
        )
    return len(payload)


def get_turns(conn: Connection, session_id: str | None = None) -> list[Turn]:
    """Fetch turns in order, for one session or for every session."""
    if session_id:
        cur = conn.execute(
            "SELECT * FROM turns WHERE session_id = %s ORDER BY pair_index", (session_id,)
        )
    else:
        cur = conn.execute("SELECT * FROM turns ORDER BY session_id, pair_index")
    return [Turn(**row) for row in cur.fetchall()]


def list_sessions(
    conn: Connection, status: ConversationStatus | None = ConversationStatus.ACCEPTED
) -> list[str]:
    """List session ids, optionally filtered by ingest status."""
    if status is None:
        cur = conn.execute("SELECT session_id FROM conversations ORDER BY session_id")
    else:
        cur = conn.execute(
            "SELECT session_id FROM conversations WHERE status = %s ORDER BY session_id",
            (status.value,),
        )
    return [row["session_id"] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Step 2 -- knowledge base
# ---------------------------------------------------------------------------


def get_document_hash(conn: Connection, doc_id: str) -> str | None:
    """Return the stored content hash for a document, or None if never indexed."""
    row = conn.execute(
        "SELECT content_hash FROM kb_documents WHERE doc_id = %s", (doc_id,)
    ).fetchone()
    return row["content_hash"] if row else None


def upsert_document(
    conn: Connection,
    doc_id: str,
    source_path: str,
    source_doc: str,
    content_hash: str,
    doc_version_date: date,
) -> None:
    """Record a KB document and the hash that decides whether to re-embed it."""
    conn.execute(
        """
        INSERT INTO kb_documents
            (doc_id, source_path, source_doc, content_hash, doc_version_date, indexed_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (doc_id) DO UPDATE SET
            source_path      = excluded.source_path,
            source_doc       = excluded.source_doc,
            content_hash     = excluded.content_hash,
            doc_version_date = excluded.doc_version_date,
            indexed_at       = excluded.indexed_at
        """,
        (doc_id, source_path, source_doc, content_hash, doc_version_date, utcnow()),
    )


def delete_chunks_for_doc(conn: Connection, doc_id: str) -> None:
    """Drop a document's chunks ahead of re-indexing it."""
    conn.execute("DELETE FROM kb_chunks WHERE doc_id = %s", (doc_id,))


def insert_chunks(
    conn: Connection,
    chunks: Sequence[KbChunk],
    embeddings: Sequence[Sequence[float]],
    embedding_model: str,
) -> int:
    """Write chunks with their unit-normalised embeddings. Lengths must match."""
    if len(chunks) != len(embeddings):
        raise ValueError(f"{len(chunks)} chunks but {len(embeddings)} embeddings")
    payload = [
        (
            chunk.chunk_id,
            chunk.doc_id,
            chunk.section,
            chunk.ordinal,
            chunk.text,
            normalise(vector),
            len(vector),
            embedding_model,
        )
        for chunk, vector in zip(chunks, embeddings)
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO kb_chunks
                (chunk_id, doc_id, section, ordinal, text,
                 embedding, embedding_dim, embedding_model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chunk_id) DO UPDATE SET
                doc_id          = excluded.doc_id,
                section         = excluded.section,
                ordinal         = excluded.ordinal,
                text            = excluded.text,
                embedding       = excluded.embedding,
                embedding_dim   = excluded.embedding_dim,
                embedding_model = excluded.embedding_model
            """,
            payload,
        )
    return len(payload)


def count_chunks(conn: Connection) -> int:
    """How many chunks are currently indexed."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM kb_chunks").fetchone()["n"])


def get_chunk(conn: Connection, chunk_id: str) -> dict[str, Any] | None:
    """Fetch a single chunk with its document provenance."""
    return conn.execute(
        """
        SELECT c.chunk_id, c.section, c.text, d.source_doc, d.doc_version_date
        FROM kb_chunks c
        JOIN kb_documents d ON d.doc_id = c.doc_id
        WHERE c.chunk_id = %s
        """,
        (chunk_id,),
    ).fetchone()


def search_chunks(conn: Connection, query_vector: Sequence[float], k: int) -> list[RetrievedChunk]:
    """Return the ``k`` chunks most similar to ``query_vector``, best first.

    This is an **exact** nearest-neighbour search. ``<#>`` is pgvector's
    negative inner product; stored chunks and the query are both unit length,
    so ``-(embedding <#> query)`` is exactly the cosine similarity. With no
    index on ``embedding`` PostgreSQL compares the query against every row, the
    same brute force the old numpy search did, just inside the database.

    An approximate index (HNSW) would be faster at large scale, but it can skip
    a true neighbour. Here that would turn a chunk that *supports* a claim into
    a NOT_FOUND verdict against Ava -- our retrieval error reported as hers.

    ``chunk_id`` breaks score ties so results are deterministic.

    Raises:
        RuntimeError: if the query's dimension does not match the stored index,
            which happens when the embedding model or AVA_EMBED_DIM changes
            without re-indexing.
    """
    if k <= 0:
        return []

    dims = conn.execute(
        "SELECT array_agg(DISTINCT embedding_dim) AS dims FROM kb_chunks"
    ).fetchone()["dims"]
    if not dims:
        return []

    query = normalise(query_vector)

    # Without these checks pgvector raises "different vector dimensions", which
    # names neither the cause nor the fix. This mistake happens the first time
    # anyone moves from the fake embedder to a real one.
    if len(dims) > 1:
        raise RuntimeError(
            f"The stored index mixes embedding dimensions {sorted(dims)}. Some documents "
            f"were indexed with a different embedding model or dimension -- re-run "
            f"'index-kb --force' to rebuild it."
        )
    if query.shape[0] != dims[0]:
        raise RuntimeError(
            f"Query embedding has {query.shape[0]} dimensions but the stored index has "
            f"{dims[0]}. The knowledge base was indexed with a different embedding model "
            f"or dimension -- re-run 'index-kb --force' to rebuild it."
        )

    rows = conn.execute(
        """
        SELECT c.chunk_id, c.text, c.section, d.source_doc,
               -(c.embedding <#> %(query)s) AS score
        FROM kb_chunks c
        JOIN kb_documents d ON d.doc_id = c.doc_id
        ORDER BY c.embedding <#> %(query)s, c.chunk_id
        LIMIT %(k)s
        """,
        {"query": query, "k": k},
    ).fetchall()

    return [
        RetrievedChunk(
            chunk_id=row["chunk_id"],
            text=row["text"],
            score=float(row["score"]),
            source_doc=row["source_doc"],
            section=row["section"],
            rank=rank,
        )
        for rank, row in enumerate(rows)
    ]


# ---------------------------------------------------------------------------
# Step 3 -- classifications
# ---------------------------------------------------------------------------


def save_classifications(conn: Connection, rows: Iterable[ClassifiedTurn]) -> int:
    """Persist turn classifications, overwriting any previous run."""
    now = utcnow()
    payload = [(r.session_id, r.pair_index, r.contains_claims, r.reason, now) for r in rows]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO turn_classifications
                (session_id, pair_index, contains_claims, reason, classified_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (session_id, pair_index) DO UPDATE SET
                contains_claims = excluded.contains_claims,
                reason          = excluded.reason,
                classified_at   = excluded.classified_at
            """,
            payload,
        )
    return len(payload)


def get_classifications(conn: Connection, session_id: str | None = None) -> list[ClassifiedTurn]:
    """Read back classifications, for one session or all of them."""
    base = """
        SELECT session_id, pair_index, contains_claims, reason
        FROM turn_classifications
    """
    if session_id:
        cur = conn.execute(base + " WHERE session_id = %s ORDER BY pair_index", (session_id,))
    else:
        cur = conn.execute(base + " ORDER BY session_id, pair_index")
    return [ClassifiedTurn(**row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Step 4 -- claims
# ---------------------------------------------------------------------------


def replace_claims_for_session(conn: Connection, session_id: str, claims: Sequence[Claim]) -> int:
    """Delete and rewrite a session's claims.

    The cascade also clears that session's retrievals and verdicts, which is
    correct: a claim whose wording changed is a different claim, and keeping an
    old verdict attached to new text is exactly the kind of quiet inconsistency
    an audit trail is supposed to prevent.
    """
    conn.execute("DELETE FROM claims WHERE session_id = %s", (session_id,))
    now = utcnow()
    payload = [
        (
            c.claim_id,
            c.session_id,
            c.pair_index,
            c.subject_tag,
            c.text,
            # Jsonb() marks the list as JSON; psycopg would otherwise try to
            # send a Python list as a PostgreSQL array.
            Jsonb(c.scope_derived_from),
            now,
        )
        for c in claims
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO claims
                (claim_id, session_id, pair_index, subject_tag,
                 claim_text, scope_derived_from, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            payload,
        )
    return len(payload)


def get_claims(conn: Connection, session_id: str | None = None) -> list[Claim]:
    """Read claims back, for one session or all of them."""
    base = """
        SELECT claim_id, session_id, pair_index, subject_tag, claim_text, scope_derived_from
        FROM claims
    """
    if session_id:
        cur = conn.execute(
            base + " WHERE session_id = %s ORDER BY pair_index, claim_id", (session_id,)
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
            # JSONB comes back already decoded into a Python list.
            scope_derived_from=row["scope_derived_from"],
        )
        for row in cur.fetchall()
    ]


# ---------------------------------------------------------------------------
# Step 5 -- retrievals
# ---------------------------------------------------------------------------


def save_retrievals(
    conn: Connection, claim_id: str, attempt: int, chunks: Sequence[RetrievedChunk]
) -> int:
    """Persist what retrieval returned for one claim on one attempt."""
    payload = [(claim_id, c.chunk_id, attempt, c.rank, c.score) for c in chunks]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO claim_retrievals (claim_id, chunk_id, attempt, rank, score)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (claim_id, chunk_id, attempt) DO UPDATE SET
                rank  = excluded.rank,
                score = excluded.score
            """,
            payload,
        )
    return len(payload)


def clear_retrievals(conn: Connection, claim_id: str) -> None:
    """Drop a claim's retrievals ahead of re-running step 5."""
    conn.execute("DELETE FROM claim_retrievals WHERE claim_id = %s", (claim_id,))


def get_retrievals(
    conn: Connection, claim_id: str, attempt: int | None = None
) -> list[RetrievedChunk]:
    """Read a claim's retrieved chunks, optionally for one attempt only."""
    query = """
        SELECT r.chunk_id, r.rank, r.score, c.text, c.section, d.source_doc
        FROM claim_retrievals r
        JOIN kb_chunks c    ON c.chunk_id = r.chunk_id
        JOIN kb_documents d ON d.doc_id   = c.doc_id
        WHERE r.claim_id = %s
    """
    params: list[Any] = [claim_id]
    if attempt is not None:
        query += " AND r.attempt = %s"
        params.append(attempt)
    query += " ORDER BY r.rank"
    return [RetrievedChunk(**row) for row in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# Step 6 -- verdicts
# ---------------------------------------------------------------------------


def save_verdict(conn: Connection, verdict: ClaimVerdict) -> None:
    """Persist one claim's verdict, replacing any earlier judgement of it."""
    conn.execute(
        """
        INSERT INTO verdicts
            (claim_id, verdict, tag, rationale, evidence_chunk_id, attempts, judged_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (claim_id) DO UPDATE SET
            verdict           = excluded.verdict,
            tag               = excluded.tag,
            rationale         = excluded.rationale,
            evidence_chunk_id = excluded.evidence_chunk_id,
            attempts          = excluded.attempts,
            judged_at         = excluded.judged_at
        """,
        (
            verdict.claim_id,
            verdict.verdict.value,
            verdict.tag.value if verdict.tag else None,
            verdict.rationale,
            verdict.evidence_chunk_id or None,
            verdict.attempts,
            utcnow(),
        ),
    )


# ---------------------------------------------------------------------------
# Step 7 -- reads over the derived views
# ---------------------------------------------------------------------------


def conversation_summary(conn: Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    """Per-conversation counts of flagged vs supported turns."""
    base = "SELECT * FROM v_conversation_summary"
    if session_id:
        cur = conn.execute(base + " WHERE session_id = %s", (session_id,))
    else:
        cur = conn.execute(base + " ORDER BY session_id")
    return cur.fetchall()


def turn_results(conn: Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    """Turn-level rollups, worst verdict per turn."""
    base = "SELECT * FROM v_turn_results"
    if session_id:
        cur = conn.execute(base + " WHERE session_id = %s ORDER BY pair_index", (session_id,))
    else:
        cur = conn.execute(base + " ORDER BY session_id, pair_index")
    return cur.fetchall()


def claim_results(
    conn: Connection,
    session_id: str | None = None,
    verdict: str | None = None,
    tag: str | None = None,
    subject_tag: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Filterable claim-level results -- what the frontend actually queries."""
    query = "SELECT * FROM v_claim_results WHERE TRUE"
    params: list[Any] = []
    if session_id:
        query += " AND session_id = %s"
        params.append(session_id)
    if verdict:
        query += " AND verdict = %s"
        params.append(verdict)
    if tag:
        query += " AND tag = %s"
        params.append(tag)
    if subject_tag:
        query += " AND subject_tag = %s"
        params.append(subject_tag)
    query += " ORDER BY session_id, pair_index, claim_id LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    return conn.execute(query, params).fetchall()


def get_claim_result(conn: Connection, claim_id: str) -> dict[str, Any] | None:
    """One claim with its verdict and the evidence text behind it."""
    return conn.execute("SELECT * FROM v_claim_results WHERE claim_id = %s", (claim_id,)).fetchone()


def verdict_stats(conn: Connection) -> dict[str, Any]:
    """Counts by verdict and by tag, for a dashboard header."""
    by_verdict = conn.execute("""
        SELECT verdict, COUNT(*) AS n
        FROM verdicts
        GROUP BY verdict
        ORDER BY verdict
        """).fetchall()
    by_tag = conn.execute("""
        SELECT tag, COUNT(*) AS n
        FROM verdicts
        WHERE tag IS NOT NULL
        GROUP BY tag
        ORDER BY tag
        """).fetchall()
    totals = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM conversations WHERE status = 'accepted') AS conversations,
            (SELECT COUNT(*) FROM turns)      AS turns,
            (SELECT COUNT(*) FROM claims)     AS claims,
            (SELECT COUNT(*) FROM kb_chunks)  AS kb_chunks
        """).fetchone()
    return {
        "totals": totals,
        "by_verdict": {row["verdict"]: row["n"] for row in by_verdict},
        "by_tag": {row["tag"]: row["n"] for row in by_tag},
    }

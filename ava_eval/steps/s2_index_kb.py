"""Step 2 — extract, structure, chunk, embed and store the knowledge base.

This runs on a different cadence to everything else: the knowledge base changes
when TTU edits the document, not when you evaluate a batch of transcripts. So it
is a separate command, and it skips the document when its content has not changed.

The knowledge base is one document, so re-indexing replaces the whole index
rather than patching it. That also means a renamed or replaced file can never
leave the previous file's chunks behind, still searchable.

Embedding happens before the database is touched. It is hundreds of network
calls, and if one fails for good, the existing index stays exactly as it was
instead of being left half-rewritten.

The index is built independently of whatever chunking Ava's own retrieval uses
internally. Phase 1 asks "is this claim true according to the KB's content",
not "did Ava's retrieval do a good job", so copying Ava's chunk boundaries would
only inherit Ava's blind spots without telling us anything about its retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..config import settings
from ..db import store
from ..kb.chunk import chunk_sections, slugify
from ..kb.extract import content_hash, extract_docx, resolve_version_date
from ..kb.structure import parse_structure
from ..llm.client import LlmError, embed_texts
from ..models import ChunkKind


@dataclass
class IndexReport:
    """What one indexing run did."""

    source_doc: str
    skipped: bool = False
    topic_count: int = 0
    grouped_topic_count: int = 0
    qa_chunks: int = 0
    reference_chunks: int = 0
    unmarked_topics: list[str] = field(default_factory=list)
    unmatched_catalog_entries: list[tuple[str, str]] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        """Chunks produced from the document."""
        return self.qa_chunks + self.reference_chunks


def index_knowledge_base(
    kb_file: Path, force: bool = False, database_url: str | None = None
) -> IndexReport:
    """Index the knowledge base document, replacing any previous index.

    Args:
        kb_file: The .docx knowledge base.
        force: Re-index even when the content hash is unchanged.
        database_url: Override the configured database.

    Returns:
        A report of what was parsed and whether it was written or skipped.

    Raises:
        FileNotFoundError: if ``kb_file`` does not exist.
        ValueError: if the file is not a readable .docx, has no text or no topic
            markers, or declares a version date that is not an ISO date.
        RuntimeError: if embedding fails. The existing index is left untouched.
    """
    paragraphs = extract_docx(kb_file)
    if not paragraphs:
        raise ValueError(f"{kb_file.name} extracted no text -- check the document")

    # The column is a DATE, so a value like "Fall 2025" in kb_meta.json would be
    # rejected by PostgreSQL mid-transaction. Checking here names the fix instead.
    raw_version_date = resolve_version_date(kb_file)
    try:
        version_date = date.fromisoformat(raw_version_date)
    except ValueError as exc:
        raise ValueError(
            f"doc_version_date {raw_version_date!r} is not an ISO date (YYYY-MM-DD) -- "
            f"fix it in kb_meta.json"
        ) from exc

    structure = parse_structure(paragraphs)
    doc_id = slugify(kb_file.stem)
    chunks = chunk_sections(
        structure.sections,
        doc_id=doc_id,
        target_chars=settings.chunk_target_chars,
        overlap_ratio=settings.chunk_overlap_ratio,
    )
    if not chunks:
        raise ValueError(f"{kb_file.name} produced no chunks")

    report = IndexReport(
        source_doc=kb_file.name,
        topic_count=len(structure.topics),
        grouped_topic_count=len(structure.groups),
        qa_chunks=sum(1 for chunk in chunks if chunk.kind is ChunkKind.QA),
        reference_chunks=sum(1 for chunk in chunks if chunk.kind is ChunkKind.REFERENCE),
        unmarked_topics=structure.unmarked_topics,
        unmatched_catalog_entries=structure.unmatched_catalog_entries,
    )

    digest = content_hash(paragraphs)
    with store.connect(database_url) as conn:
        if not force and store.current_kb_hash(conn) == digest:
            report.skipped = True
            return report

    try:
        # Chunks are documents, not queries -- that distinction selects the
        # embedding prefix, and getting it backwards degrades every search
        # without raising anything. The section header is the document title.
        embeddings = embed_texts(
            [chunk.text for chunk in chunks],
            is_query=False,
            titles=[chunk.section for chunk in chunks],
        )
    except (LlmError, ValueError) as exc:
        raise RuntimeError(
            f"embedding failed, the existing index was left unchanged: {exc}"
        ) from exc

    with store.connect(database_url) as conn:
        store.clear_kb(conn)
        store.upsert_document(
            conn,
            doc_id=doc_id,
            source_path=str(kb_file),
            source_doc=kb_file.name,
            content_hash=digest,
            doc_version_date=version_date,
        )
        store.insert_chunks(conn, chunks, embeddings, settings.embed_model)

    return report

"""Step 2 — extract, chunk, embed and store the knowledge base.

This runs on a different cadence to everything else: the knowledge base changes
when TTU edits a document, not when you evaluate a batch of transcripts. So it
is a separate command, and it skips documents whose content has not changed.

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
from ..kb.chunk import chunk_blocks, slugify
from ..kb.extract import content_hash, extract_docx, resolve_version_date
from ..llm.client import embed_texts


@dataclass
class IndexReport:
    """What one indexing run did."""

    indexed: list[tuple[str, int]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        """Chunks written in this run."""
        return sum(count for _, count in self.indexed)


def index_knowledge_base(
    kb_dir: Path, force: bool = False, database_url: str | None = None
) -> IndexReport:
    """Index every .docx in ``kb_dir``.

    Args:
        kb_dir: Directory holding the knowledge base documents.
        force: Re-index even documents whose content hash is unchanged.
        database_url: Override the configured database.

    Returns:
        A report naming what was indexed, skipped, and what failed.

    Raises:
        FileNotFoundError: if ``kb_dir`` does not exist.
    """
    if not kb_dir.exists():
        raise FileNotFoundError(f"No such knowledge base directory: {kb_dir}")

    # Word writes lock files named ~$foo.docx while a document is open. They are
    # not readable documents and would otherwise show up as extraction failures.
    documents = sorted(p for p in kb_dir.glob("*.docx") if not p.name.startswith("~$"))
    report = IndexReport()

    with store.connect(database_url) as conn:
        for path in documents:
            doc_id = slugify(path.stem)
            try:
                blocks = extract_docx(path)
            except (FileNotFoundError, ValueError, OSError) as exc:
                report.failed.append((path.name, str(exc)))
                continue

            # The column is a DATE, so a value like "Fall 2025" in kb_meta.json
            # would be rejected by PostgreSQL -- and a failed statement aborts
            # the whole indexing transaction. Checking here turns that into one
            # failed document with a reason, like every other failure above.
            raw_version_date = resolve_version_date(path, kb_dir)
            try:
                version_date = date.fromisoformat(raw_version_date)
            except ValueError:
                report.failed.append(
                    (
                        path.name,
                        f"doc_version_date {raw_version_date!r} is not an ISO date "
                        f"(YYYY-MM-DD) -- fix it in kb_meta.json",
                    )
                )
                continue

            if not blocks:
                report.failed.append((path.name, "extracted no text -- check the document"))
                continue

            digest = content_hash(blocks)
            if not force and store.get_document_hash(conn, doc_id) == digest:
                report.skipped.append(path.name)
                continue

            chunks = chunk_blocks(
                blocks,
                doc_id=doc_id,
                target_chars=settings.chunk_target_chars,
                overlap_ratio=settings.chunk_overlap_ratio,
            )
            if not chunks:
                report.failed.append((path.name, "produced no chunks"))
                continue

            try:
                # Chunks are documents, not queries -- that distinction selects
                # the embedding prefix, and getting it backwards degrades every
                # search against this document without raising anything.
                embeddings = embed_texts(
                    [c.text for c in chunks],
                    is_query=False,
                    titles=[c.section for c in chunks],
                )
            except Exception as exc:
                report.failed.append((path.name, f"embedding failed: {exc}"))
                continue

            store.upsert_document(
                conn,
                doc_id=doc_id,
                source_path=str(path),
                source_doc=path.name,
                content_hash=digest,
                doc_version_date=version_date,
            )
            store.delete_chunks_for_doc(conn, doc_id)
            store.insert_chunks(conn, chunks, embeddings, settings.embed_model)
            report.indexed.append((path.name, len(chunks)))

    return report

"""Group extracted blocks into retrievable chunks.

Chunking is semantic rather than fixed-width: chunks follow the document's own
section boundaries and only ever split between paragraphs. The reason is the
same scoping problem the claim decomposition step deals with — a fixed 500-token
window will happily cut between "minimum GPA of 2.0" and "for the Online BBA",
leaving two chunks that are each individually misleading.

Overlap exists for the same reason at the seam: a fact stated across the last
sentence of one chunk and the first of the next would otherwise belong to
neither, so each chunk carries the tail of its predecessor.
"""

from __future__ import annotations

import re
from itertools import groupby

from ..models import KbBlock, KbChunk

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def slugify(text: str, max_length: int = 40) -> str:
    """Turn arbitrary text into a short, filesystem- and id-safe slug."""
    slug = _SLUG_RE.sub("_", text.lower()).strip("_")
    return slug[:max_length].strip("_") or "untitled"


def _split_long_block(text: str, limit: int) -> list[str]:
    """Fallback for a single paragraph bigger than a whole chunk.

    Splitting mid-paragraph is the thing this module exists to avoid, so this
    only runs when a paragraph is oversized on its own and there is no
    structural boundary left to use. Sentence boundaries are the least-bad
    remaining option.
    """
    sentences = _SENTENCE_RE.split(text)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces or [text]


def _overlap_tail(texts: list[str], budget: int) -> list[str]:
    """Take whole trailing paragraphs from a chunk, up to a character budget."""
    tail: list[str] = []
    used = 0
    for text in reversed(texts):
        if tail and used + len(text) > budget:
            break
        tail.insert(0, text)
        used += len(text)
        if used >= budget:
            break
    return tail


def chunk_blocks(
    blocks: list[KbBlock],
    doc_id: str,
    target_chars: int = 1200,
    overlap_ratio: float = 0.15,
) -> list[KbChunk]:
    """Group blocks into chunks, respecting section boundaries.

    Args:
        blocks: Extracted blocks in document order.
        doc_id: Slug identifying the source document, used to build chunk ids.
        target_chars: Soft ceiling on chunk size. Chunks close rather than
            exceed it, except where a single paragraph is already larger.
        overlap_ratio: Fraction of ``target_chars`` carried over from the
            previous chunk, as whole paragraphs.

    Returns:
        Chunks with stable, human-readable ids of the form
        ``{doc_id}__{section_slug}__{ordinal:03d}``.
    """
    if not blocks:
        return []

    overlap_budget = int(target_chars * overlap_ratio)
    chunks: list[KbChunk] = []
    ordinal = 0

    # Consecutive blocks sharing a heading path form one section. Chunks never
    # straddle two sections, so a chunk always has one honest citation.
    for heading_path, group in groupby(blocks, key=lambda b: tuple(b.heading_path)):
        section = " > ".join(heading_path) if heading_path else "(no section)"
        section_slug = slugify(section)

        pieces: list[str] = []
        for block in group:
            if len(block.text) > target_chars:
                pieces.extend(_split_long_block(block.text, target_chars))
            else:
                pieces.append(block.text)

        current: list[str] = []
        current_len = 0

        def flush() -> list[str]:
            """Emit the accumulated paragraphs as a chunk, return its overlap tail."""
            nonlocal ordinal
            if not current:
                return []
            chunks.append(
                KbChunk(
                    chunk_id=f"{doc_id}__{section_slug}__{ordinal:03d}",
                    doc_id=doc_id,
                    section=section,
                    ordinal=ordinal,
                    text="\n\n".join(current),
                )
            )
            ordinal += 1
            return _overlap_tail(current, overlap_budget) if overlap_budget > 0 else []

        for piece in pieces:
            if current and current_len + len(piece) > target_chars:
                carry = flush()
                current = list(carry)
                current_len = sum(len(item) for item in current)
            current.append(piece)
            current_len += len(piece)

        flush()
        current = []
        current_len = 0

    return chunks

"""Turn the knowledge base's labelled sections into retrievable chunks.

Boundaries come from the document's own structure (``structure.py``), never a
fixed-width window. A fixed window cuts between "minimum GPA of 2.0" and "for
the Online BBA", or glues the end of one program's page onto the start of the
next, leaving chunks that are each individually misleading.

The two kinds of section are treated differently:

* A **question and its answer** is exactly one chunk. The judge's citation then
  points at one answer, and no unrelated answer rides along into its prompt.
* **Reference content** (a program's overview, prerequisites, deadlines) is
  labelled for layout rather than meaning, so its small pieces are packed
  together within one topic, up to the size target. A 71-character "Late Fall"
  deadline would otherwise spend a whole retrieval slot on its own.

Every chunk begins with its ``Group > Topic`` header. That line tells the
embedder and the judge which program a passage belongs to. Dozens of program
pages carry word-for-word identical deadline text, and without the header those
chunks are indistinguishable.

Overlap exists only between the pieces of one oversized section. Carrying text
across two separate answers would put one question's tail inside another
question's chunk.
"""

from __future__ import annotations

import re
from itertools import groupby

from ..models import ChunkKind, KbChunk, KbSection

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def slugify(text: str, max_length: int = 40) -> str:
    """Turn arbitrary text into a short, filesystem- and id-safe slug."""
    slug = _SLUG_RE.sub("_", text.lower()).strip("_")
    return slug[:max_length].strip("_") or "untitled"


def topic_header(section: KbSection) -> str:
    """The ``Group > Topic`` line every chunk of this section's topic starts with."""
    return f"{section.group} > {section.topic}" if section.group else section.topic


def _split_long_block(text: str, limit: int) -> list[str]:
    """Fallback for a single line bigger than a whole chunk.

    Splitting mid-paragraph is the thing this module exists to avoid, so this
    only runs when one line is oversized on its own and there is no structural
    boundary left to use. Sentence boundaries are the least-bad remaining option.
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
    """Take whole trailing lines from a piece, up to a character budget."""
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


def _section_pieces(section: KbSection, target_chars: int, overlap_budget: int) -> list[str]:
    """Render a section as one body, or as several parts if it exceeds the target.

    Each body starts with the section's label path, so a part of a long Overview
    still says what it is part of.
    """
    label = " > ".join(section.label_path)
    whole = "\n".join([label, *section.lines])
    if len(whole) <= target_chars:
        return [whole]

    lines: list[str] = []
    for line in section.lines:
        lines.extend(_split_long_block(line, target_chars) if len(line) > target_chars else [line])

    pieces: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current and current_len + len(line) > target_chars:
            pieces.append(current)
            current = _overlap_tail(current, overlap_budget) if overlap_budget > 0 else []
            current_len = sum(len(item) for item in current)
            if current_len + len(line) > target_chars:
                # The carried tail plus this line would already overflow, and a
                # piece that is nothing but repeated overlap tells no one anything.
                current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        pieces.append(current)

    total = len(pieces)
    return [
        "\n".join([f"{label} (part {number}/{total})", *piece])
        for number, piece in enumerate(pieces, start=1)
    ]


def chunk_sections(
    sections: list[KbSection],
    doc_id: str,
    target_chars: int = 1200,
    overlap_ratio: float = 0.15,
) -> list[KbChunk]:
    """Group labelled sections into chunks.

    Args:
        sections: Output of ``parse_structure``, in document order.
        doc_id: The source document's id, stored on every chunk.
        target_chars: Soft ceiling on a chunk's body. Reference packs close
            before exceeding it; a larger section is split into parts.
        overlap_ratio: Fraction of ``target_chars`` carried between the parts of
            one oversized section. Never applied across sections.

    Returns:
        Chunks with short positional ids: ``t{topic:02d}.q{n:02d}`` for a question
        and ``t{topic:02d}.r{n:02d}`` for reference content, numbered from 1 within
        each topic. Short ids are easy for the judge to copy back exactly.
    """
    overlap_budget = int(target_chars * overlap_ratio)
    chunks: list[KbChunk] = []

    for topic_no, topic_iter in groupby(sections, key=lambda section: section.topic_no):
        topic_sections = list(topic_iter)
        first = topic_sections[0]
        header = topic_header(first)
        counters = {ChunkKind.QA: 0, ChunkKind.REFERENCE: 0}
        pack_labels: list[str] = []
        pack_bodies: list[str] = []
        pack_len = 0

        def emit(kind: ChunkKind, labels: list[str], bodies: list[str]) -> None:
            """Append one chunk for this topic."""
            counters[kind] += 1
            letter = "q" if kind is ChunkKind.QA else "r"
            chunks.append(
                KbChunk(
                    chunk_id=f"t{topic_no:02d}.{letter}{counters[kind]:02d}",
                    doc_id=doc_id,
                    section=f"{header} > {'; '.join(labels)}",
                    ordinal=len(chunks),
                    text="\n\n".join([header, *bodies]),
                    kind=kind,
                    topic_no=topic_no,
                    group=first.group,
                    topic=first.topic,
                )
            )

        def flush_pack() -> None:
            """Emit the packed reference sections, if any, as one chunk."""
            nonlocal pack_labels, pack_bodies, pack_len
            if pack_bodies:
                emit(ChunkKind.REFERENCE, pack_labels, pack_bodies)
            pack_labels, pack_bodies, pack_len = [], [], 0

        for section in topic_sections:
            pieces = _section_pieces(section, target_chars, overlap_budget)

            if section.kind is ChunkKind.QA or len(pieces) > 1:
                # Questions and oversized sections stand alone. Flushing first
                # keeps chunk order identical to document order.
                flush_pack()
                for piece in pieces:
                    emit(section.kind, [piece.split("\n", 1)[0]], [piece])
                continue

            body = pieces[0]
            if pack_bodies and pack_len + len(body) > target_chars:
                flush_pack()
            pack_labels.append(" > ".join(section.label_path))
            pack_bodies.append(body)
            pack_len += len(body)

        flush_pack()

    return chunks

"""Pull clean, structured text out of .docx knowledge base documents.

This is the step where quality problems are invisible rather than loud. A bad
extractor does not crash — it returns *less* text, and the missing content shows
up much later as a pile of NOT_FOUND verdicts that look exactly like genuine
gaps in TTU's knowledge base. So two things matter here:

* **Tables are extracted, not skipped.** Policy documents put GPA minimums,
  deadlines and credit requirements in tables. The naive path through python-docx
  (``for p in doc.paragraphs``) silently returns none of them.
* **Headings are preserved.** They become chunk boundaries in the next step and
  the citation shown to TTU staff next to a flag.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from ..models import KbBlock

_HEADING_RE = re.compile(r"^Heading (\d+)$")


def _iter_block_items(document: Document):
    """Yield paragraphs and tables in the order they appear in the document.

    python-docx exposes ``document.paragraphs`` and ``document.tables`` as two
    separate lists, which loses their interleaving — a table would end up
    detached from the heading it sits under. Walking the underlying XML body
    keeps document order intact.
    """
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _heading_level(paragraph: Paragraph) -> int | None:
    """Return the heading level of a paragraph, or None if it is body text."""
    style_name = (paragraph.style.name or "") if paragraph.style else ""
    if style_name == "Title":
        return 1
    match = _HEADING_RE.match(style_name)
    return int(match.group(1)) if match else None


def _flatten_table(table: Table) -> list[str]:
    """Turn a table into one text line per row.

    When the first row looks like a header, each cell is labelled with its
    column name, so a row survives as ``Program: Online BBA | Minimum GPA: 2.0``
    rather than a bare ``Online BBA | 2.0``. The label is what makes the row
    retrievable — a search for "minimum GPA for the Online BBA" has to have
    something to match against.
    """
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return []

    header, *body = rows
    header_is_labels = len(rows) > 1 and all(cell for cell in header)

    if not header_is_labels:
        return [" | ".join(cell for cell in row if cell) for row in rows]

    lines: list[str] = []
    for row in body:
        parts = [f"{label}: {value}" for label, value in zip(header, row) if value and label]
        if parts:
            lines.append(" | ".join(parts))
    return lines


def extract_docx(path: Path) -> list[KbBlock]:
    """Extract a .docx into ordered blocks, each tagged with its heading path.

    Args:
        path: The .docx file to read.

    Returns:
        Blocks in document order. A block is one paragraph, or one flattened
        table row.

    Raises:
        FileNotFoundError: if the path does not exist.
        ValueError: if python-docx cannot open the file.
    """
    if not path.exists():
        raise FileNotFoundError(f"No such document: {path}")

    try:
        document = Document(str(path))
    except Exception as exc:
        raise ValueError(f"Could not read {path.name} as a .docx file: {exc}") from exc

    blocks: list[KbBlock] = []
    heading_stack: list[str] = []
    ordinal = 0

    for item in _iter_block_items(document):
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue

            level = _heading_level(item)
            if level is not None:
                # Truncate the stack to this heading's depth, then push it, so
                # "3. Admissions > 3.2 GPA" survives but a sibling heading
                # replaces its predecessor rather than nesting under it.
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(text)
                continue

            blocks.append(KbBlock(text=text, heading_path=list(heading_stack), ordinal=ordinal))
            ordinal += 1

        elif isinstance(item, Table):
            for line in _flatten_table(item):
                blocks.append(KbBlock(text=line, heading_path=list(heading_stack), ordinal=ordinal))
                ordinal += 1

    return blocks


def content_hash(blocks: list[KbBlock]) -> str:
    """Hash the extracted content so unchanged documents can be skipped.

    Hashing the *extracted text* rather than the raw file is deliberate: opening
    and re-saving a .docx changes the file bytes without changing a word of the
    content, and re-embedding an entire document because someone opened it is a
    waste of quota.
    """
    digest = hashlib.sha256()
    for block in blocks:
        digest.update(block.text.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(" > ".join(block.heading_path).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def resolve_version_date(path: Path, kb_dir: Path) -> str:
    """Work out the document's version date.

    Prefers an explicit entry in ``kb_meta.json`` next to the documents, since a
    file modification time says when someone touched the file, not when the
    policy it describes took effect. Falls back to the mtime when no entry
    exists, which is at least honest about being an approximation.
    """
    meta_path = kb_dir / "kb_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            declared = meta.get(path.name, {}).get("doc_version_date")
            if declared:
                return str(declared)
        except (json.JSONDecodeError, OSError, AttributeError):
            # A malformed sidecar should not stop indexing; the mtime fallback
            # below is good enough to keep going.
            pass

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return mtime.date().isoformat()

"""Pull clean text and formatting signals out of the knowledge base .docx.

This is the step where quality problems are invisible rather than loud. A bad
extractor does not crash — it returns *less* text, and the missing content shows
up much later as a pile of NOT_FOUND verdicts that look exactly like genuine
gaps in TTU's knowledge base. So two things matter here:

* **Tables are extracted, not skipped.** Policy documents put GPA minimums,
  deadlines and credit requirements in tables. The naive path through python-docx
  (``for p in doc.paragraphs``) silently returns none of them.
* **Formatting is kept, not just text.** TTU's document has no Word heading
  styles. Its structure lives in bold paragraphs ("If asked questions about X,
  answer with", then bold questions and labels), which ``structure.py`` reads.
  So every paragraph records whether it is bold and whether it is a list item.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from ..models import KbParagraph

_LIST_STYLE = "List Paragraph"


def _iter_block_items(document: Document):
    """Yield paragraphs and tables in the order they appear in the document.

    python-docx exposes ``document.paragraphs`` and ``document.tables`` as two
    separate lists, which loses their interleaving — a table would end up
    detached from the question it answers. Walking the underlying XML body
    keeps document order intact.
    """
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _is_bold(paragraph: Paragraph) -> bool:
    """True when every visible run in the paragraph renders bold.

    ``run.bold`` has three states: True/False when set on the run itself, None
    when inherited. Inherited bold is resolved through the run's character style
    and then the paragraph style, so a bold paragraph style still counts.

    A paragraph with only *some* bold runs ("**Full-time:** 6 credit hours") is
    body text with an emphasised lead-in, not a label, so it returns False.
    """
    runs = [run for run in paragraph.runs if run.text.strip()]
    if not runs:
        return False

    paragraph_bold = bool(paragraph.style is not None and paragraph.style.font.bold)
    for run in runs:
        bold = run.bold
        if bold is None and run.style is not None:
            bold = run.style.font.bold
        if bold is None:
            bold = paragraph_bold
        if not bold:
            return False
    return True


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


def extract_docx(path: Path) -> list[KbParagraph]:
    """Extract a .docx into ordered paragraphs with their formatting signals.

    Args:
        path: The .docx file to read.

    Returns:
        Non-empty paragraphs in document order. A table contributes one plain
        (non-bold) paragraph per flattened row.

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

    paragraphs: list[KbParagraph] = []

    for item in _iter_block_items(document):
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            style_name = item.style.name if item.style is not None else ""
            paragraphs.append(
                KbParagraph(
                    text=text,
                    bold=_is_bold(item),
                    is_list=style_name == _LIST_STYLE,
                    ordinal=len(paragraphs),
                )
            )

        elif isinstance(item, Table):
            for line in _flatten_table(item):
                paragraphs.append(
                    KbParagraph(text=line, bold=False, is_list=False, ordinal=len(paragraphs))
                )

    return paragraphs


def content_hash(paragraphs: list[KbParagraph]) -> str:
    """Hash the extracted content so an unchanged document can be skipped.

    Hashing the *extracted text* rather than the raw file is deliberate: opening
    and re-saving a .docx changes the file bytes without changing a word of the
    content, and re-embedding the whole knowledge base because someone opened it
    is a waste of quota.

    The bold and list flags are part of the hash because they decide chunk
    boundaries: bolding a line turns it into a label and moves chunks around,
    even though no word changed.
    """
    digest = hashlib.sha256()
    for paragraph in paragraphs:
        digest.update(paragraph.text.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(b"B" if paragraph.bold else b"-")
        digest.update(b"L" if paragraph.is_list else b"-")
        digest.update(b"\x1e")
    return digest.hexdigest()


def resolve_version_date(path: Path) -> str:
    """Work out the document's version date.

    Prefers an explicit entry in ``kb_meta.json`` next to the document, since a
    file modification time says when someone touched the file, not when the
    policy it describes took effect. Falls back to the mtime when no entry
    exists, which is at least honest about being an approximation.
    """
    meta_path = path.parent / "kb_meta.json"
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

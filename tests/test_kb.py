"""Extraction and chunking tests.

Extraction failures are silent — a dropped table does not raise, it just makes
the knowledge base quietly smaller, and the missing content resurfaces later as
NOT_FOUND verdicts that look like genuine gaps. So these tests assert that
content *survives*, not merely that the functions run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document

from ava_eval.kb.chunk import chunk_blocks, slugify
from ava_eval.kb.extract import content_hash, extract_docx, resolve_version_date
from ava_eval.models import KbBlock


@pytest.fixture
def sample_docx(tmp_path: Path) -> Path:
    """A document with headings, body text and a table."""
    doc = Document()
    doc.add_heading("Admissions", level=1)
    doc.add_heading("GPA", level=2)
    doc.add_paragraph("The minimum GPA is 2.0 on a 4.0 scale.")

    table = doc.add_table(rows=1, cols=2)
    header = table.rows[0].cells
    header[0].text = "Program"
    header[1].text = "Minimum GPA"
    row = table.add_row().cells
    row[0].text = "Online BBA"
    row[1].text = "2.0"

    doc.add_heading("Transcripts", level=2)
    doc.add_paragraph("Official transcripts are required.")

    path = tmp_path / "policy.docx"
    doc.save(str(path))
    return path


def test_extracts_paragraphs_with_heading_paths(sample_docx: Path) -> None:
    blocks = extract_docx(sample_docx)
    gpa = next(b for b in blocks if "minimum GPA" in b.text)
    assert gpa.heading_path == ["Admissions", "GPA"]


def test_sibling_headings_replace_rather_than_nest(sample_docx: Path) -> None:
    blocks = extract_docx(sample_docx)
    transcripts = next(b for b in blocks if "Official transcripts" in b.text)
    assert transcripts.heading_path == ["Admissions", "Transcripts"]


def test_table_rows_are_extracted_and_labelled(sample_docx: Path) -> None:
    """The failure this guards against: tables silently vanishing."""
    blocks = extract_docx(sample_docx)
    table_rows = [b.text for b in blocks if "Online BBA" in b.text]
    assert table_rows, "table content was dropped during extraction"
    assert table_rows[0] == "Program: Online BBA | Minimum GPA: 2.0"


def test_table_rows_inherit_the_surrounding_heading(sample_docx: Path) -> None:
    blocks = extract_docx(sample_docx)
    row = next(b for b in blocks if "Online BBA" in b.text)
    assert row.heading_path == ["Admissions", "GPA"]


def test_content_hash_is_stable_and_sensitive(sample_docx: Path) -> None:
    blocks = extract_docx(sample_docx)
    assert content_hash(blocks) == content_hash(extract_docx(sample_docx))

    changed = list(blocks)
    changed[0] = KbBlock(
        text=blocks[0].text + " Updated.",
        heading_path=blocks[0].heading_path,
        ordinal=blocks[0].ordinal,
    )
    assert content_hash(changed) != content_hash(blocks)


def test_missing_document_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        extract_docx(tmp_path / "nope.docx")


def test_non_docx_raises_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "fake.docx"
    path.write_text("this is not a docx", encoding="utf-8")
    with pytest.raises(ValueError, match="docx"):
        extract_docx(path)


def test_version_date_prefers_the_sidecar(tmp_path: Path, sample_docx: Path) -> None:
    (sample_docx.parent / "kb_meta.json").write_text(
        '{"policy.docx": {"doc_version_date": "2026-01-15"}}', encoding="utf-8"
    )
    assert resolve_version_date(sample_docx, sample_docx.parent) == "2026-01-15"


def test_version_date_falls_back_to_mtime(sample_docx: Path) -> None:
    assert resolve_version_date(sample_docx, sample_docx.parent).count("-") == 2


def test_malformed_sidecar_does_not_stop_indexing(sample_docx: Path) -> None:
    (sample_docx.parent / "kb_meta.json").write_text("{broken", encoding="utf-8")
    assert resolve_version_date(sample_docx, sample_docx.parent).count("-") == 2


# --- chunking -------------------------------------------------------------


def _blocks(section: list[str], texts: list[str]) -> list[KbBlock]:
    """Build blocks that all share one heading path."""
    return [KbBlock(text=t, heading_path=section, ordinal=i) for i, t in enumerate(texts)]


def test_chunks_never_straddle_sections() -> None:
    """A chunk spanning two sections could not be honestly cited."""
    blocks = _blocks(["A"], ["short one"]) + _blocks(["B"], ["short two"])
    chunks = chunk_blocks(blocks, doc_id="doc", target_chars=5000)
    assert len(chunks) == 2
    assert {c.section for c in chunks} == {"A", "B"}


def test_respects_the_size_target() -> None:
    blocks = _blocks(["A"], ["x" * 300] * 10)
    chunks = chunk_blocks(blocks, doc_id="doc", target_chars=1000, overlap_ratio=0.0)
    assert len(chunks) > 1
    # Chunks close before exceeding the target rather than after it, so the
    # only slack is the single paragraph that tipped it over.
    assert all(len(c.text) <= 1000 + 300 for c in chunks)


def test_overlap_carries_content_between_adjacent_chunks() -> None:
    """A fact split across a boundary would otherwise belong to no chunk."""
    paragraphs = [f"Paragraph number {i}. " + "y" * 280 for i in range(8)]
    chunks = chunk_blocks(
        _blocks(["A"], paragraphs), doc_id="doc", target_chars=1000, overlap_ratio=0.3
    )
    assert len(chunks) > 1
    first_tail = chunks[0].text.split("\n\n")[-1]
    assert first_tail in chunks[1].text


def test_zero_overlap_produces_no_repetition() -> None:
    paragraphs = [f"Paragraph {i}. " + "y" * 280 for i in range(8)]
    chunks = chunk_blocks(
        _blocks(["A"], paragraphs), doc_id="doc", target_chars=1000, overlap_ratio=0.0
    )
    seen = [line for c in chunks for line in c.text.split("\n\n")]
    assert len(seen) == len(set(seen))


def test_oversized_paragraph_is_split_on_sentences() -> None:
    """The documented fallback when there is no structure left to use."""
    giant = " ".join(f"Sentence number {i} goes here." for i in range(200))
    chunks = chunk_blocks(
        _blocks(["A"], [giant]), doc_id="doc", target_chars=500, overlap_ratio=0.0
    )
    assert len(chunks) > 1
    assert all(c.text.strip().endswith(".") for c in chunks)


def test_chunk_ids_are_unique_and_readable() -> None:
    blocks = _blocks(["Admissions", "GPA"], ["a" * 400] * 6)
    chunks = chunk_blocks(blocks, doc_id="policy", target_chars=500, overlap_ratio=0.0)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("policy__admissions_gpa__") for i in ids)


def test_empty_input_produces_no_chunks() -> None:
    assert chunk_blocks([], doc_id="doc") == []


def test_slugify_handles_punctuation_and_length() -> None:
    assert slugify("3.2 GPA & Transcript Requirements!") == "3_2_gpa_transcript_requirements"
    assert len(slugify("x" * 200)) <= 40
    assert slugify("!!!") == "untitled"

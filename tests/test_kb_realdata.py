"""Structure and chunking checks against TTU's real knowledge base document.

The rules in ``kb/structure.py`` were written against ``realdata/online.docx``,
and they depend on its formatting. These tests pin the properties that matter
on the real thing, so a change to the rules, or a new version of the document,
that breaks them fails loudly instead of quietly degrading retrieval.

The document is not committed, so the whole module is skipped without it.
"""

from __future__ import annotations

import re
import unicodedata

import pytest

from ava_eval.config import PROJECT_ROOT
from ava_eval.kb.chunk import chunk_sections
from ava_eval.kb.extract import extract_docx
from ava_eval.kb.structure import clean_text, parse_structure
from ava_eval.models import ChunkKind

REALDATA = PROJECT_ROOT / "realdata" / "online.docx"

pytestmark = pytest.mark.skipif(not REALDATA.exists(), reason="realdata/online.docx is not present")

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_BACHELORS = "What Bachelor's degrees do you offer through Texas Tech Online"
_RECURRING = ("Related Programs", "Application Deadlines", "Please note", "Key Courses")


def _canon(text: str) -> str:
    """Compare text loosely: TOC copies and labels differ in dashes, quotes and case."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[‒-―]", "-", text).replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).lower()


@pytest.fixture(scope="module")
def parsed():
    """Extract, parse and chunk the real document once for the whole module."""
    paragraphs = extract_docx(REALDATA)
    structure = parse_structure(paragraphs)
    chunks = chunk_sections(structure.sections, doc_id="online")
    return paragraphs, structure, chunks


def test_every_topic_is_found(parsed) -> None:
    """69 marked topics, plus 4 program pages pasted in with only a bold title."""
    _, structure, _ = parsed
    assert len(structure.topics) == 73
    assert sorted(structure.unmarked_topics) == [
        "Online Bachelor of Arts in Criminology",
        "Online Bachelor of Arts in Human Resources Development",
        "Online Bachelor of Arts in Interdisciplinary Studies",
        "Online Bachelor of Science in Computing Applications",
    ]
    assert structure.unmatched_catalog_entries == []


def test_a_pasted_program_page_is_not_cited_as_its_neighbour(parsed) -> None:
    """The computing applications page sits inside communication studies in the .docx."""
    _, _, chunks = parsed
    computing = [
        c for c in chunks if c.topic == "Online Bachelor of Science in Computing Applications"
    ]
    assert computing
    assert all(c.group == _BACHELORS for c in computing)

    communication = [
        c for c in chunks if c.topic == "Online Bachelor of Arts in Communication Studies"
    ]
    assert not any("computing applications" in c.text.lower() for c in communication)


def test_no_chunk_spills_into_another_topic(parsed) -> None:
    """The defect that started this: 77 old chunks ran from one program into the next."""
    _, _, chunks = parsed
    headers = {c.text.split("\n\n", 1)[0] for c in chunks}
    for chunk in chunks:
        own = chunk.text.split("\n\n", 1)[0]
        lines = set(chunk.text.splitlines()[1:])
        foreign = (headers - {own}) & lines
        assert not foreign, f"{chunk.chunk_id} contains other topic headers: {foreign}"
        assert "If asked questions about" not in chunk.text


def test_recurring_sections_are_never_nested_under_another_label(parsed) -> None:
    """ "Related Programs" is a section of its own, not a course under "Key Courses"."""
    _, structure, _ = parsed
    for section in structure.sections:
        for label in section.label_path[1:]:
            assert not label.startswith(_RECURRING), f"{section.topic}: {section.label_path}"


def test_deadline_terms_stay_under_application_deadlines(parsed) -> None:
    _, structure, _ = parsed
    terms = [s for s in structure.sections if s.label_path[-1] in ("Late Fall", "Early Spring")]
    assert terms
    assert all(s.label_path == ["Application Deadlines", s.label_path[-1]] for s in terms)


def test_every_body_paragraph_survives(parsed) -> None:
    """Nothing the rules treat as content may be lost between the .docx and the index."""
    paragraphs, _, chunks = parsed
    all_text = _canon("\n".join(c.text for c in chunks))
    for paragraph in paragraphs:
        if paragraph.bold:
            continue
        for sentence in _SENTENCE_RE.split(clean_text(paragraph.text)):
            if sentence.strip():
                assert _canon(sentence.strip()) in all_text, f"lost: {paragraph.text[:80]!r}"


def test_answers_keep_their_sub_labels(parsed) -> None:
    _, _, chunks = parsed
    chunk = next(c for c in chunks if "Graduate Enrollment Status (8-Week Model)" in c.text)
    assert chunk.kind is ChunkKind.QA
    assert "How many courses can students take at once?" in chunk.text


def test_master_program_pages_carry_their_group(parsed) -> None:
    _, _, chunks = parsed
    mab = [c for c in chunks if c.topic == "Online Master of Agribusiness (MAB)"]
    assert mab
    assert all(c.group == "Master's Degrees" for c in mab)
    header = "Master's Degrees > Online Master of Agribusiness (MAB)"
    assert all(c.text.startswith(header) for c in mab)


def test_toc_question_titles_are_not_indexed_without_answers(parsed) -> None:
    """A TOC copy of a question must not become a chunk of its own."""
    _, _, chunks = parsed
    for chunk in chunks:
        body = chunk.text.split("\n\n", 1)[1]
        lines = [line for line in body.splitlines() if line.strip()]
        questions = [line for line in lines if line.rstrip().endswith("?")]
        assert len(questions) < len(lines) or chunk.kind is ChunkKind.QA, chunk.chunk_id


def test_link_noise_is_gone_and_ids_are_unique(parsed) -> None:
    _, _, chunks = parsed
    assert not any("opens in new tab" in c.text.lower() for c in chunks)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))

"""Extraction, structure and chunking tests.

Failures here are silent. A dropped table or a label attached to the wrong
block does not raise; it makes the knowledge base quietly smaller or wrong, and
the damage resurfaces later as NOT_FOUND verdicts that look like genuine gaps.
So these tests assert that content *survives* and lands under the right topic,
not merely that the functions run.

Structure and chunking tests build paragraphs directly, because the rules read
only text and the bold/list flags. Extraction tests build real .docx files,
because that is where those flags come from.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from docx import Document

from ava_eval.kb.chunk import chunk_sections, slugify
from ava_eval.kb.extract import content_hash, extract_docx, resolve_version_date
from ava_eval.kb.structure import clean_text, normalise_name, parse_structure
from ava_eval.models import ChunkKind, KbParagraph

# --- helpers ----------------------------------------------------------------


def B(text: str) -> tuple[str, str]:
    """A bold paragraph."""
    return ("B", text)


def P(text: str) -> tuple[str, str]:
    """A plain paragraph."""
    return ("P", text)


def L(text: str) -> tuple[str, str]:
    """A list item."""
    return ("L", text)


def marker(topic: str) -> tuple[str, str]:
    """The bold line that opens a topic."""
    return B(f"If asked questions about {topic}, answer with")


def paras(*items: tuple[str, str]) -> list[KbParagraph]:
    """Build extracted paragraphs from (kind, text) pairs."""
    return [
        KbParagraph(text=text, bold=kind == "B", is_list=kind == "L", ordinal=index)
        for index, (kind, text) in enumerate(items)
    ]


def labels(structure) -> list[str]:
    """Every section's label path, joined, in order."""
    return [" > ".join(section.label_path) for section in structure.sections]


# --- extraction -------------------------------------------------------------


@pytest.fixture
def sample_docx(tmp_path: Path) -> Path:
    """A document with a bold marker, plain text, a partly bold line, a list item and a table."""
    doc = Document()
    doc.add_paragraph().add_run("If asked questions about the Online BBA, answer with").bold = True
    doc.add_paragraph("Delivered fully asynchronously.")
    mixed = doc.add_paragraph()
    mixed.add_run("Full-time:").bold = True
    mixed.add_run(" 12 credit hours")
    doc.add_paragraph("Bachelor's degree required", style="List Paragraph")

    table = doc.add_table(rows=1, cols=2)
    header = table.rows[0].cells
    header[0].text = "Program"
    header[1].text = "Minimum GPA"
    row = table.add_row().cells
    row[0].text = "Online BBA"
    row[1].text = "2.0"

    path = tmp_path / "kb.docx"
    doc.save(str(path))
    return path


def test_bold_and_plain_paragraphs_are_flagged(sample_docx: Path) -> None:
    extracted = extract_docx(sample_docx)
    assert extracted[0].bold is True
    assert extracted[1].bold is False


def test_partly_bold_paragraph_is_body_text(sample_docx: Path) -> None:
    """A bold lead-in ("Full-time:") must not turn a line of content into a label."""
    mixed = next(p for p in extract_docx(sample_docx) if p.text.startswith("Full-time"))
    assert mixed.bold is False


def test_list_items_are_flagged(sample_docx: Path) -> None:
    item = next(p for p in extract_docx(sample_docx) if "Bachelor's" in p.text)
    assert item.is_list is True


def test_table_rows_are_extracted_labelled_and_in_order(sample_docx: Path) -> None:
    """The failure this guards against: tables silently vanishing."""
    extracted = extract_docx(sample_docx)
    assert extracted[-1].text == "Program: Online BBA | Minimum GPA: 2.0"
    assert extracted[-1].bold is False


def test_content_hash_is_stable_and_sensitive_to_formatting(sample_docx: Path) -> None:
    extracted = extract_docx(sample_docx)
    assert content_hash(extracted) == content_hash(extract_docx(sample_docx))

    reworded = list(extracted)
    reworded[1] = extracted[1].model_copy(update={"text": extracted[1].text + " Updated."})
    assert content_hash(reworded) != content_hash(extracted)

    # Bolding a line moves chunk boundaries without changing a word.
    bolded = list(extracted)
    bolded[1] = extracted[1].model_copy(update={"bold": True})
    assert content_hash(bolded) != content_hash(extracted)


def test_missing_document_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        extract_docx(tmp_path / "nope.docx")


def test_non_docx_raises_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "fake.docx"
    path.write_text("this is not a docx", encoding="utf-8")
    with pytest.raises(ValueError, match="docx"):
        extract_docx(path)


def test_version_date_prefers_the_sidecar(sample_docx: Path) -> None:
    (sample_docx.parent / "kb_meta.json").write_text(
        '{"kb.docx": {"doc_version_date": "2026-01-15"}}', encoding="utf-8"
    )
    assert resolve_version_date(sample_docx) == "2026-01-15"


def test_version_date_falls_back_to_mtime(sample_docx: Path) -> None:
    assert resolve_version_date(sample_docx).count("-") == 2


def test_malformed_sidecar_does_not_stop_indexing(sample_docx: Path) -> None:
    (sample_docx.parent / "kb_meta.json").write_text("{broken", encoding="utf-8")
    assert resolve_version_date(sample_docx).count("-") == 2


# --- structure --------------------------------------------------------------


def test_topic_markers_open_topics_including_the_nested_wording() -> None:
    structure = parse_structure(
        paras(
            marker("the Online BBA"),
            P("Delivered asynchronously."),
            B("If asked about Online Student Support, answer with"),
            P("Advocates help you enroll."),
        )
    )
    assert structure.topics == ["Online BBA", "Online Student Support"]
    assert [s.topic_no for s in structure.sections] == [1, 2]


def test_text_before_the_first_marker_is_ignored() -> None:
    structure = parse_structure(paras(P("stray preamble"), marker("A"), P("content")))
    assert all("stray preamble" not in line for s in structure.sections for line in s.lines)


def test_a_document_without_topic_markers_is_refused() -> None:
    """Indexing it anyway would recreate the one-untitled-blob failure."""
    with pytest.raises(ValueError, match="topic markers"):
        parse_structure(paras(B("Admissions"), P("The minimum GPA is 2.0.")))


def test_text_before_any_label_is_the_overview() -> None:
    structure = parse_structure(
        paras(marker("A"), P("Intro text."), B("Prerequisites"), P("None."))
    )
    assert labels(structure) == ["Overview", "Prerequisites"]


def test_scaffold_labels_are_dropped() -> None:
    structure = parse_structure(
        paras(
            marker("A"),
            B("Question"),
            B("Below are some of the Frequently Asked Questions (FAQ's) that we receive about A"),
            B("Answer"),
            B("Introduction"),
            P("Welcome."),
            B("Explanation"),
            B("Instructions"),
        )
    )
    everything = " ".join(
        labels(structure) + [line for s in structure.sections for line in s.lines]
    )
    for scaffold in (
        "Question",
        "Answer",
        "Introduction",
        "Explanation",
        "Instructions",
        "Below are",
    ):
        assert scaffold not in everything
    assert "Welcome." in everything


def test_toc_lines_repeating_a_question_are_dropped_but_other_toc_lines_kept() -> None:
    """A question title with no answer under it is the false-kb_gap magnet."""
    structure = parse_structure(
        paras(
            marker("Master's Degrees"),
            P("We offer these degrees:"),
            B("Table of Contents"),
            P("How do I apply?"),
            P("Online Master of Agribusiness"),
            B("How do I apply?"),
            P("Apply online."),
        )
    )
    overview = structure.sections[0]
    assert overview.lines == ["We offer these degrees:", "- Online Master of Agribusiness"]
    assert sum("How do I apply?" in label for label in labels(structure)) == 1


def test_a_question_keeps_the_sub_labels_inside_its_answer() -> None:
    structure = parse_structure(
        paras(
            marker("Program Structure"),
            B("How many courses can I take?"),
            P("Enrollment is based on credit hours."),
            B("Graduate Enrollment Status (8-Week Model)"),
            L("Full-time: 6 credit hours"),
            B("Are terms offered year-round?"),
            P("Yes."),
        )
    )
    assert [s.kind for s in structure.sections] == [ChunkKind.QA, ChunkKind.QA]
    assert structure.sections[0].lines == [
        "Enrollment is based on credit hours.",
        "Graduate Enrollment Status (8-Week Model)",
        "- Full-time: 6 credit hours",
    ]


def test_a_label_listed_in_the_toc_ends_the_previous_answer() -> None:
    """The TOC is where the author declared a new section starts."""
    structure = parse_structure(
        paras(
            marker("Microcredentials"),
            B("Table of Contents"),
            P("What is a microcredential?"),
            P("Benefits & Features"),
            B("What is a microcredential?"),
            P("A short, targeted program."),
            B("Benefits & Features"),
            P("Fast and flexible."),
        )
    )
    assert labels(structure) == ["What is a microcredential?", "Benefits & Features"]
    assert [s.kind for s in structure.sections] == [ChunkKind.QA, ChunkKind.REFERENCE]


def test_an_empty_label_heads_the_labels_that_follow() -> None:
    structure = parse_structure(
        paras(
            marker("Online MAB"),
            B("Application Deadlines"),
            B("Late Fall"),
            P("Application Deadline - October 10, 2026"),
            B("Early Spring"),
            P("Application Deadline - January 1, 2027"),
        )
    )
    assert labels(structure) == [
        "Application Deadlines > Late Fall",
        "Application Deadlines > Early Spring",
    ]


def test_a_question_resets_the_parent_label() -> None:
    structure = parse_structure(
        paras(
            marker("A"),
            B("Application Deadlines"),
            B("Late Fall"),
            P("October 10."),
            B("Can I defer my start?"),
            P("Yes."),
        )
    )
    assert labels(structure)[-1] == "Can I defer my start?"


def test_catalog_toc_assigns_groups_with_normalised_names() -> None:
    structure = parse_structure(
        paras(
            marker("Master's Degrees"),
            P("Take the next step in your career."),
            B("Table of Contents"),
            P("Online Master of Agribusiness (MAB)"),
            P("Master of Arts in Legal Studies – Energy"),
            P("Online Master of Underwater Basket Weaving"),
            marker("the Online Master of Agribusiness (MAB)"),
            P("A 36-credit-hour program."),
            marker("Online Master of Arts in Legal Studies - Energy"),
            P("Energy law."),
        )
    )
    assert structure.groups == {2: "Master's Degrees", 3: "Master's Degrees"}
    assert structure.unmatched_catalog_entries == [
        ("Master's Degrees", "Online Master of Underwater Basket Weaving")
    ]
    assert {s.group for s in structure.sections if s.topic_no in (2, 3)} == {"Master's Degrees"}


def test_a_catalog_listed_title_without_a_marker_opens_its_own_topic() -> None:
    """A program page pasted under a neighbour must not be cited as the neighbour."""
    structure = parse_structure(
        paras(
            marker("Bachelor's Degrees"),
            B("Table of Contents"),
            P("Online BA in Sociology"),
            P("Online BA in Criminology"),
            marker("Online BA in Sociology"),
            P("The study of society."),
            B("Online BA in Criminology"),
            P("The study of crime."),
        )
    )
    assert structure.topics == [
        "Bachelor's Degrees",
        "Online BA in Sociology",
        "Online BA in Criminology",
    ]
    assert structure.unmarked_topics == ["Online BA in Criminology"]
    assert structure.unmatched_catalog_entries == []
    crime = next(s for s in structure.sections if "The study of crime." in s.lines)
    assert crime.topic == "Online BA in Criminology"
    assert crime.group == "Bachelor's Degrees"


def test_a_recurring_label_ends_a_parent_of_one_off_labels() -> None:
    """Course titles nest under "Key Courses"; "Related Programs" does not."""
    items = []
    for n in range(3):
        items += [
            marker(f"Program {n}"),
            B("Key Courses"),
            B(f"COURSE {n}01 - Foundations"),
            P("Foundations of the field."),
            B(f"COURSE {n}02 - Capstone"),
            P("A final project."),
            B("Related Programs"),
            P("Another program."),
            B("Application Deadlines"),
            B("Late Fall"),
            P("October 10."),
            B("Early Spring"),
            P("January 1."),
        ]
    structure = parse_structure(paras(*items))
    first_topic = [" > ".join(s.label_path) for s in structure.sections if s.topic_no == 1]
    assert first_topic == [
        "Key Courses > COURSE 001 - Foundations",
        "Key Courses > COURSE 002 - Capstone",
        "Related Programs",
        "Application Deadlines > Late Fall",
        "Application Deadlines > Early Spring",
    ]


def test_link_noise_is_stripped() -> None:
    structure = parse_structure(paras(marker("A"), L("Request Information (opens in new tab)")))
    assert structure.sections[0].lines == ["- Request Information"]
    assert clean_text("Apply (Opens in new tab)") == "Apply"


def test_normalise_name_ignores_dashes_case_and_leading_words() -> None:
    assert normalise_name("the Anthropology, B.A. – Online") == normalise_name(
        "Anthropology, B.A. - Online"
    )
    assert normalise_name("Online Master’s Degree") == normalise_name("master's degree")


# --- chunking ---------------------------------------------------------------


def _chunks(*items: tuple[str, str], target: int = 1200, overlap: float = 0.15):
    return chunk_sections(
        parse_structure(paras(*items)).sections,
        doc_id="kb",
        target_chars=target,
        overlap_ratio=overlap,
    )


def test_each_question_is_its_own_chunk() -> None:
    chunks = _chunks(
        marker("FAQ"),
        B("Can I walk the stage?"),
        P("Yes."),
        B("Is it available internationally?"),
        P("Not at this time."),
    )
    assert [c.chunk_id for c in chunks] == ["t01.q01", "t01.q02"]
    assert all(c.kind is ChunkKind.QA for c in chunks)
    assert "Can I walk the stage?\nYes." in chunks[0].text
    assert "internationally" not in chunks[0].text


def test_small_reference_sections_pack_within_a_topic_but_never_across() -> None:
    chunks = _chunks(
        marker("Online MAB"),
        P("Overview text."),
        B("Prerequisites"),
        P("Bachelor's degree."),
        B("Application Deadlines"),
        B("Late Fall"),
        P("October 10."),
        marker("Online BBA"),
        P("Asynchronous."),
    )
    assert [c.chunk_id for c in chunks] == ["t01.r01", "t02.r01"]
    assert "Prerequisites" in chunks[0].text and "Late Fall" in chunks[0].text
    assert "Asynchronous." not in chunks[0].text
    assert chunks[0].section == (
        "Online MAB > Overview; Prerequisites; Application Deadlines > Late Fall"
    )


def test_packing_closes_before_the_target() -> None:
    items = [marker("A")]
    for index in range(10):
        items += [B(f"Label {index}"), P("x" * 290)]
    chunks = _chunks(*items, target=1000)
    assert len(chunks) > 1
    for chunk in chunks:
        body = chunk.text.split("\n\n", 1)[1]
        assert len(body) <= 1000 + len("\n\n") * 3


def test_a_question_between_reference_sections_keeps_document_order() -> None:
    chunks = _chunks(
        marker("A"),
        P("Overview."),
        B("Why choose us?"),
        P("Because."),
        B("Earn Badges"),
        P("Badges."),
    )
    # "Earn Badges" is not a question and not in a TOC, so it stays in the answer.
    assert [c.chunk_id for c in chunks] == ["t01.r01", "t01.q01"]
    assert "Badges." in chunks[1].text


def test_an_oversized_section_is_split_with_overlap_only_inside_it() -> None:
    long_lines = [P(f"Overview sentence number {i}. " + "y" * 180) for i in range(8)]
    chunks = _chunks(
        marker("A"),
        *long_lines,
        B("Is it online?"),
        P("Yes."),
        target=600,
        overlap=0.4,
    )
    parts = [c for c in chunks if c.kind is ChunkKind.REFERENCE]
    assert len(parts) > 1
    assert "(part 1/" in parts[0].text
    last_line_of_first = parts[0].text.splitlines()[-1]
    assert last_line_of_first in parts[1].text, "no overlap between parts of one section"

    question = next(c for c in chunks if c.kind is ChunkKind.QA)
    assert "Overview sentence" not in question.text, "overlap leaked into another section"


def test_an_oversized_single_paragraph_is_split_on_sentences() -> None:
    giant = " ".join(f"Sentence number {i} goes here." for i in range(200))
    chunks = _chunks(marker("A"), P(giant), target=500, overlap=0.0)
    assert len(chunks) > 1
    assert all(c.text.strip().endswith(".") for c in chunks)


def test_chunk_ids_are_short_positional_and_unique() -> None:
    chunks = _chunks(
        marker("A"),
        P("Overview."),
        B("Q one?"),
        P("One."),
        B("Q two?"),
        P("Two."),
        marker("B"),
        P("Other topic."),
    )
    ids = [c.chunk_id for c in chunks]
    assert ids == ["t01.r01", "t01.q01", "t01.q02", "t02.r01"]
    assert all(re.fullmatch(r"t\d{2}\.[qr]\d{2}", i) for i in ids)
    assert len(ids) == len(set(ids))


def test_every_chunk_starts_with_its_group_and_topic() -> None:
    chunks = _chunks(
        marker("Master's Degrees"),
        B("Table of Contents"),
        P("Online MAB"),
        marker("Online MAB"),
        P("Overview."),
        B("Is it online?"),
        P("Yes."),
    )
    mab = [c for c in chunks if c.topic == "Online MAB"]
    assert mab
    assert all(c.text.startswith("Master's Degrees > Online MAB\n\n") for c in mab)
    assert all(c.section.startswith("Master's Degrees > Online MAB > ") for c in mab)


def test_table_rows_survive_into_the_answer_chunk() -> None:
    chunks = _chunks(
        marker("Admissions"),
        B("Do programs set their own GPA?"),
        P("Yes."),
        P("Program: Online BBA | Minimum GPA: 2.0"),
    )
    assert "Program: Online BBA | Minimum GPA: 2.0" in chunks[0].text


def test_content_survives_structure_and_chunking() -> None:
    """Every paragraph that is not a marker, scaffold or TOC label ends up in a chunk."""
    items = (
        marker("FAQ"),
        B("Introduction"),
        P("Welcome to the FAQ."),
        B("Table of Contents"),
        P("How do I apply?"),
        P("Something only listed"),
        B("How do I apply?"),
        P("Apply online (opens in new tab)."),
        B("Enrollment Status"),
        L("Full-time: 6 credit hours"),
        marker("Program"),
        P("Overview of the program."),
        B("Application Deadlines"),
        B("Late Fall"),
        P("October 10."),
    )
    chunks = _chunks(*items)
    all_text = "\n".join(c.text for c in chunks)
    dropped = {"Introduction", "Table of Contents"}
    for kind, text in items:
        if text.startswith("If asked questions about") or text in dropped:
            continue
        assert clean_text(text) in all_text, f"lost: {text!r}"


def test_no_sections_means_no_chunks() -> None:
    assert chunk_sections([], doc_id="kb") == []


def test_slugify_handles_punctuation_and_length() -> None:
    assert slugify("3.2 GPA & Transcript Requirements!") == "3_2_gpa_transcript_requirements"
    assert len(slugify("x" * 200)) <= 40
    assert slugify("!!!") == "untitled"

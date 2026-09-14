"""Recover the knowledge base's structure from its formatting.

TTU's ``online.docx`` has no Word heading styles. Its structure is carried by
bold paragraphs instead, following a consistent convention:

* ``If asked questions about X, answer with`` (bold) opens a **topic**: a program
  page or a themed FAQ. One topic nests a second marker worded
  ``If asked about X, answer with``; it is treated the same way.
* Inside a topic, a bold line is a **label**: either a question ("How do I drop
  a course?") or a heading ("Prerequisites", "Application Deadlines").
* ``Introduction`` / ``Answer`` / ``Explanation`` / ``Instructions`` /
  ``Question`` are authoring scaffolding for Ava's prompt, not content.
* ``Table of Contents`` is followed by plain lines listing the topic's sections.
  Most repeat a question answered further down; some are the content itself,
  like the list of programs a catalog topic ("Master's Degrees") offers.

Two irregularities in the real document shape the rules below:

* A few program pages were pasted under *another* program's topic with only a
  bold title and no marker. A catalog lists them, which is how they are found:
  a bold title matching a catalog entry that has no topic opens a topic of its
  own. Without this, a criminology page would be cited as sociology content.
* A label with nothing under it heads the labels that follow it ("Application
  Deadlines" > "Late Fall"), but the document never says where that grouping
  ends. The signal used is vocabulary: labels that recur across many topics
  ("Related Programs", "Late Fall") are section names, labels that appear once
  (a course title) are items. A parent keeps labels of the same kind as its
  first child and lets go at the first label of the other kind.

Everything here is deterministic string and formatting rules, with no model call.
That keeps indexing free and repeatable, but it depends on TTU keeping this
formatting, which is why ``parse_structure`` refuses a document with no topic
markers rather than quietly indexing it as one untitled blob.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from ..models import ChunkKind, KbParagraph, KbSection

_MARKER_RE = re.compile(
    r"^if asked (?:questions )?about (?:the )?(?P<topic>.+?),?\s+answer with:?$",
    re.IGNORECASE,
)
_SCAFFOLD_LABELS = frozenset({"answer", "question", "introduction", "explanation", "instructions"})
_FAQ_INTRO_RE = re.compile(r"^below are some of the frequently asked questions", re.IGNORECASE)
_TOC_LABEL = "table of contents"
_LINK_NOISE_RE = re.compile(r"\s*\(opens in new tab\)", re.IGNORECASE)
_DASH_RE = re.compile(r"[‒–—―]")
_MAX_LABEL_CHARS = 250
# A label found in at least this many topics is a recurring section name. In the
# real document the section names appear in 10-41 topics and nothing else in
# more than 2 (a few course titles shared by two legal studies degrees).
_RECURRING_LABEL_MIN_TOPICS = 3

OVERVIEW_LABEL = "Overview"


def clean_text(text: str) -> str:
    """Strip link boilerplate that carries no meaning but pollutes embeddings."""
    return _LINK_NOISE_RE.sub("", text).strip()


def normalise_name(text: str) -> str:
    """Canonical form used to match names written in different places.

    Catalog lists and topic markers were typed separately, so the same program
    differs in dashes ("B.A. - Online" vs "B.A. – Online"), apostrophes, a
    leading "the" or "Online", and case.
    """
    text = unicodedata.normalize("NFKC", clean_text(text))
    text = _DASH_RE.sub("-", text).replace("’", "'").replace("‘", "'")
    text = re.sub(r"\s+", " ", text).strip().strip(".").lower()
    text = re.sub(r"^the\s+", "", text)
    return re.sub(r"^online\s+", "", text)


@dataclass
class KbStructure:
    """What the parser recovered from one document.

    Attributes:
        sections: Labelled blocks in document order, ready for chunking.
        topics: Topic names, index 0 being topic number 1.
        groups: Topic number -> the catalog topic that lists it.
        unmarked_topics: Topics opened by a catalog-listed bold title rather than
            a marker -- worth telling TTU, since Ava's prompt lacks the marker too.
        unmatched_catalog_entries: (catalog, entry) pairs a catalog lists but the
            knowledge base has no content for at all.
    """

    sections: list[KbSection]
    topics: list[str]
    groups: dict[int, str] = field(default_factory=dict)
    unmarked_topics: list[str] = field(default_factory=list)
    unmatched_catalog_entries: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _Topic:
    """A topic's name and the paragraphs between its opening line and the next topic."""

    number: int
    name: str
    marked: bool = True
    paragraphs: list[KbParagraph] = field(default_factory=list)


@dataclass
class _OpenSection:
    """The block currently being filled while walking a topic."""

    label_path: list[str]
    kind: ChunkKind
    lines: list[str] = field(default_factory=list)


def _is_toc_label(paragraph: KbParagraph) -> bool:
    return paragraph.bold and paragraph.text.strip().lower() == _TOC_LABEL


def _is_scaffold(paragraph: KbParagraph) -> bool:
    """Bold authoring labels that organise the prompt but say nothing about TTU."""
    if not paragraph.bold:
        return False
    text = paragraph.text.strip()
    return text.rstrip(":").lower() in _SCAFFOLD_LABELS or bool(_FAQ_INTRO_RE.match(text))


def _is_label(paragraph: KbParagraph) -> bool:
    """A bold, short line that heads the content below it."""
    return (
        paragraph.bold
        and len(paragraph.text) <= _MAX_LABEL_CHARS
        and not _is_scaffold(paragraph)
        and not _is_toc_label(paragraph)
    )


def _split_topics(
    paragraphs: list[KbParagraph], unmarked_names: frozenset[str] = frozenset()
) -> list[_Topic]:
    """Cut the document into topics. Text before the first one is ignored.

    Args:
        paragraphs: The whole document.
        unmarked_names: Normalised names whose bold title opens a topic even
            without a marker (catalog entries with no marked topic).
    """
    topics: list[_Topic] = []
    for paragraph in paragraphs:
        match = _MARKER_RE.match(paragraph.text.strip()) if paragraph.bold else None
        if match:
            topics.append(_Topic(number=len(topics) + 1, name=clean_text(match.group("topic"))))
        elif _is_label(paragraph) and normalise_name(paragraph.text) in unmarked_names:
            topics.append(
                _Topic(number=len(topics) + 1, name=clean_text(paragraph.text), marked=False)
            )
        elif topics:
            topics[-1].paragraphs.append(paragraph)
    return topics


def _toc_entries(topic: _Topic) -> list[str]:
    """Plain lines listed under the topic's "Table of Contents" label, as written."""
    entries: list[str] = []
    in_toc = False
    for paragraph in topic.paragraphs:
        if paragraph.bold:
            in_toc = _is_toc_label(paragraph)
            continue
        if in_toc:
            entries.append(paragraph.text)
    return entries


def _recurring_labels(topics: list[_Topic]) -> frozenset[str]:
    """Non-question labels used as section names across many topics."""
    counts: Counter[str] = Counter()
    for topic in topics:
        counts.update(
            {
                normalise_name(p.text)
                for p in topic.paragraphs
                if _is_label(p) and not clean_text(p.text).endswith("?")
            }
        )
    return frozenset(label for label, n in counts.items() if n >= _RECURRING_LABEL_MIN_TOPICS)


def _sections_for_topic(topic: _Topic, group: str, recurring: frozenset[str]) -> list[KbSection]:
    """Walk one topic's paragraphs and cut them into labelled blocks."""
    toc = {normalise_name(entry) for entry in _toc_entries(topic)}
    labels = {normalise_name(p.text) for p in topic.paragraphs if _is_label(p)}

    sections: list[KbSection] = []
    current: _OpenSection | None = None
    parent: list[str] = []
    # Whether the parent's first child was a recurring label; None when no parent.
    parent_children_recur: bool | None = None
    in_toc = False

    def emit() -> None:
        """Keep the open block if it has content. A label with nothing under it is not a section."""
        if current is not None and current.lines:
            sections.append(
                KbSection(
                    topic_no=topic.number,
                    group=group,
                    topic=topic.name,
                    label_path=current.label_path,
                    kind=current.kind,
                    lines=current.lines,
                )
            )

    for paragraph in topic.paragraphs:
        text = clean_text(paragraph.text)

        if _is_toc_label(paragraph):
            in_toc = True
            continue

        if _is_scaffold(paragraph):
            in_toc = False
            continue

        if _is_label(paragraph):
            in_toc = False
            is_question = text.endswith("?")
            listed_in_toc = normalise_name(text) in toc
            recurs = normalise_name(text) in recurring

            if current is not None and current.kind is ChunkKind.QA:
                if not is_question and not listed_in_toc:
                    # A sub-heading inside an answer ("Graduate Enrollment Status")
                    # belongs to that answer. Splitting it off would leave the
                    # enrollment figures with no question attached.
                    current.lines.append(text)
                    continue

            if current is not None and not current.lines:
                # The previous label had nothing under it, so it heads the
                # labels that follow: "Application Deadlines" > "Late Fall".
                prefix = current.label_path
                if is_question or listed_in_toc:
                    parent, parent_children_recur = [], None
                else:
                    parent, parent_children_recur = current.label_path, recurs
            else:
                emit()
                if parent and (is_question or listed_in_toc or recurs != parent_children_recur):
                    # "Related Programs" after a run of course titles is a new
                    # section, not another course under "Key Courses".
                    parent, parent_children_recur = [], None
                prefix = parent

            kind = ChunkKind.QA if is_question else ChunkKind.REFERENCE
            current = _OpenSection(label_path=[*prefix, text], kind=kind)
            continue

        if paragraph.bold:
            # A long bold sentence is emphasised body text, and it ends any TOC list.
            in_toc = False

        if not text:
            continue

        if in_toc and normalise_name(text) in labels:
            # The question itself is answered further down this topic. Keeping the
            # TOC copy would index a question title with no answer under it.
            continue

        if current is None:
            current = _OpenSection(label_path=[OVERVIEW_LABEL], kind=ChunkKind.REFERENCE)
        current.lines.append(f"- {text}" if paragraph.is_list or in_toc else text)

    emit()
    return sections


def _assign_groups(topics: list[_Topic]) -> tuple[dict[int, str], list[tuple[str, str]]]:
    """Place topics under the catalog topic whose table of contents lists them.

    A catalog is any topic whose TOC names at least one other topic ("Master's
    Degrees" lists the ten master's program pages). The group is what tells the
    judge that an "Overview" belongs to a master's program rather than a
    bachelor's one when the topic name alone does not say so.
    """
    by_name = {normalise_name(topic.name): topic for topic in topics}
    groups: dict[int, str] = {}
    unmatched: list[tuple[str, str]] = []

    for catalog in topics:
        entries = _toc_entries(catalog)
        members = [
            by_name[normalise_name(entry)]
            for entry in entries
            if normalise_name(entry) in by_name and by_name[normalise_name(entry)] is not catalog
        ]
        if not members:
            continue

        for member in members:
            # Document order wins if two catalogs ever list the same topic.
            groups.setdefault(member.number, catalog.name)

        own_labels = {normalise_name(p.text) for p in catalog.paragraphs if _is_label(p)}
        for entry in entries:
            key = normalise_name(entry)
            if key not in by_name and key not in own_labels:
                unmatched.append((catalog.name, clean_text(entry)))

    return groups, unmatched


def parse_structure(paragraphs: list[KbParagraph]) -> KbStructure:
    """Turn extracted paragraphs into topics and labelled sections.

    Args:
        paragraphs: Output of ``extract_docx``, in document order.

    Returns:
        The recovered structure.

    Raises:
        ValueError: if the document contains no topic markers. Indexing it anyway
            would produce one untitled blob, which is exactly the failure this
            module exists to prevent, so it has to be loud.
    """
    topics = _split_topics(paragraphs)
    if not topics:
        raise ValueError(
            "found no 'If asked questions about ..., answer with' topic markers -- the "
            "knowledge base formatting has changed, so it cannot be chunked reliably"
        )

    # Catalog entries with no marked topic may still have a page, pasted under
    # another topic behind a bold title. A second split promotes those titles.
    _, unmatched = _assign_groups(topics)
    if unmatched:
        topics = _split_topics(
            paragraphs, frozenset(normalise_name(entry) for _, entry in unmatched)
        )

    groups, unmatched = _assign_groups(topics)
    recurring = _recurring_labels(topics)
    sections = [
        section
        for topic in topics
        for section in _sections_for_topic(topic, groups.get(topic.number, ""), recurring)
    ]
    return KbStructure(
        sections=sections,
        topics=[topic.name for topic in topics],
        groups=groups,
        unmarked_topics=[topic.name for topic in topics if not topic.marked],
        unmatched_catalog_entries=unmatched,
    )

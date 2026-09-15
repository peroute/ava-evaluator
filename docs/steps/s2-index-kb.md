# Step 2 — Index the knowledge base

[← Step 1](s1-ingest.md) · [Overview](../overview.md) · [Step 3 →](s3-classify.md)

**Command:** `python -m ava_eval index-kb [--kb-file FILE] [--force]` · **Code:** [`ava_eval/steps/s2_index_kb.py`](../../ava_eval/steps/s2_index_kb.py), [`ava_eval/kb/extract.py`](../../ava_eval/kb/extract.py), [`ava_eval/kb/structure.py`](../../ava_eval/kb/structure.py), [`ava_eval/kb/chunk.py`](../../ava_eval/kb/chunk.py)

## What it does

Turns TTU's knowledge base document into something you can search by meaning. It pulls the text out (tables
included), works out the document's structure from its formatting, cuts it into pieces that can each be cited on their
own (chunks), converts each chunk into a list of numbers that captures its meaning (an embedding), and stores those in
PostgreSQL.

The knowledge base is **one document**: `realdata/online.docx` for real runs (set `AVA_KB_FILE`), or the generated
sample KB by default. It runs on its own schedule, whenever TTU edits the document, not every time you evaluate. An
unchanged document is skipped.

## Inputs → outputs

| | |
|---|---|
| **Reads** | the `.docx` at `--kb-file` (default `AVA_KB_FILE`), plus an optional `kb_meta.json` next to it |
| **Produces** | [`KbParagraph`](../../ava_eval/models.py) (text + bold/list flags) → [`KbSection`](../../ava_eval/models.py) (one labelled block) → [`KbChunk`](../../ava_eval/models.py) |
| **Writes** | `kb_documents` (one row, with a content hash), `kb_chunks` (text, `kind`, `vector` embedding). Replaces whatever was indexed before |

## How the document is laid out

`online.docx` has **no Word heading styles**. Every paragraph is "Normal" or "List Paragraph". Its structure is carried
by bold text, following a convention written for Ava's prompt:

```
**If asked questions about the Online Master of Agribusiness (MAB), answer with**   <- opens a topic
Advance your career in today's evolving agricultural economy ...                     <- text before any label
**Prerequisites**                                                                   <- a label
Bachelor's degree from any accredited institution. No GMAT/GRE is required.
**Application Deadlines**                                                           <- a label with nothing under it
**Late Fall**
Application Deadline - October 10, 2026
```

FAQ-style topics add scaffolding (**Introduction**, **Question**, **Answer**, **Explanation**) and a **Table of
Contents** whose plain lines repeat the bold questions answered further down. Catalog topics ("Master's Degrees") use
their table of contents to list the program topics they cover.

In the real document that is 69 marked topics, plus 4 program pages that were pasted under a neighbouring program with
only a bold title (see [Gotchas](#gotchas)): 73 topics, 322 chunks (144 question, 178 reference).

## How the code works

[`index_knowledge_base`](../../ava_eval/steps/s2_index_kb.py) runs these steps on the one file:

1. **Extract** with [`extract_docx`](../../ava_eval/kb/extract.py).
   - `_iter_block_items` walks the document's raw XML so paragraphs and tables come out **in the order they appear**.
     python-docx's `doc.paragraphs` and `doc.tables` are two separate lists, which would detach a table from the
     question it answers.
   - `_is_bold` is true only when **every** visible run is bold, resolving inherited bold through the run and
     paragraph styles. A line with a bold lead-in ("**Full-time:** 6 credit hours") is body text, not a label.
   - `_flatten_table` turns each table row into one line. If the first row looks like a header row, each cell is
     labelled with its column name: `Program: Online BBA in General Business | Minimum GPA: 2.0 | ...`.
2. **Version date** from [`resolve_version_date`](../../ava_eval/kb/extract.py): `kb_meta.json` if it has an entry,
   otherwise the file's modified time. It must parse as `YYYY-MM-DD`, or indexing stops with a clear message.
3. **Structure** with [`parse_structure`](../../ava_eval/kb/structure.py). Fixed rules, no model call:

   | Rule | Example |
   |---|---|
   | A bold `If asked (questions) about X, answer with` opens topic X | both wordings appear in the real document |
   | Bold `Introduction`, `Question`, `Answer`, `Explanation`, `Instructions`, and "Below are some of the Frequently Asked Questions…" are dropped | authoring scaffolding, not content |
   | A table-of-contents line that repeats a later label in the same topic is dropped; other TOC lines are kept | the program list of a catalog topic survives |
   | A bold line of at most 250 characters is a **label**; ending in `?` makes it a **question** | `How do I drop a course?` |
   | A question keeps the non-question labels under it as part of its answer, until the next question or a label listed in the topic's TOC | `Graduate Enrollment Status (8-Week Model)` stays with *How many courses can students take at once?* |
   | A label with nothing under it becomes the parent of the labels that follow | `Application Deadlines > Late Fall` |
   | A parent lets go at the first label of a different kind than its first child: **recurring** (used in 3+ topics) vs **one-off** | course titles nest under `Key Courses`; `Related Programs` does not |
   | A catalog topic's TOC entries that name other topics put those topics in its **group** (names compared ignoring dashes, apostrophes, case, a leading "the"/"Online") | `Master's Degrees > Online Master of Agribusiness (MAB)` |
   | A bold title matching a catalog entry that has no marked topic opens a topic of its own | the 4 pasted program pages |
   | `(opens in new tab)` is stripped; list items become `- item` | |

   A document with **no topic markers is refused** with a `ValueError`, rather than indexed as one untitled blob.
4. **Chunk** with [`chunk_sections`](../../ava_eval/kb/chunk.py).
   - A **question** section becomes exactly **one chunk** (`kind = qa`).
   - Neighbouring **reference** sections in the same topic are **packed** into one chunk (`kind = reference`) until the
     next one would push the body past `AVA_CHUNK_TARGET_CHARS` (1200). Packing never crosses a topic, and a question in
     between closes the pack, so chunk order follows the document.
   - A section larger than the target is split into `(part 1/3)`-style pieces at line boundaries, falling back to
     sentence boundaries for a single oversized line. Only these pieces overlap (`AVA_CHUNK_OVERLAP_RATIO`, 15% of the
     target, as whole lines). There is **no overlap between sections**.
   - Every chunk's text starts with its `Group > Topic` header. `section` is `Group > Topic > label(s)`.
   - Chunk ids are short and positional: `t{topic:02d}.q{n:02d}` for questions, `t{topic:02d}.r{n:02d}` for reference
     chunks, numbered from 1 within each topic.
5. **Skip if unchanged.** [`content_hash`](../../ava_eval/kb/extract.py) is a SHA-256 of the extracted text **and the
   bold/list flags**. If it matches the indexed KB ([`store.current_kb_hash`](../../ava_eval/db/store.py)), the run
   stops (unless `--force`).
6. **Embed** with [`embed_texts(..., is_query=False, titles=sections)`](../../ava_eval/llm/client.py). Each chunk is
   prefixed `title: <section> | text: <chunk>`. This happens **before** any database write.
7. **Store** in one transaction: [`clear_kb`](../../ava_eval/db/store.py) (removes the old document and, by cascade, its
   chunks), [`upsert_document`](../../ava_eval/db/store.py), [`insert_chunks`](../../ava_eval/db/store.py). The insert
   scales every vector to length 1 ([`normalise`](../../ava_eval/db/store.py)). Step 5 relies on that.

Two chunks from the sample KB (`python scripts/make_sample_kb.py`):

```
t01.q02  [qa]  Undergraduate Admissions > Do programs set their own requirements?

Undergraduate Admissions

Do programs set their own requirements?
Yes. Where a program states a requirement different from the university minimum, the program requirement applies.
Program Requirements
Program: Online BBA in General Business | Minimum GPA: 2.0 | Transferable Credit Hours Required: 30
...
```

```
t03.r01  [reference]  Bachelor's Degrees > Online Bachelor of Business Administration in General Business
                      > Overview; Prerequisites; Application Deadlines > Late Fall; Application Deadlines > Early Spring

Bachelor's Degrees > Online Bachelor of Business Administration in General Business

Overview
The Online Bachelor of Business Administration in General Business is delivered fully asynchronously, ...

Prerequisites
- High school diploma or equivalent
- Degree and Course Details

Application Deadlines > Late Fall
Application Deadline - October 10, 2026
Classes Begin - October 19, 2026
...
```

The command's output also lists **program pages found without a topic marker**, and **catalog entries with no content
at all**. Neither stops indexing; both are worth telling TTU about.

## Stub vs live

| `AVA_STUB_EMBED=1` (default) | `AVA_STUB_EMBED=0` |
|---|---|
| [`_fake_embed`](../../ava_eval/llm/client.py): a hashed bag of words plus word pairs. Deterministic, and texts with shared words land near each other, so offline search still returns plausible chunks. | `gemini-embedding-2`, **one API call per chunk** (about 320 for the real document), with retries and backoff. |

Extraction, structure and chunking are always real.

## Why it's built this way

- **Structure from formatting, not a fixed window.** A fixed 500-token window cuts between "minimum GPA of 2.0" and
  "for the Online BBA", or glues the end of one program's page to the start of the next. The previous version, which
  looked for heading styles this document doesn't use, did exactly that: every chunk was `(no section)` and 77 of 228
  ran from one topic into the next.
- **Questions stand alone; descriptive sections are packed.** A question and its answer is a natural citable unit, and
  keeping it alone means no unrelated answer rides along into the judge prompt. Program-page labels are layout, not
  meaning: a 71-character "Late Fall" chunk would waste one of the judge's five retrieval slots.
- **The header is on every chunk.** Dozens of program pages carry word-for-word identical deadline text. Without
  `Group > Topic`, neither the search nor the judge could tell which program a deadline belongs to.
- **Drop table-of-contents copies of questions.** A question title with no answer matches a claim's wording very well,
  so search returns it, the judge finds nothing in it, and a false `kb_gap` gets blamed on TTU's documentation.
- **Deterministic rules, not a model, for structure.** Free, repeatable, and testable against the real document. The
  cost is that it depends on TTU's formatting, which is why a document without markers is refused and
  [`test_kb_realdata.py`](../../tests/test_kb_realdata.py) pins the real document's properties.
- **Short positional ids.** The judge has to copy the evidence id back exactly. `t11.r02` is hard to mistype, and
  reports show the readable `section` next to it.
- **Replace the whole index.** With one document there is nothing to patch, and a renamed file can never leave its old
  chunks searchable.
- **Our own chunking, not Ava's.** Phase 1 asks "is this claim true according to the KB's *content*?", not "did Ava's
  retrieval do a good job?". Copying Ava's chunk boundaries would inherit Ava's blind spots and tell us nothing.
- **Hash the extracted content, not the file bytes.** Opening and re-saving a `.docx` changes its bytes without
  changing a word. Re-embedding because someone opened the file wastes API quota. The bold flags are included because
  bolding a line moves chunk boundaries.

## Gotchas

- **The parser depends on TTU's formatting.** If a topic loses its bold marker, or labels stop being bold, content lands
  under the wrong topic without any error. After TTU edits the document, check the `index-kb` output (topic count,
  pasted pages) and run `pytest tests/test_kb_realdata.py`.
- **Program pages pasted without a marker.** In the real document, Computing Applications, Criminology, Human Resources
  Development and Interdisciplinary Studies each sit inside another program's topic under a bold title. They are only
  recovered because a catalog lists them. A pasted page that no catalog lists would be attributed to its neighbour.
- **Chunk ids are positional.** A topic inserted near the top renumbers every topic below it, so after a re-index an
  old verdict's `evidence_chunk_id` can name a different passage. Verdicts store the KB hash they were judged against,
  and the report warns on stale ones ([Step 7](s7-aggregate.md#gotchas)). Re-run `evaluate` after re-indexing.
- **Documents vs queries use different prefixes.** Chunks are embedded as documents (`is_query=False`), claims in
  step 5 as queries. Getting this backwards raises no error and quietly makes every search worse.
- **Batching embeddings is a trap.** Passing a list to Gemini's `embed_content` returns *one* blended vector for the
  whole list. That's why [`embed_texts`](../../ava_eval/llm/client.py) calls once per text.
- **Changing embedding model or `AVA_EMBED_DIM` requires `index-kb --force`.** Otherwise the stored vectors have
  the old size and step 5 stops with a "dimensions" error.
- **`realdata/online.docx` is not committed** (it's in `.gitignore`). The default `AVA_KB_FILE` is the generated sample
  KB, so run `python scripts/make_sample_kb.py` on a fresh clone.

## Tests

- [`tests/test_kb.py`](../../tests/test_kb.py): bold, partly bold, list and table extraction; hash sensitivity to
  formatting; version date; topic markers (both wordings); scaffold and TOC handling; questions keeping their
  sub-labels; TOC-listed labels starting a section; parent labels and when they end; catalog groups and name
  normalisation; pasted program pages; question chunks alone; packing within but never across topics; oversized
  splits with overlap only inside them; id format; headers; **content survival**.
- [`tests/test_kb_realdata.py`](../../tests/test_kb_realdata.py): the same properties on the real document, skipped
  when it isn't present. 73 topics, no chunk spills into another topic, pasted pages cited correctly, recurring
  sections never nested, every body paragraph survives.
- [`tests/test_kb_versioning.py`](../../tests/test_kb_versioning.py): chunk `kind` stored and constrained, KB
  replacement, verdicts recording the KB hash, the stale-verdict flag and warning, migration 0003 round trip.
- [`tests/test_retrieve_and_rollup.py`](../../tests/test_retrieve_and_rollup.py) checks embeddings are stored normalised.

## Connects to

- **Used by:** [Step 5](s5-retrieve.md) searches `kb_chunks`. `evaluate` refuses to start while it's empty
  ([`pipeline.py`](../../ava_eval/pipeline.py)), because every claim would otherwise come back as a false KB gap.
- **Cited by:** [Step 6](s6-judge.md) shows the judge each chunk's `section` as its source, and
  [Step 7](s7-aggregate.md) prints it next to every flag.
- **Sample data:** [`scripts/make_sample_kb.py`](../../scripts/make_sample_kb.py) generates `sample_online.docx` in the
  same format as the real document: topic markers, scaffolding, a table of contents, a catalog topic, program pages
  with nested deadline labels, a table inside an answer, and a general rule next to a program-specific override.

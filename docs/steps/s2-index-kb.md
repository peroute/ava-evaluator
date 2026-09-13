# Step 2 — Index the knowledge base

[← Step 1](s1-ingest.md) · [Overview](../overview.md) · [Step 3 →](s3-classify.md)

**Command:** `python -m ava_eval index-kb [--kb-dir DIR] [--force]` · **Code:** [`ava_eval/steps/s2_index_kb.py`](../../ava_eval/steps/s2_index_kb.py), [`ava_eval/kb/extract.py`](../../ava_eval/kb/extract.py), [`ava_eval/kb/chunk.py`](../../ava_eval/kb/chunk.py)

## What it does

Turns TTU's Word documents into something you can search by meaning. It pulls the text out (tables included),
cuts it into section-sized pieces (chunks), converts each piece into a list of numbers that captures its meaning
(an embedding), and stores those in PostgreSQL.

It runs on its own schedule, whenever TTU edits a document, not every time you evaluate. Documents whose
content hasn't changed are skipped.

## Inputs → outputs

| | |
|---|---|
| **Reads** | `*.docx` in `--kb-dir` (default `sample_data/kb/`), plus an optional `kb_meta.json` next to them |
| **Produces** | [`KbBlock`](../../ava_eval/models.py#L101) (one paragraph or table row), then [`KbChunk`](../../ava_eval/models.py#L109) |
| **Writes** | `kb_documents` (one row per file, with a content hash), `kb_chunks` (text + `vector` embedding) |

## How the code works

[`index_knowledge_base`](../../ava_eval/steps/s2_index_kb.py#L40) loops over the documents. For each one:

1. **Extract** with [`extract_docx`](../../ava_eval/kb/extract.py#L86).
   - [`_iter_block_items`](../../ava_eval/kb/extract.py#L33) walks the document's raw XML so paragraphs and tables
     come out **in the order they appear**. python-docx's `doc.paragraphs` and `doc.tables` are two separate lists,
     which would detach a table from the heading it sits under.
   - Headings aren't saved as text. They're kept as a running "heading path" (like
     `["Undergraduate Admissions", "2.1 Minimum Grade Point Average"]`) that gets attached to every block below them.
     A heading at the same level replaces its sibling instead of nesting under it ([line 123](../../ava_eval/kb/extract.py#L123)).
   - [`_flatten_table`](../../ava_eval/kb/extract.py#L58) turns each table row into one line. If the first row
     looks like a header row, each cell is labelled with its column name:
     `Program: Online BBA in General Business | Minimum GPA: 2.0 | ...`.
2. **Version date** from [`resolve_version_date`](../../ava_eval/kb/extract.py#L155): `kb_meta.json` if it has an
   entry, otherwise the file's modified time. It must parse as `YYYY-MM-DD`, or the document fails with a clear message.
3. **Skip if unchanged.** [`content_hash`](../../ava_eval/kb/extract.py#L138) is a SHA-256 of the *extracted text
   and headings*. If it matches `kb_documents.content_hash`, the document is skipped (unless `--force`).
4. **Chunk** with [`chunk_blocks`](../../ava_eval/kb/chunk.py#L68).
   - Blocks are grouped by heading path, and **a chunk never spans two sections**.
   - Paragraphs are added to a chunk until the next one would push it past `AVA_CHUNK_TARGET_CHARS` (1200).
   - The next chunk starts with the last few whole paragraphs of the previous one (the overlap,
     `AVA_CHUNK_OVERLAP_RATIO` = 15% of the target), handled by [`_overlap_tail`](../../ava_eval/kb/chunk.py#L54).
   - A single paragraph bigger than a whole chunk is split on sentence boundaries by
     [`_split_long_block`](../../ava_eval/kb/chunk.py#L31). This is the only mid-paragraph split.
   - Chunk ids are readable: `{doc}__{section_slug}__{ordinal:03d}`.
5. **Embed** with [`embed_texts(..., is_query=False, titles=sections)`](../../ava_eval/llm/client.py#L132). Each
   chunk is prefixed `title: <section> | text: <chunk>` before embedding.
6. **Store:** [`upsert_document`](../../ava_eval/db/store.py#L238), then
   [`delete_chunks_for_doc`](../../ava_eval/db/store.py#L263), then
   [`insert_chunks`](../../ava_eval/db/store.py#L268). The insert scales every vector to length 1
   ([`normalise`](../../ava_eval/db/store.py#L144)) before saving. Step 5 relies on that.

Every per-document failure (unreadable file, bad date, no text, embedding error) goes into `report.failed`, and the
loop moves on to the next document.

## Stub vs live

| `AVA_STUB_EMBED=1` (default) | `AVA_STUB_EMBED=0` |
|---|---|
| [`_fake_embed`](../../ava_eval/llm/client.py#L104): a hashed bag of words plus word pairs. Deterministic, and texts with shared words land near each other, so offline search still returns plausible chunks. | `gemini-embedding-2`, **one API call per chunk**, with retries and backoff. |

Extraction and chunking are always real.

## Why it's built this way

- **Chunks follow the document's sections, not a fixed window.** A fixed 500-token window will happily cut between
  "minimum GPA of 2.0" and "for the Online BBA", leaving two chunks that are each misleading on their own. It also
  means each chunk has exactly one section to cite.
- **Tables are extracted, and labelled.** Policy documents put the GPA minimums and deadlines in tables. A naive
  extractor returns none of it, and the missing content later shows up as `NOT_FOUND` verdicts that look exactly
  like real gaps in the KB.
- **Our own chunking, not Ava's.** Phase 1 asks "is this claim true according to the KB's *content*?", not "did Ava's
  retrieval do a good job?". Copying Ava's chunk boundaries would inherit Ava's blind spots and tell us nothing.
- **Hash the extracted text, not the file bytes.** Opening and re-saving a `.docx` changes its bytes without
  changing a word. Re-embedding a document because someone opened it wastes API quota.

## Gotchas

- **Documents vs queries use different prefixes.** Chunks are embedded as documents (`is_query=False`), claims in
  step 5 as queries. Getting this backwards raises no error and quietly makes every search worse.
- **Batching embeddings is a trap.** Passing a list to Gemini's `embed_content` returns *one* blended vector for the
  whole list. That's why [`embed_texts`](../../ava_eval/llm/client.py#L171) calls once per text.
- **Changing embedding model or `AVA_EMBED_DIM` requires `index-kb --force`.** Otherwise the stored vectors have
  the old size and step 5 stops with a "dimensions" error.
- **Deleting a `.docx` from the folder doesn't remove it from the index.** The loop only sees files that exist,
  so old chunks stay searchable until the rows are deleted.
- Word's lock files (`~$foo.docx`) are ignored on purpose ([line 61](../../ava_eval/steps/s2_index_kb.py#L61)).

## Tests

[`tests/test_kb.py`](../../tests/test_kb.py): heading paths, sibling headings replacing each other, table rows
extracted, labelled, and inheriting their heading, hash stability, version-date sidecar and fallback, chunks never
straddling sections, the size target, overlap on and off, oversized paragraphs, readable unique ids.
[`tests/test_retrieve_and_rollup.py`](../../tests/test_retrieve_and_rollup.py) checks embeddings are stored normalised.

## Connects to

- **Used by:** [Step 5](s5-retrieve.md) searches `kb_chunks`. `evaluate` refuses to start while it's empty
  ([`pipeline.py:77`](../../ava_eval/pipeline.py#L77)), because every claim would otherwise come back as a false KB gap.
- **Sample data:** [`scripts/make_sample_kb.py`](../../scripts/make_sample_kb.py) generates three `.docx` files with a
  heading hierarchy, a policy table, and a general rule next to a program-specific override.

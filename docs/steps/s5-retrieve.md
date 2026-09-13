# Step 5 — Retrieve evidence

[← Step 4](s4-decompose.md) · [Overview](../overview.md) · [Step 6 →](s6-judge.md)

**Runs inside:** `python -m ava_eval evaluate` · **Code:** [`ava_eval/steps/s5_retrieve.py`](../../ava_eval/steps/s5_retrieve.py), [`store.search_chunks`](../../ava_eval/db/store.py#L329)

## What it does

For every claim, finds the knowledge-base chunks most likely to confirm or refute it. The claim text is turned into an
embedding, and PostgreSQL returns the top `k` chunks whose embeddings point in the most similar direction (highest
cosine similarity). Every result is saved, so you can always see exactly what evidence the judge was shown.

## Inputs → outputs

| | |
|---|---|
| **Reads** | `Claim`s (step 4), `kb_chunks` + `kb_documents` (step 2) |
| **Produces** | `dict[claim_id, list[RetrievedChunk]]`. [`RetrievedChunk`](../../ava_eval/models.py#L207) has `chunk_id`, `text`, `score`, `source_doc`, `section`, `rank` |
| **Writes** | `claim_retrievals` (`claim_id`, `chunk_id`, `attempt`, `rank`, `score`) |

## How the code works

1. [`retrieve_for_claims(conn, claims, k=None, attempt=1)`](../../ava_eval/steps/s5_retrieve.py#L56) checks the index isn't empty.
2. [`embed_claims`](../../ava_eval/steps/s5_retrieve.py#L33) embeds all claims **in parallel** with a thread pool
   (`AVA_MAX_WORKERS`, default 4). Each claim goes through `embed_texts([text], is_query=True)`, which adds the prefix
   `task: fact checking | query: <claim>`.
3. Back on the main thread, for each claim:
   - [`store.search_chunks(conn, vector, k)`](../../ava_eval/db/store.py#L329) runs
     ```sql
     SELECT c.chunk_id, c.text, c.section, d.source_doc,
            -(c.embedding <#> %(query)s) AS score
     FROM kb_chunks c JOIN kb_documents d ON d.doc_id = c.doc_id
     ORDER BY c.embedding <#> %(query)s, c.chunk_id
     LIMIT %(k)s
     ```
     `<#>` is pgvector's *negative* inner product. Both sides are scaled to length 1 ([`normalise`](../../ava_eval/db/store.py#L144)),
     and for vectors of length 1 the inner product *is* the cosine similarity. So `-(a <#> b)` is the similarity score,
     and sorting ascending by `<#>` puts the best match first. `chunk_id` breaks ties so the order is always the same.
   - [`store.save_retrievals`](../../ava_eval/db/store.py#L512) saves the results with the `attempt` number.
4. `k` defaults to `AVA_TOP_K` (5). Step 6 calls this function again with `k = 5 × AVA_RETRY_K_MULTIPLIER` (15) and `attempt=2`.

Before searching, `search_chunks` checks that every stored embedding has the same size, and that the query is that size
too. If not, it raises a message telling you to run `index-kb --force`.

## Stub vs live

| `AVA_STUB_EMBED=1` | `AVA_STUB_EMBED=0` |
|---|---|
| Hashed bag-of-words vectors (same as step 2). Retrieval works on shared words, not meaning. | `gemini-embedding-2` with the query prefix. |

The SQL search is always real.

## Why it's built this way

- **One search per claim, not per turn.** A reply stating a GPA rule *and* a transcript rule touches two different parts
  of the KB. One combined search would favour whichever fact dominated the query. This works because step 4 made each
  claim stand on its own.
- **Exact search, deliberately no vector index.** An approximate index (HNSW/IVFFlat) is faster at large scale but can skip
  the true best match. Here, a skipped supporting chunk becomes a `NOT_FOUND` blamed on Ava when it was really our
  search that failed. At KB scale an exact scan takes milliseconds, and the Gemini calls around it take seconds.
  Revisit only past about 100k chunks, and measure recall against exact search before trusting an index.
- **Threads, not asyncio.** The only slow part is waiting on the network, and Python releases its global lock (the GIL)
  while waiting. Parallel is safe here, unlike in step 4, because the lookups don't depend on each other.
- **Worker threads never touch the database connection.** Only the main thread does. psycopg connections aren't meant
  to be shared across threads mid-query.

## Gotchas

- **Query vs document prefix.** Claims must be embedded with `is_query=True`. The wrong prefix still returns results,
  just worse ones, with nothing to warn you.
- **Switching from the stub embedder to Gemini** (or changing `AVA_EMBED_DIM`) without `index-kb --force` triggers the
  dimension error. That's intended: the alternative is pgvector's unhelpful "different vector dimensions".
- `claim_retrievals.chunk_id` has **no foreign key** to `kb_chunks`, so re-indexing a document never fails because of
  old retrieval history. Reads join to `kb_chunks`, so rows pointing to deleted chunks simply stop appearing.

## Tests

[`tests/test_retrieve_and_rollup.py`](../../tests/test_retrieve_and_rollup.py): nearest first, scores descending,
`k` larger or smaller than the index, empty index, query normalisation, **pgvector results match a brute-force numpy
comparison** ([`test_search_is_exact_brute_force`](../../tests/test_retrieve_and_rollup.py#L88)), stored vectors are unit length.
[`test_dimension_mismatch_explains_itself`](../../tests/test_judge_policy.py#L185) covers the error message.

## Connects to

- **Previous:** [Step 4](s4-decompose.md) made claims self-contained. [Step 2](s2-index-kb.md) built the index.
- **Next:** [Step 6](s6-judge.md) receives the results dict, and calls back into this step for the wider retry.
- **API:** `GET /claims/{id}` returns `attempt_1` and `attempt_2` retrievals via [`store.get_retrievals`](../../ava_eval/db/store.py#L536).

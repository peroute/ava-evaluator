"""Step 5 — find the knowledge base chunks relevant to each claim.

Retrieval is per *claim*, not per turn. A reply that states a GPA requirement
and a transcript requirement produces two claims about quite different parts of
the knowledge base; one shared search would serve whichever claim happened to
dominate the query and quietly under-serve the other. Per-claim search works
here precisely because step 4 already made each claim self-contained — a
compounding payoff from that earlier decision.

Claims are embedded **in parallel**, which is a deliberate exception to step 4's
sequential rule. The distinction is that these calls share no reasoning: each is
an independent lookup against a fixed index, so there is nothing that can drift
between them. Threads rather than asyncio because the only cost is waiting on
the network, and the GIL is released during that wait. The worker threads never
touch the database connection; only this thread does.

The similarity search itself runs in PostgreSQL (``store.search_chunks``) as an
exact pgvector scan. Nothing in this module knows how similarity is computed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Sequence

from ..config import settings
from ..db import store
from ..db.store import Connection
from ..llm.client import embed_texts
from ..models import Claim, RetrievedChunk


def embed_claims(claims: Sequence[Claim], max_workers: int | None = None) -> dict[str, list[float]]:
    """Embed claim texts in parallel, keyed by claim id.

    A claim is a *query*, so it gets the query prefix. Embedding it as a
    document would compare a short question against long passages using the
    wrong side of an asymmetric model — still returns results, just worse ones,
    with nothing to indicate anything went wrong.
    """
    workers = max_workers or settings.max_workers
    vectors: dict[str, list[float]] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(embed_texts, [claim.text], is_query=True): claim.claim_id
            for claim in claims
        }
        for future in as_completed(futures):
            claim_id = futures[future]
            vectors[claim_id] = future.result()[0]

    return vectors


def retrieve_for_claims(
    conn: Connection,
    claims: Sequence[Claim],
    k: int | None = None,
    attempt: int = 1,
) -> dict[str, list[RetrievedChunk]]:
    """Retrieve and persist the top-k chunks for each claim.

    Args:
        conn: An open database connection.
        claims: The claims to search for.
        k: How many chunks to retrieve. Defaults to the configured top-k.
        attempt: Retrieval round. 1 is the normal pass; step 6 re-runs this with
            attempt 2 and a wider k when a claim comes back NOT_FOUND.

    Returns:
        Retrieved chunks per claim id, best first.

    Raises:
        RuntimeError: if the knowledge base has not been indexed.
    """
    if not claims:
        return {}

    top_k = k or settings.top_k
    if store.count_chunks(conn) == 0:
        raise RuntimeError("The knowledge base index is empty. Run 'index-kb' before evaluating.")

    vectors = embed_claims(claims)
    results: dict[str, list[RetrievedChunk]] = {}

    for claim in claims:
        chunks = store.search_chunks(conn, vectors[claim.claim_id], top_k)
        results[claim.claim_id] = chunks
        store.save_retrievals(conn, claim.claim_id, attempt, chunks)

    return results

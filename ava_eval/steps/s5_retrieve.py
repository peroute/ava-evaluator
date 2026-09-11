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
the network, and the GIL is released during that wait.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from ..config import settings
from ..db import store
from ..llm.client import embed_texts
from ..models import Claim, RetrievedChunk


@dataclass
class KbIndex:
    """Every chunk embedding held as one matrix, with parallel metadata."""

    meta: list[dict[str, Any]]
    matrix: np.ndarray

    @property
    def size(self) -> int:
        """Number of indexed chunks."""
        return len(self.meta)

    def search(self, query_vector: Sequence[float], k: int) -> list[RetrievedChunk]:
        """Return the ``k`` most similar chunks to a query vector.

        Both the stored chunks and the query are unit-normalised, so cosine
        similarity reduces to a dot product and the entire search is one matrix
        multiply. ``argpartition`` then finds the top k without sorting all n
        scores — O(n) instead of O(n log n).

        This is the function that becomes ``ORDER BY embedding <=> %s LIMIT k``
        when the index moves to pgvector. Nothing else in the codebase has to
        know that similarity search happened in Python.
        """
        if self.size == 0:
            return []

        query = np.asarray(query_vector, dtype=np.float32)

        # A stored index and a live query can disagree on dimensionality after
        # AVA_EMBED_DIM changes or the embedding model is swapped -- which is
        # exactly what happens the first time you move from the fake embedder
        # to a real one. Without this check numpy raises a matmul shape error
        # that says nothing about the actual cause or the actual fix.
        if query.shape[0] != self.matrix.shape[1]:
            raise RuntimeError(
                f"Query embedding has {query.shape[0]} dimensions but the stored "
                f"index has {self.matrix.shape[1]}. The knowledge base was indexed "
                f"with a different embedding model or dimension -- re-run "
                f"'index-kb --force' to rebuild it."
            )

        norm = float(np.linalg.norm(query))
        if norm > 0.0:
            query = query / norm

        scores = self.matrix @ query

        k = min(k, self.size)
        if k >= self.size:
            order = np.argsort(-scores)
        else:
            candidates = np.argpartition(-scores, k - 1)[:k]
            order = candidates[np.argsort(-scores[candidates])]

        results: list[RetrievedChunk] = []
        for rank, index in enumerate(order[:k]):
            record = self.meta[int(index)]
            results.append(
                RetrievedChunk(
                    chunk_id=record["chunk_id"],
                    text=record["text"],
                    score=float(scores[int(index)]),
                    source_doc=record["source_doc"],
                    section=record["section"],
                    rank=rank,
                )
            )
        return results


def load_index(conn: sqlite3.Connection) -> KbIndex:
    """Load the whole KB index into memory.

    Loading everything at once is reasonable at knowledge-base scale — a few
    thousand chunks at 768 dimensions is a handful of megabytes — and it means
    a batch of claims pays the load cost once rather than per search.
    """
    meta, matrix = store.load_chunk_matrix(conn)
    return KbIndex(meta=meta, matrix=matrix)


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
    conn: sqlite3.Connection,
    claims: Sequence[Claim],
    k: int | None = None,
    attempt: int = 1,
    index: KbIndex | None = None,
) -> dict[str, list[RetrievedChunk]]:
    """Retrieve and persist the top-k chunks for each claim.

    Args:
        conn: An open database connection.
        claims: The claims to search for.
        k: How many chunks to retrieve. Defaults to the configured top-k.
        attempt: Retrieval round. 1 is the normal pass; step 6 re-runs this with
            attempt 2 and a wider k when a claim comes back NOT_FOUND.
        index: A preloaded index, to avoid reloading it per batch.

    Returns:
        Retrieved chunks per claim id, best first.
    """
    if not claims:
        return {}

    top_k = k or settings.top_k
    kb = index or load_index(conn)
    if kb.size == 0:
        raise RuntimeError("The knowledge base index is empty. Run 'index-kb' before evaluating.")

    vectors = embed_claims(claims)
    results: dict[str, list[RetrievedChunk]] = {}

    for claim in claims:
        chunks = kb.search(vectors[claim.claim_id], top_k)
        results[claim.claim_id] = chunks
        store.save_retrievals(conn, claim.claim_id, attempt, chunks)

    return results

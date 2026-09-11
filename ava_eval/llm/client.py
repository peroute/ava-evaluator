"""The only module that talks to Gemini.

Everything provider-specific lives here: model names, the SDK call shape, the
embedding prefixes, retry policy. Steps call ``embed_texts`` and
``generate_structured`` and know nothing else.

SDK surface verified against ai.google.dev on 2026-09-11:

* Generation is ``client.interactions.create(...)``, and structured output is
  requested via ``response_format={"type": "text", "mime_type":
  "application/json", "schema": <json schema>}``. The result is JSON text on
  ``interaction.output_text``.
* Embeddings are ``client.models.embed_content(...)`` on ``gemini-embedding-2``.

Two behaviours of that embedding model shape the code below and are easy to get
wrong:

1. ``task_type`` no longer exists. You steer the model with **text prefixes**,
   and the distinction matters for us: a claim is a short question-like string,
   a KB chunk is a long passage, and the model has a mode built for exactly
   that mismatch (asymmetric retrieval). Drop the prefixes and you lose the
   accuracy the model was designed for, with no error to warn you.
2. Passing a *list* to ``contents`` returns ONE aggregated embedding for the
   whole list, not one per item. Embedding a batch of chunks that way would
   produce a single meaningless average. So chunks go one call at a time.
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from typing import Sequence, Type, TypeVar

from pydantic import BaseModel

from ..config import settings

T = TypeVar("T", bound=BaseModel)

# Prefix templates, straight from the Gemini Embedding 2 guidance.
QUERY_PREFIX = "task: fact checking | query: {text}"
DOCUMENT_PREFIX = "title: {title} | text: {text}"

_MAX_ATTEMPTS = 4
_BASE_BACKOFF_SECONDS = 1.5

_client = None


class LlmError(RuntimeError):
    """Raised when a Gemini call fails after exhausting retries."""


def _get_client():
    """Create the SDK client on first use.

    Lazy so that stub mode -- and the whole test suite -- runs with no API key
    present and without importing the SDK at all.
    """
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise LlmError(
                "GEMINI_API_KEY is not set. Either set it in .env, or run with "
                "AVA_STUB_LLM=1 / AVA_STUB_EMBED=1 to work offline."
            )
        from google import genai  # imported here so stub mode has no hard dependency

        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _with_retries(operation, description: str):
    """Run ``operation`` with exponential backoff and jitter.

    Free-tier quota shows up as transient failures under any real batch, and a
    retry storm from several worker threads hitting the limit in lockstep is
    worse than the original problem -- hence the jitter, which spreads the
    retries out instead of synchronising them.
    """
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:  # SDK exception types vary; treat all as retryable
            last_error = exc
            if attempt == _MAX_ATTEMPTS:
                break
            delay = _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            time.sleep(delay + random.uniform(0.0, 0.5))
    raise LlmError(f"{description} failed after {_MAX_ATTEMPTS} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _fake_embed(text: str, dim: int) -> list[float]:
    """A deterministic offline stand-in for a real embedding.

    This is not a toy random vector. It is a hashed bag of words (feature
    hashing): every token is hashed to a bucket and counted, so two texts that
    share vocabulary end up pointing in a similar direction. That means offline
    retrieval returns *plausible* chunks rather than noise, and the end-to-end
    demo shows something meaningful before you spend a single API call.

    Note the use of blake2b rather than Python's built-in ``hash()``: string
    hashing is randomised per process, so ``hash()`` would produce different
    vectors on every run and silently break the stored index.
    """
    vector = [0.0] * dim
    tokens = _TOKEN_RE.findall(text.lower())
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest, "big") % dim
        vector[bucket] += 1.0
    # Bigrams give a little word-order signal, so "2.0 GPA" and "GPA 2.0"
    # are not identical to unrelated text that happens to contain both tokens.
    for first, second in zip(tokens, tokens[1:]):
        digest = hashlib.blake2b(f"{first}_{second}".encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest, "big") % dim
        vector[bucket] += 0.5
    return vector


def embed_texts(
    texts: Sequence[str],
    *,
    is_query: bool,
    titles: Sequence[str] | None = None,
) -> list[list[float]]:
    """Embed texts for retrieval.

    Args:
        texts: The raw strings -- claim text for queries, chunk text for documents.
        is_query: True for claims being looked up, False for KB chunks being indexed.
            This picks the prefix, which is how Gemini Embedding 2 is told what
            kind of thing it is looking at.
        titles: Section headings, used as the document title. Ignored for queries.

    Returns:
        One vector per input text, in the same order.
    """
    if titles is not None and len(titles) != len(texts):
        raise ValueError(f"{len(texts)} texts but {len(titles)} titles")

    prepared: list[str] = []
    for index, text in enumerate(texts):
        if is_query:
            prepared.append(QUERY_PREFIX.format(text=text))
        else:
            title = titles[index] if titles else ""
            prepared.append(DOCUMENT_PREFIX.format(title=title, text=text))

    if settings.stub_embed:
        return [_fake_embed(item, settings.embed_dim) for item in prepared]

    from google.genai import types

    client = _get_client()
    vectors: list[list[float]] = []

    # One call per text -- see the module docstring. Batching here would return
    # a single blended vector for the whole list.
    for item in prepared:

        def _call(payload: str = item):
            return client.models.embed_content(
                model=settings.embed_model,
                contents=payload,
                config=types.EmbedContentConfig(output_dimensionality=settings.embed_dim),
            )

        result = _with_retries(_call, f"embed_content({settings.embed_model})")
        vectors.append(list(result.embeddings[0].values))

    return vectors


# ---------------------------------------------------------------------------
# Structured generation
# ---------------------------------------------------------------------------


def generate_structured(
    prompt: str,
    schema: Type[T],
    *,
    system_instruction: str | None = None,
    temperature: float = 0.0,
) -> T:
    """Ask Gemini a question and get back a validated instance of ``schema``.

    Constraining the model to a JSON Schema is what makes a judge usable in a
    pipeline: the alternative is parsing prose, which fails in new and creative
    ways forever. Temperature defaults to 0 because an evaluator that returns a
    different verdict on the same evidence twice is not an evaluator.

    Raises:
        LlmError: if the call fails after retries, or the reply does not
            validate against ``schema``.
    """
    client = _get_client()

    def _call():
        return client.interactions.create(
            model=settings.judge_model,
            input=prompt,
            system_instruction=system_instruction,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": schema.model_json_schema(),
            },
            # The SDK documents response_mime_type as required whenever
            # response_format is set, even though the format already carries a
            # mime_type of its own. Sending both is harmless and avoids relying
            # on which of the two the server reads.
            response_mime_type="application/json",
            generation_config={"temperature": temperature},
        )

    interaction = _with_retries(_call, f"interactions.create({settings.judge_model})")

    try:
        return schema.model_validate_json(interaction.output_text)
    except Exception as exc:
        raise LlmError(
            f"{settings.judge_model} returned output that does not match "
            f"{schema.__name__}: {exc}"
        ) from exc

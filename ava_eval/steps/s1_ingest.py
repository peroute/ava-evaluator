"""Step 1 — acquire, validate and store transcripts.

This step is the project's only point of contact with the transcript *format*.
Everything downstream is written against the flat ``Turn`` records it produces,
which is what makes the likely future switch to the live ElevenLabs API a matter
of adding one source class here rather than editing five steps::

    TranscriptSource ──▶ FetchedConversation (raw payload + canonical payload)
                                   │
                          parse_export_v1 (validate + normalise)
                           ┌───────┴────────┐
                        invalid           valid
                           │                │
          conversations: rejected,     conversations: accepted, raw payload kept
          raw payload kept, turns      turns replaced with exactly the new ones
          removed                           │
                                        Steps 3–7

It also fails early and locally. A conversation missing a field is recorded as
rejected, with the reason, and the batch continues — one malformed export should
not cost you the other four, and the rejection needs to be visible rather than
silently absent from the totals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..db import store
from ..models import Conversation, ConversationStatus, RejectedConversation, Turn

# ---------------------------------------------------------------------------
# Reports, errors and source records
# ---------------------------------------------------------------------------


@dataclass
class IngestReport:
    """What one ingest run did, for the CLI to print."""

    accepted: list[Conversation] = field(default_factory=list)
    rejected: list[RejectedConversation] = field(default_factory=list)
    # Inputs that could not be read at all (a bad file, an undecodable API item).
    unreadable_files: list[tuple[str, str]] = field(default_factory=list)
    # Conversations whose storage failed unexpectedly; each was rolled back whole.
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def turn_count(self) -> int:
        """Total turns across accepted conversations."""
        return sum(len(c.turns) for c in self.accepted)


class TranscriptFormatError(ValueError):
    """A conversation payload did not match the expected export shape."""


@dataclass
class FetchedConversation:
    """One conversation as delivered by a source, before validation.

    ``raw_payload`` is the provider's original object, stored untouched for
    auditing. ``canonical_payload`` is the same data mapped into the export-v1
    shape that ``parse_export_v1`` validates. For the JSON export they are the
    same object; for an API they usually differ.
    """

    origin: str  # stored as conversations.source_file: a filename, or e.g. "api:<id>"
    source_type: str  # stored as conversations.source_type
    position: int = 0  # index within ``origin``, for naming payloads with no session id
    raw_payload: Any = None
    canonical_payload: Any = None
    error: str | None = None  # set when the input could not be read or decoded at all


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class TranscriptSource:
    """Somewhere transcripts come from.

    Subclasses yield one ``FetchedConversation`` per conversation and must not
    raise for bad input: an item that cannot be read is yielded with ``error``
    set, so the rest of the batch still runs.

    Adapters should *map* fields into the canonical shape, not validate them. A
    missing field should simply be missing from ``canonical_payload``, so that
    ``parse_export_v1`` rejects it with the same reason for every source.
    """

    name = "unknown"

    def fetch_conversations(self) -> Iterator[FetchedConversation]:
        """Yield every conversation this source currently has."""
        raise NotImplementedError


class JsonTranscriptSource(TranscriptSource):
    """Exported JSON files: a directory of ``.json`` files, or a single file.

    Each file holds one conversation object or a list of them. The export format
    already *is* the canonical shape, so the raw and canonical payloads match.
    """

    name = "json_export"

    def __init__(self, input_path: Path) -> None:
        if not input_path.exists():
            raise FileNotFoundError(f"No such path: {input_path}")
        self.input_path = input_path

    def fetch_conversations(self) -> Iterator[FetchedConversation]:
        path = self.input_path
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for file_path in files:
            name = file_path.name
            try:
                payloads = _load_payloads(file_path)
            except (json.JSONDecodeError, OSError) as exc:
                yield FetchedConversation(origin=name, source_type=self.name, error=str(exc))
                continue

            for position, payload in enumerate(payloads):
                yield FetchedConversation(
                    origin=name,
                    source_type=self.name,
                    position=position,
                    raw_payload=payload,
                    canonical_payload=payload,
                )


# A live API source slots in beside JsonTranscriptSource. Only the mapping is
# provider-specific; validation, storage and Steps 3-7 do not change:
#
#     class ElevenLabsApiTranscriptSource(TranscriptSource):
#         name = "elevenlabs_api"
#
#         def fetch_conversations(self):
#             for response in client.list_conversations():
#                 yield FetchedConversation(
#                     origin=f"api:{response.get('conversation_id')}",
#                     source_type=self.name,
#                     raw_payload=response,
#                     canonical_payload=map_elevenlabs_api(response),
#                 )
#
#     def map_elevenlabs_api(response):
#         return {"provider_session_id": response.get("conversation_id"),
#                 "raw_pairs": [...one {pair_index, caller_asked, agent_said} per exchange...]}


def _load_payloads(path: Path) -> list[dict[str, Any]]:
    """Read a JSON file holding either one conversation or a list of them."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return [data]


# ---------------------------------------------------------------------------
# Validation and normalisation of the canonical shape
# ---------------------------------------------------------------------------


def _require_text(payload: dict[str, Any], key: str, where: str) -> str:
    """Pull a non-empty string out of a payload or explain what was wrong."""
    value = payload.get(key)
    if value is None:
        raise TranscriptFormatError(f"{where}: missing {key!r}")
    if not isinstance(value, str):
        raise TranscriptFormatError(
            f"{where}: {key!r} must be a string, got {type(value).__name__}"
        )
    if not value.strip():
        raise TranscriptFormatError(f"{where}: {key!r} is empty")
    return value.strip()


def parse_export_v1(payload: dict[str, Any], source_file: str) -> Conversation:
    """Validate a canonical (export-v1) payload and normalise it into a Conversation.

    Expected shape::

        {"provider_session_id": "...",
         "raw_pairs": [{"pair_index": 0, "caller_asked": "...", "agent_said": "..."}]}

    Every source maps its own format into this shape first, so this is the one
    place transcript validity is decided.

    Raises:
        TranscriptFormatError: on any validation failure, with the specific cause.
    """
    if not isinstance(payload, dict):
        raise TranscriptFormatError(
            f"{source_file}: expected a JSON object, got {type(payload).__name__}"
        )

    session_id = _require_text(payload, "provider_session_id", source_file)

    raw_pairs = payload.get("raw_pairs")
    if raw_pairs is None:
        raise TranscriptFormatError(f"{session_id}: missing 'raw_pairs'")
    if not isinstance(raw_pairs, list):
        raise TranscriptFormatError(f"{session_id}: 'raw_pairs' must be a list")
    if not raw_pairs:
        raise TranscriptFormatError(f"{session_id}: 'raw_pairs' is empty")

    turns: list[Turn] = []
    seen: set[int] = set()

    for position, pair in enumerate(raw_pairs):
        where = f"{session_id}[{position}]"
        if not isinstance(pair, dict):
            raise TranscriptFormatError(f"{where}: expected an object")

        pair_index = pair.get("pair_index")
        if not isinstance(pair_index, int) or isinstance(pair_index, bool):
            raise TranscriptFormatError(f"{where}: 'pair_index' must be an integer")
        if pair_index in seen:
            raise TranscriptFormatError(f"{where}: duplicate pair_index {pair_index}")
        seen.add(pair_index)

        turns.append(
            Turn(
                session_id=session_id,
                pair_index=pair_index,
                caller_asked=_require_text(pair, "caller_asked", where),
                agent_said=_require_text(pair, "agent_said", where),
            )
        )

    # Ordering is not cosmetic: step 4 sends the transcript to the model as a
    # narrative and resolves each claim's scope against what came before it. An
    # out-of-order or gappy transcript silently corrupts that reasoning, so it
    # is rejected here instead.
    expected = list(range(len(turns)))
    actual = sorted(seen)
    if actual != expected:
        missing = sorted(set(expected) - seen)
        raise TranscriptFormatError(
            f"{session_id}: pair_index values are not sequential from 0 "
            f"(got {actual}, missing {missing})"
        )

    turns.sort(key=lambda t: t.pair_index)
    return Conversation(session_id=session_id, source_file=source_file, turns=turns)


def _fallback_session_id(payload: Any, source_file: str, position: int) -> str:
    """Best-effort id for a conversation that failed validation.

    A rejected conversation still gets a row, so it still needs a primary key.
    Falling back to the filename keeps the rejection visible in reports even
    when the payload was too broken to name itself.
    """
    if isinstance(payload, dict):
        candidate = payload.get("provider_session_id")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return f"{source_file}#{position}"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _store_conversation(
    conn: Any, item: FetchedConversation
) -> Conversation | RejectedConversation:
    """Validate one fetched conversation and write its latest state.

    Afterwards the database reflects this version only: the conversation row
    holds its raw payload and status, and ``turns`` holds exactly its turns, or
    none at all if it was rejected, so no later step can evaluate stale turns.
    """
    try:
        conversation = parse_export_v1(item.canonical_payload, item.origin)
    except TranscriptFormatError as exc:
        session_id = _fallback_session_id(item.canonical_payload, item.origin, item.position)
        store.upsert_conversation(
            conn,
            session_id,
            item.origin,
            ConversationStatus.REJECTED,
            str(exc),
            source_type=item.source_type,
            raw_payload=item.raw_payload,
        )
        store.delete_turns(conn, session_id)
        return RejectedConversation(session_id=session_id, source_file=item.origin, reason=str(exc))

    store.upsert_conversation(
        conn,
        conversation.session_id,
        item.origin,
        ConversationStatus.ACCEPTED,
        source_type=item.source_type,
        raw_payload=item.raw_payload,
    )
    store.replace_turns(conn, conversation.session_id, conversation.turns)
    return conversation


def ingest(source: TranscriptSource, db_path: Path | None = None) -> IngestReport:
    """Ingest every conversation a source yields into the database.

    Each conversation is written atomically: its row and its turns are updated
    together or not at all. A conversation whose write fails unexpectedly is
    rolled back, recorded in ``report.failed``, and the batch continues.

    Returns:
        A report of what was accepted, rejected, unreadable and failed.
    """
    report = IngestReport()

    with store.connect(db_path) as conn:
        for item in source.fetch_conversations():
            if item.error is not None:
                report.unreadable_files.append((item.origin, item.error))
                continue

            try:
                with store.savepoint(conn, "ingest_conversation"):
                    outcome = _store_conversation(conn, item)
            except Exception as exc:
                # Same policy as pipeline.evaluate: one bad conversation should
                # not end the batch. The savepoint has already undone its writes.
                report.failed.append((item.origin, f"{type(exc).__name__}: {exc}"))
                continue

            if isinstance(outcome, RejectedConversation):
                report.rejected.append(outcome)
            else:
                report.accepted.append(outcome)

    return report


def ingest_directory(input_path: Path, db_path: Path | None = None) -> IngestReport:
    """Ingest every transcript JSON under ``input_path`` into the database.

    Args:
        input_path: A directory of ``.json`` files, or a single ``.json`` file.
        db_path: Override the configured database location.

    Returns:
        A report of what was accepted, rejected, unreadable and failed.

    Raises:
        FileNotFoundError: if ``input_path`` does not exist.
    """
    return ingest(JsonTranscriptSource(input_path), db_path=db_path)

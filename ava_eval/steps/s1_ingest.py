"""Step 1 — parse and validate exported transcripts.

This step is the project's only point of contact with the transcript *format*.
Everything downstream is written against the flat ``Turn`` records it produces,
which is what makes the likely future switch to the live ElevenLabs API a matter
of adding one adapter function here rather than editing five steps.

It also fails early and locally. A conversation missing a field is recorded as
rejected, with the reason, and the batch continues — one malformed export should
not cost you the other four, and the rejection needs to be visible rather than
silently absent from the totals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db import store
from ..models import Conversation, ConversationStatus, RejectedConversation, Turn


@dataclass
class IngestReport:
    """What one ingest run did, for the CLI to print."""

    accepted: list[Conversation] = field(default_factory=list)
    rejected: list[RejectedConversation] = field(default_factory=list)
    unreadable_files: list[tuple[str, str]] = field(default_factory=list)

    @property
    def turn_count(self) -> int:
        """Total turns across accepted conversations."""
        return sum(len(c.turns) for c in self.accepted)


class TranscriptFormatError(ValueError):
    """A conversation payload did not match the expected export shape."""


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
    """Adapter for the current exported-JSON format.

    Expected shape::

        {"provider_session_id": "...",
         "raw_pairs": [{"pair_index": 0, "caller_asked": "...", "agent_said": "..."}]}

    When the live ElevenLabs API becomes a source, add ``parse_elevenlabs_api``
    beside this function and dispatch on the payload shape. Nothing outside this
    module needs to know.

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


def _load_payloads(path: Path) -> list[dict[str, Any]]:
    """Read a JSON file holding either one conversation or a list of them."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return [data]


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


def ingest_directory(input_path: Path, database_url: str | None = None) -> IngestReport:
    """Ingest every transcript JSON under ``input_path`` into the database.

    Args:
        input_path: A directory of ``.json`` files, or a single ``.json`` file.
        database_url: Override the configured database.

    Returns:
        A report of what was accepted, rejected, and unreadable.

    Raises:
        FileNotFoundError: if ``input_path`` does not exist.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"No such path: {input_path}")

    files = sorted(input_path.glob("*.json")) if input_path.is_dir() else [input_path]
    report = IngestReport()

    with store.connect(database_url) as conn:
        for file_path in files:
            name = file_path.name
            try:
                payloads = _load_payloads(file_path)
            except (json.JSONDecodeError, OSError) as exc:
                report.unreadable_files.append((name, str(exc)))
                continue

            for position, payload in enumerate(payloads):
                try:
                    conversation = parse_export_v1(payload, name)
                except TranscriptFormatError as exc:
                    session_id = _fallback_session_id(payload, name, position)
                    store.upsert_conversation(
                        conn, session_id, name, ConversationStatus.REJECTED, str(exc)
                    )
                    report.rejected.append(
                        RejectedConversation(
                            session_id=session_id, source_file=name, reason=str(exc)
                        )
                    )
                    continue

                store.upsert_conversation(
                    conn, conversation.session_id, name, ConversationStatus.ACCEPTED
                )
                store.replace_turns(conn, conversation.session_id, conversation.turns)
                report.accepted.append(conversation)

    return report

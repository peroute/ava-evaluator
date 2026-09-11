"""Orchestration: run steps 3 through 6 over ingested conversations.

Steps 1 and 2 are separate commands because they run on different triggers —
step 1 when new transcripts arrive, step 2 when TTU edits the knowledge base.
This module covers the part that runs as one unit per evaluation batch.

Conversations are processed one at a time. Parallelism lives inside steps 5 and
6, where the calls are genuinely independent; at the conversation level, keeping
it sequential means a failure is attributable to one conversation instead of
arriving out of order from four.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .db import store
from .models import ConversationStatus, Verdict
from .steps.s3_classify import classify_turns
from .steps.s4_decompose import decompose_conversation, to_claims
from .steps.s5_retrieve import load_index, retrieve_for_claims
from .steps.s6_judge import judge_claims


@dataclass
class SessionResult:
    """What evaluating one conversation produced."""

    session_id: str
    turn_count: int = 0
    flagged_turns: int = 0
    claim_count: int = 0
    supported: int = 0
    contradicted: int = 0
    not_found: int = 0
    error: str | None = None


@dataclass
class EvaluationReport:
    """What one evaluation run produced across all conversations."""

    sessions: list[SessionResult] = field(default_factory=list)

    @property
    def claim_count(self) -> int:
        """Claims judged across the whole run."""
        return sum(s.claim_count for s in self.sessions)

    @property
    def failures(self) -> list[SessionResult]:
        """Conversations that errored out."""
        return [s for s in self.sessions if s.error]


def evaluate(session_ids: list[str] | None = None, db_path: Path | None = None) -> EvaluationReport:
    """Run steps 3-6 over the requested conversations.

    Args:
        session_ids: Which conversations to evaluate. Defaults to every accepted
            conversation in the database.
        db_path: Override the configured database location.

    Returns:
        A per-conversation summary of what was judged.

    Raises:
        RuntimeError: if the knowledge base has not been indexed yet, since
            every claim would otherwise be judged against nothing and come back
            as a false KB gap.
    """
    report = EvaluationReport()

    with store.connect(db_path) as conn:
        index = load_index(conn)
        if index.size == 0:
            raise RuntimeError(
                "The knowledge base index is empty. Run 'index-kb' first -- "
                "judging against an empty index would report every claim as a "
                "KB gap."
            )

        targets = session_ids or store.list_sessions(conn, ConversationStatus.ACCEPTED)

        for session_id in targets:
            result = SessionResult(session_id=session_id)
            try:
                turns = store.get_turns(conn, session_id)
                result.turn_count = len(turns)
                if not turns:
                    result.error = "no turns found (was it rejected at ingest?)"
                    report.sessions.append(result)
                    continue

                # Step 3 -- the cheap gate.
                classifications = classify_turns(turns)
                store.save_classifications(conn, classifications)
                by_index = {c.pair_index: c for c in classifications}
                result.flagged_turns = sum(1 for c in classifications if c.contains_claims)

                # Step 4 -- one call for the whole conversation.
                response = decompose_conversation(session_id, turns, by_index)
                claims = to_claims(session_id, response, {t.pair_index for t in turns})
                store.replace_claims_for_session(conn, session_id, claims)
                result.claim_count = len(claims)

                if not claims:
                    report.sessions.append(result)
                    continue

                # Step 5 -- per-claim retrieval, parallel across claims.
                retrievals = retrieve_for_claims(conn, claims, index=index)

                # Step 6 -- verdicts, with the NOT_FOUND-only retry.
                verdicts = judge_claims(conn, claims, retrievals, index=index)
                result.supported = sum(1 for v in verdicts if v.verdict == Verdict.SUPPORTED)
                result.contradicted = sum(1 for v in verdicts if v.verdict == Verdict.CONTRADICTED)
                result.not_found = sum(1 for v in verdicts if v.verdict == Verdict.NOT_FOUND)

            except NotImplementedError as exc:
                # A deliberate stub, not a bug. Surface it plainly.
                result.error = str(exc)
            except Exception as exc:
                # One bad conversation should not end the batch, for the same
                # reason a malformed transcript does not stop ingest.
                result.error = f"{type(exc).__name__}: {exc}"

            report.sessions.append(result)

    return report

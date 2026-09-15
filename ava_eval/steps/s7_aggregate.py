"""Step 7 — roll claim verdicts up to turn and conversation level, and report.

There is almost no logic in this module, and that is the design working. The
rollups live in SQL views (the Alembic migrations in ``db/migrations``), so claim, turn and conversation
level results are three readings of one set of rows rather than three copies
that can disagree. This file only reads them and formats them.

Severity order is ``CONTRADICTED`` > ``NOT_FOUND`` > ``SUPPORTED``, and the two
flagged states stay distinct everywhere. They are not two shades of "bad":
CONTRADICTED means Ava said something the knowledge base refutes, which is an
Ava problem. NOT_FOUND means Ava said something the knowledge base cannot
confirm, which is usually a TTU content problem. Collapsing them into one
"flagged" count would hand both teams a number neither can act on.
"""

from __future__ import annotations

from typing import Any

from ..db import store
from ..db.store import Connection

# Ordered worst-first, which is the order a reviewer wants to read them in.
VERDICT_ORDER = ["CONTRADICTED", "NOT_FOUND", "SUPPORTED"]

_LABELS = {
    "CONTRADICTED": "CONTRADICTED  (Ava said something the KB refutes)",
    "NOT_FOUND": "NOT_FOUND     (KB cannot confirm it)",
    "SUPPORTED": "SUPPORTED     (grounded in the KB)",
    "UNJUDGED": "UNJUDGED      (judge failed, re-run evaluate)",
    "NO_CLAIMS": "NO_CLAIMS     (flagged, but nothing checkable found)",
    "NOT_EVALUATED": "NOT_EVALUATED (conversational filler)",
    "UNCLASSIFIED": "UNCLASSIFIED  (not yet evaluated)",
}


def conversation_report(conn: Connection, session_id: str | None = None) -> dict[str, Any]:
    """Assemble the full report payload: totals, per-conversation, per-claim."""
    return {
        "stats": store.verdict_stats(conn),
        "conversations": store.conversation_summary(conn, session_id),
        "turns": store.turn_results(conn, session_id),
        "flagged_claims": [
            row
            for verdict in ("CONTRADICTED", "NOT_FOUND")
            for row in store.claim_results(conn, session_id=session_id, verdict=verdict, limit=500)
        ],
        # Claims step 6 could not judge. Listed separately: they are not flags
        # about Ava, they are gaps in this run.
        "unjudged_claims": store.claim_results(
            conn, session_id=session_id, unjudged=True, limit=500
        ),
    }


def _bar(counts: dict[str, int], width: int = 28) -> str:
    """A tiny text bar so the verdict mix is visible at a glance."""
    total = sum(counts.values()) or 1
    segments = []
    for verdict in VERDICT_ORDER:
        filled = round(width * counts.get(verdict, 0) / total)
        symbol = {"CONTRADICTED": "#", "NOT_FOUND": "~", "SUPPORTED": "."}[verdict]
        segments.append(symbol * filled)
    return "".join(segments).ljust(width)


def format_report(report: dict[str, Any]) -> str:
    """Render the report as plain text for the terminal."""
    lines: list[str] = []
    stats = report["stats"]
    totals = stats["totals"]

    lines.append("=" * 72)
    lines.append("AVA FAITHFULNESS REPORT")
    lines.append("=" * 72)
    lines.append(
        f"{totals['conversations']} conversations | {totals['turns']} turns | "
        f"{totals['claims']} claims | {totals['kb_chunks']} KB chunks indexed"
    )
    if totals["unjudged"]:
        lines.append(f"[!] {totals['unjudged']} claims are UNJUDGED -- re-run evaluate")
    lines.append("")

    by_verdict = stats["by_verdict"]
    if by_verdict:
        lines.append("Claim verdicts")
        lines.append("-" * 72)
        for verdict in VERDICT_ORDER:
            count = by_verdict.get(verdict, 0)
            lines.append(f"  {_LABELS[verdict]:<52} {count:>4}")
        if stats["by_tag"]:
            tags = "  ".join(f"{tag}={n}" for tag, n in sorted(stats["by_tag"].items()))
            lines.append(f"  tags: {tags}")
        lines.append(f"  [{_bar(by_verdict)}]")
        lines.append("")

    lines.append("Conversations")
    lines.append("-" * 72)
    for row in report["conversations"]:
        if row["status"] != "accepted":
            lines.append(f"  {row['session_id']:<24} REJECTED: {row['note']}")
            continue
        lines.append(
            f"  {row['session_id']:<24} {row['turn_count']:>3} turns | "
            f"contradicted {row['contradicted_turns']} | "
            f"not_found {row['not_found_turns']} | "
            f"supported {row['supported_turns']} | "
            f"skipped {row['skipped_turns']} | "
            f"unjudged {row['unjudged_turns']}"
        )
    lines.append("")

    unjudged = report["unjudged_claims"]
    if unjudged:
        lines.append(f"Unjudged claims ({len(unjudged)})")
        lines.append("-" * 72)
        for row in unjudged:
            lines.append(f"  UNJUDGED  {row['session_id']} pair {row['pair_index']}")
            lines.append(f"    claim   : {row['claim_text']}")
        lines.append("")

    flagged = report["flagged_claims"]
    if not flagged:
        lines.append("No flagged claims.")
        return "\n".join(lines)

    lines.append(f"Flagged claims ({len(flagged)})")
    lines.append("-" * 72)
    for row in flagged:
        tag = f" [{row['tag']}]" if row["tag"] else ""
        lines.append(f"  {row['verdict']}{tag}  {row['session_id']} pair {row['pair_index']}")
        lines.append(f"    subject : {row['subject_tag']}")
        lines.append(f"    claim   : {row['claim_text']}")
        lines.append(f"    why     : {row['rationale']}")
        if row["evidence_chunk_id"]:
            lines.append(f"    evidence: {row['evidence_section']} ({row['evidence_chunk_id']})")
        if row.get("kb_changed_since_judged"):
            # Chunk ids are positional: after the KB changes, this id may name a
            # different passage than the one the judge actually read.
            lines.append(
                "    [!] the knowledge base changed since this verdict -- the evidence shown "
                "may not be what was judged; re-run evaluate"
            )
        lines.append("")

    return "\n".join(lines)

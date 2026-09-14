"""Command line entry points.

Each pipeline stage is its own subcommand rather than one monolithic ``run``,
because the stages have genuinely different triggers: ``index-kb`` fires when
TTU edits a document, ``ingest`` when new transcripts arrive, ``evaluate`` when
you want verdicts. Separate commands are what make the persisted intermediate
tables worth having — you can re-run any stage without redoing the others.

argparse rather than a CLI framework: this is stdlib, the command surface is
small, and one fewer dependency in a system whose value is auditability is worth
slightly less polish.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import PROJECT_ROOT, settings
from .db import store
from .db.migrate import upgrade_to_head
from .pipeline import evaluate
from .steps.s1_ingest import ingest_directory
from .steps.s2_index_kb import index_knowledge_base
from .steps.s7_aggregate import conversation_report, format_report


def _mode_banner() -> str:
    """Describe which parts of the run are offline, so results are never mistaken."""
    parts = []
    if settings.stub_llm:
        parts.append("LLM=STUB")
    if settings.stub_embed:
        parts.append("EMBED=FAKE")
    if not parts:
        return f"live ({settings.judge_model} / {settings.embed_model})"
    return " ".join(parts) + "  <- offline mode, results are not real verdicts"


def cmd_migrate(args: argparse.Namespace) -> int:
    """Bring the database schema up to the latest Alembic migration."""
    upgrade_to_head()
    print("Database schema is up to date.")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Step 1: parse and validate transcripts."""
    report = ingest_directory(Path(args.input))

    print(f"Accepted {len(report.accepted)} conversations ({report.turn_count} turns)")
    for conversation in report.accepted:
        print(f"  + {conversation.session_id:<24} {len(conversation.turns)} turns")

    if report.rejected:
        print(f"\nRejected {len(report.rejected)} conversations")
        for rejected in report.rejected:
            print(f"  - {rejected.session_id:<24} {rejected.reason}")

    if report.unreadable_files:
        print(f"\nUnreadable files ({len(report.unreadable_files)})")
        for name, reason in report.unreadable_files:
            print(f"  ! {name}: {reason}")

    return 0 if report.accepted else 1


def cmd_index_kb(args: argparse.Namespace) -> int:
    """Step 2: extract, chunk and embed the knowledge base."""
    print(f"Indexing mode: {_mode_banner()}")
    report = index_knowledge_base(Path(args.kb_file), force=args.force)

    if report.skipped:
        print(
            f"\nSkipped {report.source_doc}: content unchanged since it was last indexed "
            f"(use --force to re-index)"
        )
        return 0

    print(
        f"\nIndexed {report.source_doc}: {report.chunk_count} chunks "
        f"({report.qa_chunks} question, {report.reference_chunks} reference)"
    )
    print(
        f"  {report.topic_count} topics, "
        f"{report.grouped_topic_count} placed under a catalog group"
    )

    if report.unmarked_topics:
        # Indexed correctly, but worth telling TTU: these pages sit under another
        # program's topic with no "If asked questions about" line of their own.
        print(f"\nProgram pages found without a topic marker ({len(report.unmarked_topics)})")
        for name in report.unmarked_topics:
            print(f"  ~ {name}")

    if report.unmatched_catalog_entries:
        # Not an error: TTU lists a program but gives it no page. Claims about
        # its details can only come back NOT_FOUND, and that is a real KB gap.
        print(
            f"\nListed in a catalog but no topic in the KB "
            f"({len(report.unmatched_catalog_entries)})"
        )
        for catalog, entry in report.unmatched_catalog_entries:
            print(f"  ? {entry}  (listed in: {catalog})")

    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Steps 3-6: classify, decompose, retrieve, judge."""
    print(f"Evaluation mode: {_mode_banner()}\n")
    sessions = [args.session] if args.session else None
    report = evaluate(sessions)

    for result in report.sessions:
        if result.error:
            print(f"  ! {result.session_id:<24} {result.error}")
            continue
        print(
            f"  + {result.session_id:<24} {result.claim_count:>3} claims | "
            f"contradicted {result.contradicted} | not_found {result.not_found} | "
            f"supported {result.supported} | failed {len(result.failed_claims)}"
        )
        for claim_id, error in result.failed_claims.items():
            print(f"      x {claim_id}: {error}")

    failed = sum(len(s.failed_claims) for s in report.sessions)
    print(f"\n{report.claim_count} claims across {len(report.sessions)} conversations.")
    if failed:
        print(f"{failed} claims could not be judged (UNJUDGED) -- re-run evaluate to retry them.")
    # Exit 1 on a partial run too, so `evaluate && report` and CI don't treat it as clean.
    return 1 if report.failures or report.claim_failures else 0


def cmd_report(args: argparse.Namespace) -> int:
    """Step 7: print the aggregated report."""
    with store.connect() as conn:
        report = conversation_report(conn, args.session)
    print(format_report(report))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the read-only API the TTU staff frontend talks to."""
    import uvicorn

    print(f"Serving http://{args.host}:{args.port}  (docs at /docs)")
    uvicorn.run("ava_eval.api.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Assemble the command line interface."""
    parser = argparse.ArgumentParser(
        prog="ava_eval",
        description="Check whether Ava's answers are grounded in TTU's knowledge base.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "migrate", help="create or upgrade the database schema (Alembic)"
    ).set_defaults(func=cmd_migrate)

    ingest = subparsers.add_parser("ingest", help="step 1: load transcript JSON")
    ingest.add_argument(
        "--input",
        default=str(PROJECT_ROOT / "sample_data" / "transcripts"),
        help="directory of transcript .json files, or a single file",
    )
    ingest.set_defaults(func=cmd_ingest)

    index = subparsers.add_parser("index-kb", help="step 2: index the knowledge base")
    index.add_argument(
        "--kb-file",
        default=str(settings.kb_file),
        help="the .docx knowledge base (default: AVA_KB_FILE, else the generated sample KB)",
    )
    index.add_argument("--force", action="store_true", help="re-index even unchanged documents")
    index.set_defaults(func=cmd_index_kb)

    evaluate_cmd = subparsers.add_parser("evaluate", help="steps 3-6: produce claim verdicts")
    evaluate_cmd.add_argument("--session", help="evaluate one session id only")
    evaluate_cmd.set_defaults(func=cmd_evaluate)

    report_cmd = subparsers.add_parser("report", help="step 7: print the aggregated report")
    report_cmd.add_argument("--session", help="report on one session id only")
    report_cmd.set_defaults(func=cmd_report)

    serve = subparsers.add_parser("serve", help="run the read-only API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

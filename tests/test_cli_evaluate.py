"""The `evaluate` command's exit code.

Exit 1 on a partial run is what stops `evaluate && report` (and CI) from
treating a run with unjudged claims as clean.
"""

from __future__ import annotations

import argparse

import pytest

from ava_eval import cli
from ava_eval.pipeline import EvaluationReport, SessionResult


def _run(monkeypatch: pytest.MonkeyPatch, *sessions: SessionResult) -> int:
    monkeypatch.setattr(cli, "evaluate", lambda session_ids: EvaluationReport(list(sessions)))
    return cli.cmd_evaluate(argparse.Namespace(session=None))


def test_clean_run_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, SessionResult(session_id="s1", claim_count=2, supported=2)) == 0


def test_failed_claim_exits_one_and_is_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = SessionResult(
        session_id="s1", claim_count=2, supported=1, failed_claims={"c1": "boom"}
    )

    assert _run(monkeypatch, result) == 1
    out = capsys.readouterr().out
    assert "failed 1" in out
    assert "x c1: boom" in out


def test_errored_conversation_still_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, SessionResult(session_id="s1", error="bad")) == 1

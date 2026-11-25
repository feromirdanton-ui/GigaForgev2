"""Goal-loop guards against spending cycles on identical failed output."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gigaforge.core.goal import (
    GoalRunner,
    VerificationCheck,
    VerificationReport,
    WorkspaceVerifier,
)


class _Pipeline:
    def __init__(self, root: Path, outputs: list[str]) -> None:
        self.root = root
        self.outputs = outputs
        self.calls = 0
        self.budgets: list[int] = []

    def run(self, _goal: str, **_kwargs: Any) -> SimpleNamespace:
        content = self.outputs[min(self.calls, len(self.outputs) - 1)]
        budget = int(_kwargs.get("budget_tokens", 0) or 0)
        self.budgets.append(budget)
        self.calls += 1
        (self.root / "app.py").write_text(content, encoding="utf-8")
        return SimpleNamespace(
            accepted=True,
            status="succeeded",
            changed_files=["app.py"],
            summary="candidate",
            error="",
            tokens=min(10, budget) if budget else 10,
            calls=1,
            duration_ms=1.0,
        )


class _FailingVerifier:
    def verify(self, _root: Path, _changed: list[str], _goal: str) -> VerificationReport:
        return VerificationReport(
            [VerificationCheck("behavior", "failed", "expected output was missing")]
        )


class _UnavailableVerifier:
    def verify(self, _root: Path, _changed: list[str], _goal: str) -> VerificationReport:
        return VerificationReport(
            [VerificationCheck("browser", "unavailable", "browser runtime is missing")]
        )


def _runner(root: Path, outputs: list[str]) -> tuple[GoalRunner, _Pipeline]:
    pipeline = _Pipeline(root, outputs)
    return GoalRunner(pipeline, repo_root=root, verifier=_FailingVerifier()), pipeline


def test_repeated_failed_output_stops_after_one_retry(tmp_path: Path) -> None:
    runner, pipeline = _runner(tmp_path, ["print('same')\n"])

    result = runner.run("make the app work", max_cycles=6)

    assert pipeline.calls == 2
    assert len(result.cycles) == 2
    assert result.status == "needs_attention"
    assert not result.accepted
    assert "same output" in result.error
    assert result.cycles[0].output_fingerprint
    assert result.cycles[0].output_fingerprint == result.cycles[1].output_fingerprint


def test_changed_output_can_continue_to_the_cycle_limit(tmp_path: Path) -> None:
    runner, pipeline = _runner(
        tmp_path,
        ["print('first')\n", "print('second')\n", "print('third')\n"],
    )

    result = runner.run("make the app work", max_cycles=3)

    assert pipeline.calls == 3
    assert len(result.cycles) == 3
    assert result.status == "needs_attention"
    assert len({cycle.output_fingerprint for cycle in result.cycles}) == 3


def test_unavailable_verifier_blocks_without_spending_another_cycle(tmp_path: Path) -> None:
    pipeline = _Pipeline(tmp_path, ["print('same')\n"])
    runner = GoalRunner(pipeline, repo_root=tmp_path, verifier=_UnavailableVerifier())

    result = runner.run("make the app work", max_cycles=6)

    assert pipeline.calls == 1
    assert len(result.cycles) == 1
    assert result.status == "blocked"


def test_token_budget_is_shared_across_cycles_and_resume(tmp_path: Path) -> None:
    runner, pipeline = _runner(
        tmp_path,
        ["print('first')\n", "print('second')\n", "print('third')\n"],
    )

    first = runner.run("make the app work", max_cycles=5, budget_tokens=10)

    assert first.status == "blocked"
    assert first.tokens == first.budget_tokens == 10
    assert pipeline.budgets == [10]

    # Omitting a limit while resuming keeps the persisted cap, so the goal
    # cannot accidentally become unbounded after a restart.
    resumed = runner.run(
        goal_id=first.goal_id, resume=True, max_cycles=5
    )
    assert resumed.status == "blocked"
    assert resumed.tokens == resumed.budget_tokens == 10
    assert pipeline.budgets == [10]

    extended = runner.run(
        goal_id=first.goal_id, resume=True, max_cycles=5, budget_tokens=20
    )
    assert extended.status == "blocked"
    assert extended.tokens == extended.budget_tokens == 20
    assert pipeline.budgets == [10, 10]


def test_verifier_decodes_bytes_from_a_timed_out_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(command: list[str], **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=1,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr("gigaforge.core.goal.subprocess.run", timeout)
    check = WorkspaceVerifier(timeout_s=1)._command_check("test", ["test"], tmp_path)

    assert check.status == "failed"
    assert "partial stdout" in check.summary
    assert "partial stderr" in check.summary

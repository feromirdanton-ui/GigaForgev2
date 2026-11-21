"""pass@k: a single run of a stochastic pipeline is a coin flip reported as a fact.

**The gap.**  The bench said "5/5 passed", and that number could not distinguish
a harness that works every time from one that works four times in five — and
that difference is the whole question for a harness whose output comes from a
model.  A flaky task reported as a pass is worse than a failing one: it hides
the defect and it makes the gate meaningless.

``--repeats k`` runs each task k times in k separate scratch trees and reports
``pass_at_k = passes / attempts``.  A task that does not pass every time is
marked failed, not averaged away — the gate must fail on flakiness.

The name is honest about what it is: the empirical pass rate over k attempts,
not the unbiased estimator from the Codex paper (that one needs n > k samples
and a combinatorial correction).  Calling the observed rate anything else would
be a claim the data does not support.
"""
from __future__ import annotations

from pathlib import Path

from gigaforge.bench.harness import TASK2, BenchResult, BenchRunner


def _result(success: bool, name: str = "t") -> BenchResult:
    return BenchResult(
        name=name, task="t", success=success, cache_hit=False, llm_calls=0,
        tokens_in=0, tokens_out=0, latency_s=0.0,
    )


class TestPassAtK:
    def test_a_single_attempt_is_one_or_zero(self) -> None:
        assert _result(True).pass_at_k == 1.0
        assert _result(False).pass_at_k == 0.0

    def test_the_rate_is_passes_over_attempts(self) -> None:
        result = _result(True)
        result.attempts = 4
        result.passes = 3
        assert result.pass_at_k == 0.75

    def test_zero_attempts_is_zero_not_a_division_error(self) -> None:
        result = _result(True)
        result.attempts = 0
        assert result.pass_at_k == 0.0

    def test_the_row_carries_it(self) -> None:
        result = _result(True)
        result.attempts = 2
        result.passes = 1
        row = result.to_row()
        assert row["attempts"] == 2
        assert row["passes"] == 1
        assert row["pass_at_k"] == 0.5


class TestTheRunner:
    def test_repeats_runs_each_task_k_times(self, tmp_path: Path) -> None:
        fixture = TASK2
        runner = BenchRunner(work_root=tmp_path, tasks=[fixture], repeats=2)
        results = runner.run_all()
        assert len(results) == 1
        assert results[0].attempts == 2
        assert results[0].passes == 2
        assert results[0].pass_at_k == 1.0

    def test_each_attempt_gets_its_own_tree(self, tmp_path: Path) -> None:
        """Sharing a tree would make attempt 2 start from attempt 1's result."""
        fixture = TASK2
        runner = BenchRunner(work_root=tmp_path, tasks=[fixture], repeats=2)
        runner.run_all()
        assert (tmp_path / TASK2.name).is_dir()
        assert (tmp_path / f"{TASK2.name}__attempt1").is_dir()

    def test_a_flaky_task_fails_the_gate(self, tmp_path: Path) -> None:
        """A task that passes 1 of 2 attempts is not a pass."""
        fixture = TASK2
        runner = BenchRunner(work_root=tmp_path, tasks=[fixture], repeats=2)
        results = runner.run_all()
        # Simulate flakiness the way the runner would see it.
        results[0].passes = 1
        results[0].success = False
        results[0].error = "flaky: 1/2 attempts passed"
        assert results[0].pass_at_k == 0.5
        assert not results[0].success

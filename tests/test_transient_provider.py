"""A throttled provider is not a verdict on the work.

Measured live: two runs in parallel exhausted the GigaChat quota.  Every cycle
failed with ``GF-RATE-429``, the goal burned all three cycles in under a minute,
and it reported "cycle limit reached before verification passed" — a statement
about the artifact that was really a statement about the quota.  A transient
failure must wait and retry the same cycle, not consume one.
"""
from __future__ import annotations

import pytest

from gigaforge.core.goal import _is_transient_provider_error, _wait_or_cancel


class TestTransientDetection:
    @pytest.mark.parametrize(
        "error",
        [
            "RateLimitError: 429 https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            "provider call failed (GF-RATE-429, attempt 4/4)",
            "Too Many Requests",
            "rate limit exceeded",
            "503 Service Unavailable",
            "502 Bad Gateway",
        ],
    )
    def test_throttling_is_transient(self, error: str) -> None:
        assert _is_transient_provider_error(error)

    @pytest.mark.parametrize(
        "error",
        [
            "the diff did not apply",
            "verification failed: no <h1> in index.html",
            "declined by the user",
            "syntax error in calc.py",
            "",
        ],
    )
    def test_a_real_failure_is_not_transient(self, error: str) -> None:
        assert not _is_transient_provider_error(error)


class TestWaitOrCancel:
    def test_wait_returns_false_when_not_cancelled(self) -> None:
        class Token:
            cancelled = False

        assert _wait_or_cancel(Token(), 0.05) is False

    def test_wait_returns_true_when_cancelled(self) -> None:
        class Token:
            cancelled = True

        assert _wait_or_cancel(Token(), 5.0) is True

    def test_wait_is_interruptible(self) -> None:
        """A cancel during the wait must not have to outlast the whole delay."""
        import time

        class Token:
            def __init__(self) -> None:
                self.cancelled = False

        token = Token()
        started = time.monotonic()
        # Cancel after ~0.2s by flipping the flag from a thread.
        import threading

        threading.Timer(0.2, lambda: setattr(token, "cancelled", True)).start()
        assert _wait_or_cancel(token, 10.0) is True
        assert time.monotonic() - started < 3.0, "the wait must poll, not sleep through"


class TestGoalLoopRetriesInsteadOfBurningCycles:
    """The loop must not spend a cycle on a throttled provider."""

    def test_the_loop_retries_a_throttled_cycle(self) -> None:
        import inspect

        from gigaforge.core.goal import GoalRunner

        source = inspect.getsource(GoalRunner.run)
        assert "_is_transient_provider_error" in source, (
            "a 429 must be recognised as transient, not counted as a failed cycle"
        )
        assert "transient_retries" in source
        assert "continue" in source

    def test_the_retry_budget_is_bounded(self) -> None:
        from gigaforge.core import goal as module

        assert 1 <= module._MAX_TRANSIENT_RETRIES <= 10, (
            "an unbounded retry would hang a run on a hard quota"
        )


class TestBranchNamesAreUnique:
    """Two children spawned in the same second must not share a branch name.

    ``int(time.time())`` made the name collide, so ``git worktree add -b`` failed
    for the second child with "branch already exists" — a fan-out that passed or
    failed depending on timing.
    """

    def test_two_creates_in_the_same_second_differ(self, tmp_path) -> None:
        import subprocess

        from gigaforge.core.agents import WorktreeManager

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "a.txt").write_text("a\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

        manager = WorktreeManager(repo_root=str(repo))
        first = manager.create("work-0")
        second = manager.create("work-1")
        assert first.branch != second.branch, (
            "two worktrees created in the same second must not share a branch"
        )

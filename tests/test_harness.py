"""Tests for the harness layer: policy, recovery, and the ledger.

The harness is the part LangGraph is a bridge *to*.  These tests pin the four
things it adds over a bare graph: a retry policy that distinguishes a 429 from
a 400, recovery from context overflow instead of termination, a bounded
compaction, and a ledger that records what the run actually cost.

Offline and deterministic: the sleep function is injected, so no test waits.
"""
from __future__ import annotations

import json
from typing import Any

import pytest


# ── compaction ──────────────────────────────────────────────────────────────
def _trajectory(count: int, *, size: int = 400) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": "you are an agent"}]
    messages.append({"role": "user", "content": "fix the bug in validate_email"})
    for index in range(count):
        role = "assistant" if index % 2 == 0 else "user"
        messages.append({"role": role, "content": f"step {index}: " + "x" * size})
    return messages


def test_compaction_shrinks_the_trajectory() -> None:
    from gigaforge.harness.compaction import compact_trajectory

    original = _trajectory(20)
    compacted = compact_trajectory(original)
    before = sum(len(m["content"]) for m in original)
    after = sum(len(m["content"]) for m in compacted)
    assert after < before
    assert len(compacted) < len(original)


def test_compaction_keeps_the_system_message_first() -> None:
    """GigaChat rejects a system message that is not first with a hard 400."""
    from gigaforge.harness.compaction import compact_trajectory

    compacted = compact_trajectory(_trajectory(20))
    assert compacted[0]["role"] == "system"
    assert compacted[0]["content"] == "you are an agent"


def test_compaction_keeps_the_goal_second() -> None:
    from gigaforge.harness.compaction import compact_trajectory

    compacted = compact_trajectory(_trajectory(20))
    assert compacted[1]["role"] == "user"
    assert "validate_email" in compacted[1]["content"]


def test_compaction_keeps_the_tail_verbatim() -> None:
    """The model is mid-thought; its recent reasoning must survive intact."""
    from gigaforge.harness.compaction import compact_trajectory

    original = _trajectory(20)
    compacted = compact_trajectory(original, keep_tail=4)
    assert compacted[-4:] == original[-4:]


def test_compaction_is_idempotent() -> None:
    """Compacting an already-compact trajectory must not shrink it further."""
    from gigaforge.harness.compaction import compact_trajectory

    once = compact_trajectory(_trajectory(20))
    twice = compact_trajectory(once)
    assert twice == once


def test_compaction_never_grows_the_request() -> None:
    """A "compaction" that grows the request is worse than none."""
    from gigaforge.harness.compaction import compact_trajectory

    short = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    assert compact_trajectory(short) == short


def test_compaction_marks_what_it_dropped() -> None:
    """A run that silently rewrote its history is unauditable."""
    from gigaforge.harness.compaction import COMPACTION_MARKER, compact_trajectory

    compacted = compact_trajectory(_trajectory(20))
    assert any(COMPACTION_MARKER in m["content"] for m in compacted)


def test_compaction_fits_reports_whether_recovery_is_possible() -> None:
    from gigaforge.harness.compaction import compaction_fits

    assert compaction_fits(_trajectory(20), limit_tokens=10_000) is True
    assert compaction_fits(_trajectory(20), limit_tokens=1) is False


# ── error classification ────────────────────────────────────────────────────
def test_rate_limit_is_retryable_but_auth_is_not() -> None:
    """A 429 is transient; a 401 is a statement about the credentials."""
    from gigaforge.client.errors import AuthError, RateLimitError, RequestError
    from gigaforge.harness import is_retryable

    assert is_retryable(RateLimitError("429")) is True
    assert is_retryable(AuthError("401")) is False
    assert is_retryable(RequestError("400")) is False


def test_budget_and_overflow_are_not_retried() -> None:
    """Both are decisions, not transient failures."""
    from gigaforge.client.errors import BudgetExceeded
    from gigaforge.core.context_window_guard import ContextOverflowError
    from gigaforge.harness import is_retryable

    assert is_retryable(BudgetExceeded("cap")) is False
    assert is_retryable(ContextOverflowError("too long")) is False


def test_classify_error_maps_each_family() -> None:
    from gigaforge.client.errors import AuthError, BudgetExceeded, RateLimitError
    from gigaforge.core.context_window_guard import ContextOverflowError
    from gigaforge.harness import classify_error

    assert classify_error(ContextOverflowError("x")) == "context_overflow"
    assert classify_error(BudgetExceeded("x")) == "budget_exceeded"
    assert classify_error(RateLimitError("x")) == "rate_limited"
    assert classify_error(AuthError("x")) == "auth_error"
    assert classify_error(ValueError("x")) == "error"


# ── the harness itself ──────────────────────────────────────────────────────
class _State:
    task_id = "run-1"


class _Result:
    def __init__(self, finish_reason: str = "finish_action", llm_calls: int = 2) -> None:
        self.finish_reason = finish_reason
        self.llm_calls = llm_calls
        self.steps = [{"step_idx": 0, "action": "grep", "ok": True}]


class _Runner:
    """A runner that fails a scripted number of times, then succeeds."""

    def __init__(self, failures: list[BaseException] | None = None) -> None:
        self.failures = list(failures or [])
        self.calls = 0

    async def run(self, task: str, state: Any, *, ctx: Any = None, bootstrap_hint: Any = None) -> Any:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return _Result()


def _harness(**policy: Any) -> Any:
    from gigaforge.harness import Harness, HarnessPolicy

    waits: list[float] = []
    harness = Harness(policy=HarnessPolicy(sleep=waits.append, **policy))
    return harness, waits


def test_harness_returns_ok_on_a_clean_run() -> None:
    harness, _ = _harness()
    outcome = harness.run(_Runner(), "goal", _State(), run_id="run-1")
    assert outcome.ok
    assert outcome.finish_reason == "ok"
    assert outcome.ledger.llm_calls == 2
    assert outcome.ledger.tool_calls == 1


def test_harness_retries_a_rate_limit_and_succeeds() -> None:
    from gigaforge.client.errors import RateLimitError

    harness, waits = _harness()
    runner = _Runner([RateLimitError("429", retry_after=2.0)])
    outcome = harness.run(runner, "goal", _State(), run_id="run-1")

    assert outcome.ok
    assert runner.calls == 2
    assert waits == [2.0], "the server's Retry-After must win over the backoff"
    assert outcome.ledger.retries == 1


def test_harness_does_not_retry_a_bad_request() -> None:
    from gigaforge.client.errors import RequestError

    harness, waits = _harness()
    runner = _Runner([RequestError("400 bad request")])
    outcome = harness.run(runner, "goal", _State(), run_id="run-1")

    assert not outcome.ok
    assert outcome.finish_reason == "error"
    assert runner.calls == 1, "a 400 must not be retried"
    assert waits == []


def test_harness_clamps_a_hostile_retry_after() -> None:
    """A server-supplied wait must not stall the run indefinitely."""
    from gigaforge.client.errors import RateLimitError

    harness, waits = _harness(max_wait=5.0)
    runner = _Runner([RateLimitError("429", retry_after=3600.0)])
    harness.run(runner, "goal", _State(), run_id="run-1")
    assert waits == [5.0]


def test_harness_gives_up_after_max_attempts() -> None:
    from gigaforge.client.errors import ServerError

    harness, _ = _harness(max_attempts=3)
    runner = _Runner([ServerError("500")] * 5)
    outcome = harness.run(runner, "goal", _State(), run_id="run-1")

    assert not outcome.ok
    assert runner.calls == 3
    assert outcome.ledger.retries == 2


def test_harness_recovers_from_context_overflow() -> None:
    """Overflow is a recovery trigger, not a terminal state."""
    from gigaforge.core.context_window_guard import ContextOverflowError

    harness, _ = _harness()
    runner = _Runner([ContextOverflowError("too long")])
    outcome = harness.run(runner, "goal", _State(), run_id="run-1")

    assert outcome.ok, "the run must continue after a compaction"
    assert runner.calls == 2
    assert outcome.ledger.compactions == 1


def test_harness_stops_when_compaction_cannot_help() -> None:
    from gigaforge.core.context_window_guard import ContextOverflowError

    harness, _ = _harness(max_compactions=1)
    runner = _Runner([ContextOverflowError("too long")] * 5)
    outcome = harness.run(runner, "goal", _State(), run_id="run-1")

    assert not outcome.ok
    assert outcome.finish_reason == "context_overflow"
    assert runner.calls == 2, "one compaction, then give up"


def test_harness_records_a_budget_stop_as_a_decision() -> None:
    from gigaforge.client.errors import BudgetExceeded

    harness, _ = _harness()
    outcome = harness.run(_Runner([BudgetExceeded("cap")]), "goal", _State(), run_id="run-1")
    assert outcome.finish_reason == "budget_exceeded"
    assert outcome.ledger.finish_reason == "budget_exceeded"


# ── the ledger ──────────────────────────────────────────────────────────────
def test_ledger_round_trips_through_json(tmp_path: Any) -> None:
    harness, _ = _harness()
    outcome = harness.run(_Runner(), "goal", _State(), run_id="run-1")
    path = outcome.ledger.write(tmp_path / "ledger.json")

    payload = json.loads(path.read_text())
    assert payload["run_id"] == "run-1"
    assert payload["finish_reason"] == "finish_action"
    assert payload["llm_calls"] == 2
    assert isinstance(payload["steps"], list)


def test_ledger_summary_line_is_one_line() -> None:
    harness, _ = _harness()
    outcome = harness.run(_Runner(), "goal", _State(), run_id="run-1")
    line = outcome.ledger.summary_line()
    assert "\n" not in line
    assert "run-1" in line


def test_ledger_counts_failures() -> None:
    from gigaforge.harness.ledger import RunLedger

    ledger = RunLedger(run_id="r")
    ledger.tool(name="grep", ok=True)
    ledger.tool(name="run_tests", ok=False)
    assert ledger.failures == 1
    assert ledger.tool_calls == 2


@pytest.mark.parametrize("reason", ["ok", "budget_exceeded", "context_overflow"])
def test_harness_result_ok_flag_follows_the_reason(reason: str) -> None:
    from gigaforge.harness import HarnessResult

    assert HarnessResult(finish_reason=reason).ok is (reason == "ok")

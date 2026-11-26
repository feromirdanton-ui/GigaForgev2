"""Cost per task is a different number from cost per run.

**The gap this closes.**  ``cost_micros`` answers "what did this run cost".  The
question an operator actually asks is "what does a task cost", and the two differ
as soon as a run does more than one thing.  Measured live: a goal run cost
14 722 micro-USD over five model calls, and the report said nothing about
whether that was one task or five.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.contracts import RunContext  # noqa: E402
from gigaforge.core.pipeline import (  # noqa: E402
    PipelineResult,
    _cost_per_task,
    _tasks_completed,
)
from gigaforge.core.state import TaskState  # noqa: E402


class _Kind:
    def __init__(self, text_product: bool) -> None:
        self.is_text_product = text_product


def _pipeline(text_product: bool) -> Any:
    class _P:
        pass

    return _P()


def _ctx(cost: int = 0, **extras: Any) -> RunContext:
    ctx = RunContext(run_id="r", state=TaskState(run_id="r", goal="g"))
    ctx.state.cost_micros = cost
    ctx.extras.update(extras)
    return ctx


@pytest.fixture(autouse=True)
def _patch_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(pipeline: Any, ctx: RunContext) -> _Kind:
        return _Kind(bool(ctx.extras.get("_text")))

    monkeypatch.setattr("gigaforge.core.pipeline._task_kind_for", fake)


def test_a_single_run_is_at_least_one_task() -> None:
    """Dividing by zero is how a metric becomes a crash."""
    assert _tasks_completed(_pipeline(False), _ctx()) == 1


def test_a_plan_contributes_its_executed_steps() -> None:
    ctx = _ctx(plan_steps_executed=4)
    assert _tasks_completed(_pipeline(False), ctx) == 4


def test_a_text_product_is_one_task_however_many_phases() -> None:
    ctx = _ctx(_text=True, plan_steps_executed=5)
    assert _tasks_completed(_pipeline(True), ctx) == 1


def test_cost_per_task_divides_the_run_cost() -> None:
    ctx = _ctx(cost=12_000, plan_steps_executed=4)
    assert _cost_per_task(_pipeline(False), ctx) == 3_000


def test_cost_per_task_equals_run_cost_for_a_single_task() -> None:
    ctx = _ctx(cost=14_722)
    assert _cost_per_task(_pipeline(False), ctx) == 14_722


def test_cost_per_task_is_zero_before_any_spend() -> None:
    assert _cost_per_task(_pipeline(False), _ctx()) == 0


def test_the_report_carries_the_metric_and_its_usd_mirror() -> None:
    report = PipelineResult(run_id="r", tasks_completed=2, cost_per_task_micros=3_500)
    payload = report.to_dict()
    assert payload["tasks_completed"] == 2
    assert payload["cost_per_task_micros"] == 3_500
    assert payload["cost_per_task_usd"] == pytest.approx(0.0035)

"""Regression tests for V2: the ReAct layer must be reachable from the product.

The review found that ``run_react_graph`` and ``ReActRunner`` were only ever
constructed by tests — the DAG's ``plan`` node always called a one-shot prompt,
so the whole observe/act/observe engine (and the four guards wired into it)
could not run in a real pipeline.

The fix is :class:`gigaforge.core.strategies.ReActPlan`: a plan strategy that
builds a ``ReActRunner`` from the pipeline's own collaborators and runs it
through :func:`gigaforge.agents.engine.run_react`, which dispatches to the
classic loop or the LangGraph graph.

These tests pin the wiring, not the model: no GigaChat key, no network.
"""
from __future__ import annotations

from typing import Any


# ── the strategy is selectable ───────────────────────────────────────────────
def test_react_plan_is_registered() -> None:
    """``GIGAFORGE_PLAN_STRATEGY=react`` must resolve to a real strategy."""
    from gigaforge.core.strategies import PLAN_STRATEGIES, ReActPlan

    assert PLAN_STRATEGIES["react"] is ReActPlan


def test_build_plan_strategy_returns_react_plan(monkeypatch: Any) -> None:
    """The factory honours the name and threads the runner factory through."""
    from gigaforge.core.strategies import ReActPlan, build_plan_strategy

    sentinel = lambda goal="": object()  # noqa: E731
    strategy = build_plan_strategy("react", react_runner_factory=sentinel)
    assert isinstance(strategy, ReActPlan)
    assert strategy._runner_factory is sentinel


def test_build_plan_strategy_reads_the_env(monkeypatch: Any) -> None:
    """The env var is the deployment switch, so it must win over the default."""
    from gigaforge.core.strategies import ReActPlan, build_plan_strategy

    monkeypatch.setenv("GIGAFORGE_PLAN_STRATEGY", "react")
    assert isinstance(build_plan_strategy(), ReActPlan)


# ── the strategy actually drives the engine ──────────────────────────────────
def test_react_plan_runs_through_the_engine_dispatch(monkeypatch: Any) -> None:
    """``ReActPlan.plan`` must call ``run_react`` — the product seam.

    A stub runner stands in for the real one; the assertion is that the strategy
    goes through the engine dispatcher (which is what makes both the classic
    loop and the LangGraph graph reachable) rather than calling a prompt.
    """
    from gigaforge.agents import engine
    from gigaforge.core.strategies import ReActPlan

    calls: list[tuple[Any, str]] = []

    class _Step:
        description = "edit the function"

    class _Result:
        plan = [_Step()]
        finish_reason = "finish_action"
        llm_calls = 2

    class _Runner:
        state = object()
        ctx = object()

    def _fake_run_react(runner: Any, task: str, state: Any, *, ctx: Any, **kw: Any) -> Any:
        calls.append((runner, task))
        return _Result()

    monkeypatch.setattr(engine, "run_react", _fake_run_react)

    runner = _Runner()
    strategy = ReActPlan(runner_factory=lambda goal="": runner)
    outcome = strategy.plan(
        "fix the bug",
        "",
        ask=lambda *a, **k: "[]",
        parse=lambda text: [],
    )

    assert calls and calls[0][1] == "fix the bug"
    assert outcome.strategy == "react"
    assert outcome.steps == ["edit the function"]


def test_react_plan_degrades_without_a_runner() -> None:
    """No runner means the one-shot plan, not a failed run."""
    from gigaforge.core.strategies import ReActPlan

    strategy = ReActPlan(runner_factory=None)
    outcome = strategy.plan(
        "goal",
        "slice",
        ask=lambda *a, **k: "- step one",
        parse=lambda text: [line.strip("- ") for line in text.splitlines() if line.strip()],
    )
    assert outcome.strategy == "react->linear"
    assert outcome.steps == ["step one"]


def test_react_plan_degrades_when_the_engine_raises(monkeypatch: Any) -> None:
    """An exploration failure must not take the run down."""
    from gigaforge.agents import engine
    from gigaforge.core.strategies import ReActPlan

    class _Runner:
        state = object()
        ctx = object()

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("exploration exploded")

    monkeypatch.setattr(engine, "run_react", _boom)
    strategy = ReActPlan(runner_factory=lambda goal="": _Runner())
    outcome = strategy.plan(
        "goal",
        "",
        ask=lambda *a, **k: "- fallback step",
        parse=lambda text: [line.strip("- ") for line in text.splitlines() if line.strip()],
    )
    assert outcome.strategy == "react->linear"
    assert outcome.steps == ["fallback step"]


# ── the pipeline wires the factory ───────────────────────────────────────────
def test_pipeline_exposes_the_react_runner_factory() -> None:
    """The pipeline must own the seam that builds a runner from its collaborators."""
    import inspect

    from gigaforge.core.pipeline import TaskPipeline

    assert hasattr(TaskPipeline, "_build_react_runner")
    source = inspect.getsource(TaskPipeline.__init__)
    assert "react_runner_factory" in source

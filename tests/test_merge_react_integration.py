"""Integration tests: the ReAct layer must *execute*, not merely be wired.

The previous review's central finding was that three green commits were all
inert: each added a capability that the default configuration never ran, and
the tests only asserted that a parameter travelled, never that a feature
executed.  ``test_merge_react_reachable.py`` used a stub runner and therefore
never touched ``_build_react_runner`` — so it could not see the ``ImportError``
on ``DEFAULT_ALLOWLIST``, the ``NameError`` on the missing ``log``, or the
``AttributeError`` on ``SyncChatClient.chat``.

These tests build a **real** ``TaskPipeline`` with a fake provider and drive the
real strategy → engine → runner → client path.  They are the regression net for
exactly those three defects, plus the accounting bypass (a runner with its own
``BudgetTracker``/``Tracer``/``run_id``).

Offline and deterministic: no GigaChat key, no network.
"""
from __future__ import annotations

import json
from typing import Any

import pytest


# ── fixtures ────────────────────────────────────────────────────────────────
def _pipeline(tmp_path: Any) -> Any:
    """A real TaskPipeline over a fake provider, in a throwaway repo root."""
    from gigaforge.core.config import CoreConfig
    from gigaforge.core.pipeline import TaskPipeline
    from gigaforge.core.sync_adapter import SyncChatClient

    class _Provider:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat(self, messages: Any, *, model: str, **kwargs: Any) -> Any:
            from gigaforge.client.giga_client import ChatResult

            self.calls.append({"model": model, "messages": list(messages), **kwargs})
            return ChatResult(
                content=json.dumps(
                    {
                        "thought": "the plan is ready",
                        "action": "finish",
                        "action_input": {
                            "steps": [
                                {"tool": "edit_function", "args": {"name": "validate_email"}},
                                {"tool": "run_tests", "args": {}},
                            ]
                        },
                    }
                ),
                usage={"prompt_tokens": 11, "completion_tokens": 7},
            )

        async def list_models(self) -> list[dict[str, Any]]:
            return []

        def capabilities(self) -> Any:
            return None

    provider = _Provider()
    config = CoreConfig(
        db_path=str(tmp_path / "core.sqlite3"),
        trace_path=str(tmp_path / "traces" / "core.jsonl"),
        repo_root=str(tmp_path),
    )
    pipeline = TaskPipeline(config=config, client=SyncChatClient(provider))
    return pipeline, provider


# ── P0 #1: the runner builds at all ─────────────────────────────────────────
def test_build_react_runner_returns_a_runner(tmp_path: Any) -> None:
    """``_build_react_runner`` must not raise.

    This is the test the review asked for: it imports the real module graph, so
    a bad symbol (``DEFAULT_ALLOWLIST``) or a missing logger (``log``) fails
    here instead of at runtime inside a swallowed ``except``.
    """
    pipeline, _ = _pipeline(tmp_path)
    runner = pipeline._build_react_runner("fix the bug", run_id="run-1", max_steps=3)
    assert runner is not None
    assert type(runner).__name__ == "ReActRunner"


def test_build_react_runner_returns_none_without_a_client(tmp_path: Any) -> None:
    """No client is a legitimate "cannot explore", not an error."""
    from gigaforge.core.config import CoreConfig
    from gigaforge.core.pipeline import TaskPipeline

    config = CoreConfig(
        db_path=str(tmp_path / "c.sqlite3"),
        trace_path=str(tmp_path / "t.jsonl"),
        repo_root=str(tmp_path),
    )
    assert TaskPipeline(config=config).client is None
    assert TaskPipeline(config=config)._build_react_runner("goal") is None


# ── P0 #2: the client has the method the agents call ────────────────────────
def test_sync_client_exposes_an_async_chat() -> None:
    """``ReActRunner`` does ``await client.chat(...)``; the adapter must answer.

    A blocking ``chat`` would deadlock the adapter loop it is called from, so
    the method is a coroutine function — assert that, not just its presence.
    """
    import inspect

    from gigaforge.core.sync_adapter import SyncChatClient

    assert hasattr(SyncChatClient, "chat")
    assert inspect.iscoroutinefunction(SyncChatClient.chat)


def test_sync_client_chat_returns_a_chat_result(tmp_path: Any) -> None:
    """The shape the agents read: ``.content``, ``.usage``, ``.estimated``."""
    import asyncio

    from gigaforge.core.sync_adapter import SyncChatClient

    class _Provider:
        async def chat(self, messages: Any, *, model: str, **kwargs: Any) -> Any:
            from gigaforge.client.giga_client import ChatResult

            return ChatResult(content="hello", usage={"prompt_tokens": 1})

    client = SyncChatClient(_Provider())
    result = asyncio.run(client.chat("GigaChat", [{"role": "user", "content": "hi"}]))
    assert result.content == "hello"
    assert result.usage == {"prompt_tokens": 1}
    assert result.estimated is False


# ── P0 #3: the runner shares the run's accounting ───────────────────────────
def test_runner_shares_the_router_budget_and_the_pipeline_tracer(tmp_path: Any) -> None:
    """A fresh ``BudgetTracker()`` would spend tokens the budget never sees."""
    pipeline, _ = _pipeline(tmp_path)
    runner = pipeline._build_react_runner("goal", run_id="run-42")
    assert runner._budgets is pipeline.router.budget
    assert runner._tracer is pipeline.tracer


def test_runner_carries_the_callers_run_id(tmp_path: Any) -> None:
    """A synthetic ``run_id`` would split one run's trace across two files."""
    pipeline, _ = _pipeline(tmp_path)
    runner = pipeline._build_react_runner("goal", run_id="run-42")
    assert runner.state.task_id == "run-42"


def test_runner_honours_the_step_cap(tmp_path: Any) -> None:
    """``GIGAFORGE_REACT_MAX_STEPS`` was read twice and the strategy's value lost."""
    pipeline, _ = _pipeline(tmp_path)
    runner = pipeline._build_react_runner("goal", max_steps=2)
    assert runner._max_steps == 2


# ── the whole path: strategy → engine → runner → client ─────────────────────
def test_react_plan_executes_end_to_end(tmp_path: Any) -> None:
    """The one test that would have caught all three defects at once.

    It builds the runner from a real pipeline, runs the real engine, and
    asserts that a model call actually happened and produced a plan.
    """
    from gigaforge.core.strategies import ReActPlan

    pipeline, provider = _pipeline(tmp_path)
    strategy = ReActPlan(runner_factory=pipeline._build_react_runner, max_steps=3)
    strategy.bind_run("run-e2e")

    outcome = strategy.plan(
        "add email validation",
        "",
        ask=lambda *a, **k: "- fallback step",
        parse=lambda text: [text],
    )

    assert outcome.strategy == "react", "the ReAct path must win, not the fallback"
    assert outcome.steps == ["edit_function: validate_email", "run_tests"]
    assert provider.calls, "the model was never called"
    assert provider.calls[0]["phase"] == "plan"


def test_react_plan_records_llm_calls_in_the_candidate(tmp_path: Any) -> None:
    """The DAG's plan metadata must show what exploration cost."""
    from gigaforge.core.strategies import ReActPlan

    pipeline, _ = _pipeline(tmp_path)
    strategy = ReActPlan(runner_factory=pipeline._build_react_runner, max_steps=3)
    strategy.bind_run("run-cost")
    outcome = strategy.plan("goal", "", ask=lambda *a, **k: "- x", parse=lambda t: [t])

    assert outcome.candidates
    assert outcome.candidates[0]["llm_calls"] >= 1
    assert outcome.candidates[0]["finish_reason"] == "finish_action"


def test_react_plan_writes_into_the_pipeline_trace(tmp_path: Any) -> None:
    """Exploration tokens must land in the run's own trace file."""
    from gigaforge.core.strategies import ReActPlan

    pipeline, _ = _pipeline(tmp_path)
    strategy = ReActPlan(runner_factory=pipeline._build_react_runner, max_steps=3)
    strategy.bind_run("run-trace")
    strategy.plan("goal", "", ask=lambda *a, **k: "- x", parse=lambda t: [t])

    trace_file = tmp_path / "traces" / "run-trace.jsonl"
    assert trace_file.exists(), "the ReAct trace did not reach the pipeline's tracer"
    lines = [json.loads(line) for line in trace_file.read_text().splitlines() if line.strip()]
    assert any(entry.get("kind") == "llm" for entry in lines)


# ── the re-entrancy guard ───────────────────────────────────────────────────
def test_nested_await_blocking_does_not_deadlock() -> None:
    """A coroutine on the adapter loop must not block on that same loop.

    ``run_react`` submits the runner to the adapter loop; the runner's first
    ``client.chat`` used to submit again from inside it — a guaranteed
    deadlock.  The guard runs the nested coroutine on a private loop.
    """
    import asyncio

    from gigaforge.core.sync_adapter import await_blocking

    async def _outer() -> str:
        # Called from inside the adapter loop: this is the nested case.
        return await_blocking(_inner())

    async def _inner() -> str:
        await asyncio.sleep(0)
        return "nested-ok"

    assert await_blocking(_outer(), timeout=10) == "nested-ok"


def test_await_blocking_still_works_from_a_plain_thread() -> None:
    """The normal path is unchanged."""
    import asyncio

    from gigaforge.core.sync_adapter import await_blocking

    async def _work() -> int:
        await asyncio.sleep(0)
        return 7

    assert await_blocking(_work(), timeout=10) == 7


@pytest.mark.parametrize("run_id", ["", "run-x"])
def test_runner_falls_back_to_a_stable_id(tmp_path: Any, run_id: str) -> None:
    """An empty run id must still produce a usable ``task_id``."""
    pipeline, _ = _pipeline(tmp_path)
    runner = pipeline._build_react_runner("goal", run_id=run_id)
    assert runner.state.task_id == (run_id or "react-plan")

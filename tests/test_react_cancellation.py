from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gigaforge.agents.react import ReActRunner
from gigaforge.core.cancellation import CancellationToken
from gigaforge.core.errors import RunCancelledError
from gigaforge.core.strategies import ReActPlan


class _Claim:
    cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Budgets:
    def __init__(self, claim: _Claim) -> None:
        self.claim = claim

    def task_remaining_for(self, task_id: str) -> int:
        return 100_000

    def check(self, *args, **kwargs) -> None:
        return None

    def commit_task(self, *args, **kwargs) -> None:
        return None

    def check_overshoot(self, *args, **kwargs) -> None:
        return None

    def reserve(self, *args, **kwargs) -> _Claim:
        return self.claim


class _Router:
    def route(self, phase: str) -> str:
        return "m"


class _Tracer:
    def trace(self, *args, **kwargs) -> None:
        return None


class _State:
    task_id = "cancel-test"
    goal = "wait for cancellation"

    def serialize_prefix(self) -> dict:
        return {}


def test_node_cancellation_stops_llm_request_and_releases_reservation(
    tmp_path: Path,
) -> None:
    class _Client:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def chat(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def exercise() -> None:
        cancellation = CancellationToken()
        client = _Client()
        claim = _Claim()
        runner = ReActRunner(
            client=client,
            router=_Router(),
            budgets=_Budgets(claim),
            tracer=_Tracer(),
            max_steps=2,
        )
        ctx = SimpleNamespace(
            repo_root=tmp_path,
            worktree=tmp_path,
            index=None,
            sandbox=None,
            cancellation=cancellation,
        )

        run = asyncio.create_task(runner.run("wait for cancellation", _State(), ctx=ctx))
        await asyncio.wait_for(client.started.wait(), timeout=1)
        cancellation.cancel("node timed out")

        with pytest.raises(RunCancelledError):
            await asyncio.wait_for(run, timeout=1)

        assert client.cancelled
        assert claim.cancelled

    asyncio.run(exercise())


def test_react_plan_attaches_pipeline_cancellation_to_runner(monkeypatch) -> None:
    runner = SimpleNamespace(ctx=SimpleNamespace(), state=SimpleNamespace(), budgets=None)

    class _Harness:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *args, **kwargs):
            return SimpleNamespace(
                ok=False,
                finish_reason="cancelled",
                error="node timed out",
                ledger=None,
            )

    monkeypatch.setattr("gigaforge.harness.Harness", _Harness)
    cancellation = CancellationToken()
    strategy = ReActPlan(runner_factory=lambda goal, **kwargs: runner)

    with pytest.raises(RuntimeError, match="LangGraph ReAct planning failed"):
        strategy.plan_under_harness(
            "goal",
            "",
            ask=lambda *args, **kwargs: "",
            parse=lambda text: [],
            cancellation=cancellation,
        )

    assert runner.ctx.cancellation is cancellation

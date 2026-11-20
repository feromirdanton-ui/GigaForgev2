"""Regression tests for the level-2 agent layer (LangGraph + deepagents).

The merge plan splits GigaForge in two levels and this suite guards the seam:

* the **deterministic core** (``dag.py`` contracts, ``l0/``, ``firewall/``,
  ``router.py``/``budgets.py``/``breaker.py``, ``bench/``) must stay untouched
  and keep working — those modules are imported here only to prove they still
  load and behave;
* the **agent runtime** (goal mode, ReAct, sub-agents) must run on LangGraph
  with checkpointing, ``interrupt()`` approval and event streaming.

Everything runs offline: the pipeline, verifier and model client are fakes, so
the suite is deterministic and free.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from gigaforge.core.langgraph_runtime import (
    LANGGRAPH_AVAILABLE,
    graph_state_summary,
    iter_graph_events,
    open_checkpointer,
    thread_config,
)

# The level-2 agent layer is part of the product, not an optional extra: the
# goal engine defaults to LangGraph.  Skipping this suite silently when the
# package is missing is how the whole layer stayed unverified, so the skip is
# now opt-in — set ``GIGAFORGE_ALLOW_MISSING_LANGGRAPH=1`` to run a bare
# install, and CI (which installs the ``dev`` extra) fails loudly instead.
_ALLOW_MISSING = os.environ.get("GIGAFORGE_ALLOW_MISSING_LANGGRAPH") == "1"

pytestmark = pytest.mark.skipif(
    not LANGGRAPH_AVAILABLE and _ALLOW_MISSING,
    reason="langgraph is not installed (allowed by GIGAFORGE_ALLOW_MISSING_LANGGRAPH)",
)


def test_langgraph_is_installed() -> None:
    """A bare install must not silently drop the agent layer's coverage."""
    assert LANGGRAPH_AVAILABLE, (
        "langgraph is required for the level-2 agent layer; install the 'dev' "
        "extra (pip install -e '.[dev]') or set "
        "GIGAFORGE_ALLOW_MISSING_LANGGRAPH=1 to skip this suite deliberately"
    )


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeResult:
    """The subset of ``PipelineResult`` the goal graph reads."""

    def __init__(self, *, accepted: bool, changed: list[str] | None = None,
                 diff: str = "", error: str = "", tokens: int = 7, calls: int = 1) -> None:
        self.accepted = accepted
        self.status = "succeeded" if accepted else "rejected"
        self.changed_files = changed or []
        self.final_diff = diff
        self.summary = "ok" if accepted else "no change"
        self.error = error
        self.tokens = tokens
        self.calls = calls
        self.duration_ms = 1.0
        self.nodes: list[dict[str, Any]] = []


class FakePipeline:
    """Accepts on the Nth call, so a retry path can be exercised."""

    def __init__(self, *, accept_on: int = 1, changed: list[str] | None = None) -> None:
        self.accept_on = accept_on
        self.calls = 0
        self.changed = changed or ["calc.py"]
        self.prompts: list[str] = []

    def run(self, prompt: str, **kwargs: Any) -> FakeResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls >= self.accept_on:
            return FakeResult(accepted=True, changed=self.changed, diff="--- a\n+++ b\n")
        return FakeResult(accepted=False, error="validator rejected the diff")


class FakeCheck:
    def __init__(self, name: str, status: str, summary: str = "") -> None:
        self.name = name
        self.status = status
        self.summary = summary
        self.required = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "summary": self.summary,
                "required": self.required}


class FakeReport:
    def __init__(self, passed: bool | None) -> None:
        self.passed = passed
        self.checks = [FakeCheck("pytest", "passed" if passed else "failed", "1 failed")]

    def feedback(self, *, limit: int = 3500) -> str:
        return "" if self.passed else "- pytest: failed — 1 failed"

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": [c.to_dict() for c in self.checks]}


class FakeVerifier:
    def __init__(self, results: list[bool | None] | None = None) -> None:
        self.results = list(results or [True])
        self.calls = 0

    def verify(self, root: Path, changed: list[str], goal: str) -> FakeReport:
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return FakeReport(self.results[index])


class FakeWorkspace:
    def __init__(self) -> None:
        self.root = Path("/tmp/ws")
        self.published: list[list[str]] = []

    def publish(self, changed: list[str]) -> list[str]:
        self.published.append(list(changed))
        return list(changed)


# ── plumbing ─────────────────────────────────────────────────────────────────
def test_checkpointer_is_durable_and_reopenable(tmp_path: Path) -> None:
    path = tmp_path / "graph.sqlite3"
    saver, conn = open_checkpointer(path)
    assert conn is not None
    conn.close()
    assert path.exists()
    # A second open must see the same file (that is what makes resume work).
    saver2, conn2 = open_checkpointer(path)
    assert conn2 is not None
    conn2.close()


def test_in_memory_checkpointer_has_no_connection() -> None:
    saver, conn = open_checkpointer(None)
    assert conn is None
    assert saver is not None


def test_thread_config_carries_thread_id() -> None:
    cfg = thread_config("goal-1", checkpoint_ns="x")
    assert cfg["configurable"]["thread_id"] == "goal-1"
    assert cfg["configurable"]["checkpoint_ns"] == "x"


# ── goal graph ───────────────────────────────────────────────────────────────
def _run_goal(tmp_path: Path, **kwargs: Any):
    from gigaforge.core.langgraph_goal import run_goal_graph

    pipeline = kwargs.pop("pipeline", FakePipeline())
    verifier = kwargs.pop("verifier", FakeVerifier())
    workspace = kwargs.pop("workspace", FakeWorkspace())
    return run_goal_graph(
        pipeline,
        repo_root=tmp_path,
        goal="make add() work",
        verifier=verifier,
        workspace=workspace,
        checkpoint_path=tmp_path / "cp.sqlite3",
        **kwargs,
    )


def test_goal_graph_completes_and_publishes(tmp_path: Path) -> None:
    workspace = FakeWorkspace()
    result = _run_goal(tmp_path, workspace=workspace)
    assert result.status == "completed"
    assert result.accepted is True
    assert result.published_files == ["calc.py"]
    assert workspace.published == [["calc.py"]]
    assert len(result.cycles) == 1


def test_goal_graph_retries_until_verification_passes(tmp_path: Path) -> None:
    pipeline = FakePipeline(accept_on=1)
    verifier = FakeVerifier([False, True])
    result = _run_goal(tmp_path, pipeline=pipeline, verifier=verifier)
    assert result.status == "completed"
    assert len(result.cycles) == 2
    # The second cycle's prompt must carry the first cycle's failure forward.
    assert "Previous cycle feedback" in pipeline.prompts[1]


def test_goal_graph_stops_at_cycle_limit(tmp_path: Path) -> None:
    pipeline = FakePipeline(accept_on=99)
    result = _run_goal(tmp_path, pipeline=pipeline, max_cycles=3)
    assert pipeline.calls == 3
    assert result.status == "needs_attention"
    assert result.accepted is False


def test_goal_graph_blocks_when_verifier_unavailable(tmp_path: Path) -> None:
    result = _run_goal(tmp_path, verifier=FakeVerifier([None]))
    assert result.status == "blocked"
    assert result.accepted is False


def test_goal_graph_records_engine_and_persists(tmp_path: Path) -> None:
    result = _run_goal(tmp_path)
    record = json.loads((tmp_path / ".gigaforge" / "goals" / f"{result.goal_id}.json").read_text())
    assert record["engine"] == "langgraph"
    assert record["status"] == "completed"
    assert record["schema_version"] == 1


def test_goal_graph_emits_stream_events(tmp_path: Path) -> None:
    seen: list[str] = []
    _run_goal(tmp_path, emit=lambda event, payload: seen.append(event))
    assert "goal.cycle_start" in seen
    assert "goal.verify_start" in seen
    assert "goal.publish" in seen


def test_goal_graph_interrupt_approval_apply(tmp_path: Path) -> None:
    asked: list[str] = []

    def approve(diff: str) -> bool:
        asked.append(diff)
        return True

    result = _run_goal(tmp_path, approve=approve)
    assert result.status == "completed"
    assert len(asked) == 1
    assert "calc.py" in asked[0]


def test_goal_graph_interrupt_approval_reject_does_not_publish(tmp_path: Path) -> None:
    workspace = FakeWorkspace()
    result = _run_goal(tmp_path, workspace=workspace, approve=lambda _d: False, max_cycles=2)
    assert workspace.published == []
    assert result.status == "needs_attention"
    assert "declined" in result.error


def test_goal_graph_without_gate_never_interrupts(tmp_path: Path) -> None:
    result = _run_goal(tmp_path, approve=None)
    assert result.status == "completed"


def test_goal_graph_resume_returns_completed_record(tmp_path: Path) -> None:
    from gigaforge.core.langgraph_goal import run_goal_graph

    first = _run_goal(tmp_path)
    again = run_goal_graph(
        FakePipeline(),
        repo_root=tmp_path,
        goal_id=first.goal_id,
        resume=True,
        checkpoint_path=tmp_path / "cp.sqlite3",
    )
    assert again.status == "completed"
    assert again.goal_id == first.goal_id


def test_goal_graph_rejects_empty_goal(tmp_path: Path) -> None:
    from gigaforge.core.langgraph_goal import run_goal_graph

    with pytest.raises(ValueError):
        run_goal_graph(FakePipeline(), repo_root=tmp_path, goal="  ")


def test_goal_graph_rejects_bad_cycle_limit(tmp_path: Path) -> None:
    from gigaforge.core.langgraph_goal import run_goal_graph

    with pytest.raises(ValueError):
        run_goal_graph(FakePipeline(), repo_root=tmp_path, goal="x", max_cycles=0)


def test_goal_graph_handles_pipeline_exception(tmp_path: Path) -> None:
    class Boom(FakePipeline):
        def run(self, prompt: str, **kwargs: Any) -> FakeResult:
            raise RuntimeError("provider exploded")

    result = _run_goal(tmp_path, pipeline=Boom())
    assert result.status == "failed"
    assert "provider exploded" in result.error


def test_goal_graph_state_is_json_serialisable(tmp_path: Path) -> None:
    """A SQLite checkpointer can only store JSON-safe values."""
    from gigaforge.core.langgraph_goal import build_goal_graph

    graph = build_goal_graph(
        FakePipeline(), repo_root=tmp_path, verifier=FakeVerifier(), workspace=FakeWorkspace()
    )
    config = thread_config("json-check")
    for _ in graph.stream({"goal": "x", "goal_id": "json-check", "max_cycles": 1,
                           "cycles": [], "status": "running"}, config, stream_mode="updates"):
        pass
    values = graph.get_state(config).values
    json.dumps(values, default=str)  # must not raise


# ── ReAct graph ──────────────────────────────────────────────────────────────
class FakeChatResult:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5}
        self.estimated = False
        self.finish_reason = "stop"


class FakeClient:
    """Replays a scripted list of ReAct JSON answers."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def chat(self, model: Any, messages: Any, **kwargs: Any) -> FakeChatResult:
        index = min(self.calls, len(self.answers) - 1)
        self.calls += 1
        return FakeChatResult(self.answers[index])


class FakeRouter:
    def route(self, phase: str) -> str:
        return "GigaChat"

    last_route = None


class FakeBudgets:
    def check(self, *a: Any, **k: Any) -> None:
        return None

    def commit_task(self, *a: Any, **k: Any) -> None:
        return None

    def check_overshoot(self, *a: Any, **k: Any) -> None:
        return None

    def reserve(self, *a: Any, **k: Any) -> Any:
        from tests.budget_stubs import NullClaim

        return NullClaim()


class FakeTracer:
    def trace(self, *a: Any, **k: Any) -> None:
        return None


class FakeSettings:
    budget_plan_out = 600


class FakeTool:
    def __init__(self, name: str, output: str) -> None:
        self.name = name
        self.output = output
        self.calls = 0

    async def run(self, action_input: str, *, ctx: Any) -> str:
        self.calls += 1
        return self.output


def _react_runner(answers: list[str], tools: list[FakeTool]):
    from gigaforge.agents.react import ReActRunner, ToolRegistry

    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ReActRunner(
        FakeClient(answers),
        FakeRouter(),
        FakeBudgets(),
        FakeTracer(),
        registry=registry,
        max_steps=4,
        settings=FakeSettings(),
    )


def _react_ctx(tmp_path: Path):
    from gigaforge.agents.react import ReActContext

    return ReActContext(repo_root=tmp_path, worktree=tmp_path, index=None, sandbox=None)


def _task_state():
    from gigaforge.core.task_state import TaskState

    return TaskState(run_id="r1", task_id="t1", goal="find add")


def test_react_graph_finishes_with_plan(tmp_path: Path) -> None:
    from gigaforge.agents.langgraph_react import run_react_graph

    finish = json.dumps({
        "thought": "done",
        "action": "finish",
        "action_input": json.dumps([{"tool": "edit_function", "args": {"name": "add"}}]),
    })
    runner = _react_runner([finish], [])
    result = run_react_graph(
        runner, "find add", _task_state(), ctx=_react_ctx(tmp_path), thread_id="t-react"
    )
    assert result.finished is True
    assert result.finish_reason == "finish_action"
    assert result.plan and result.plan[0].tool == "edit_function"


def test_react_graph_runs_a_tool_then_finishes(tmp_path: Path) -> None:
    from gigaforge.agents.langgraph_react import run_react_graph

    grep = FakeTool("grep", "calc.py:3:def add")
    step = json.dumps({"thought": "look", "action": "grep", "action_input": "def add"})
    finish = json.dumps({"thought": "done", "action": "finish", "action_input": "[]"})
    runner = _react_runner([step, finish], [grep])
    result = run_react_graph(
        runner, "find add", _task_state(), ctx=_react_ctx(tmp_path), thread_id="t-react-2"
    )
    assert grep.calls == 1
    assert result.finished is True
    assert result.llm_calls == 2


def test_react_graph_stops_at_max_steps(tmp_path: Path) -> None:
    from gigaforge.agents.langgraph_react import run_react_graph

    grep = FakeTool("grep", "nothing")
    step = json.dumps({"thought": "again", "action": "grep", "action_input": "x"})
    runner = _react_runner([step], [grep])
    result = run_react_graph(
        runner, "loop", _task_state(), ctx=_react_ctx(tmp_path), thread_id="t-react-3"
    )
    assert result.finished is False
    assert result.finish_reason == "max_steps"
    assert grep.calls == 3


def test_react_graph_repairs_unparseable_answer(tmp_path: Path) -> None:
    from gigaforge.agents.langgraph_react import run_react_graph

    finish = json.dumps({"thought": "ok", "action": "finish", "action_input": "[]"})
    runner = _react_runner(["not json at all", finish], [])
    result = run_react_graph(
        runner, "x", _task_state(), ctx=_react_ctx(tmp_path), thread_id="t-react-4"
    )
    assert result.finished is True
    assert any(step.get("error") == "parse_failed" for step in result.steps)


def test_react_graph_unknown_tool_is_an_observation(tmp_path: Path) -> None:
    from gigaforge.agents.langgraph_react import run_react_graph

    bad = json.dumps({"thought": "?", "action": "nope", "action_input": ""})
    finish = json.dumps({"thought": "ok", "action": "finish", "action_input": "[]"})
    runner = _react_runner([bad, finish], [])
    result = run_react_graph(
        runner, "x", _task_state(), ctx=_react_ctx(tmp_path), thread_id="t-react-5"
    )
    assert result.finished is True


def test_react_graph_loop_breaker_nudges_are_human_messages(tmp_path: Path) -> None:
    """The breaker must inject a HumanMessage, never a system message."""
    import asyncio

    from gigaforge.agents.langgraph_react import build_react_graph
    from gigaforge.core.loop_breaker import LoopBreaker

    grep = FakeTool("grep", "nothing")
    step = json.dumps({"thought": "again", "action": "grep", "action_input": "x"})
    runner = _react_runner([step], [grep])
    graph = build_react_graph(
        runner, ctx=_react_ctx(tmp_path), state=_task_state(), breaker=LoopBreaker()
    )
    config = thread_config("t-react-nudge")

    async def _drive() -> None:
        async for _chunk in graph.astream(
            {"task": "x", "step_idx": 0, "max_steps": 4, "trajectory": [], "steps": [],
             "finished": False, "plan": [], "localization": {}, "llm_calls": 0,
             "tokens_in": 0, "tokens_out": 0, "nudges": []},
            config,
            stream_mode="updates",
        ):
            pass

    asyncio.run(_drive())
    trajectory = graph.get_state(config).values.get("trajectory") or []
    nudges = [m for m in trajectory if "[NUDGE]" in str(m.get("content") or "")]
    assert nudges, "the breaker should have injected a nudge"
    assert all(m["role"] == "user" for m in nudges)


# ── sub-agent graph ──────────────────────────────────────────────────────────
def test_subagent_graph_preserves_spawn_order(tmp_path: Path) -> None:
    import asyncio

    from gigaforge.agents.langgraph_subagents import run_subagents_graph
    from gigaforge.agents.subagents import SubAgentRunner, SubAgentSpawn

    async def factory(payload: dict[str, Any], worktree: Any, budget: int) -> dict[str, Any]:
        # Later spawns finish first, so completion order != spawn order.
        await asyncio.sleep(0.01 * (3 - int(payload["i"])))
        return {"i": payload["i"]}

    spawns = [
        SubAgentSpawn(payload={"i": i}, budget_tokens=100, worktree=tmp_path)
        for i in range(3)
    ]
    results = run_subagents_graph(
        SubAgentRunner(), spawns, factory, thread_id="t-sub"
    )
    assert [r["i"] for r in results] == [0, 1, 2]
    assert [r["spawn_index"] for r in results] == [0, 1, 2]


def test_subagent_graph_empty_spawns() -> None:
    from gigaforge.agents.langgraph_subagents import run_subagents_graph
    from gigaforge.agents.subagents import SubAgentRunner

    assert run_subagents_graph(SubAgentRunner(), [], lambda *a: {}) == []


def test_subagent_graph_isolates_a_crashing_child(tmp_path: Path) -> None:
    from gigaforge.agents.langgraph_subagents import run_subagents_graph
    from gigaforge.agents.subagents import SubAgentRunner, SubAgentSpawn

    async def factory(payload: dict[str, Any], worktree: Any, budget: int) -> dict[str, Any]:
        if payload.get("boom"):
            raise RuntimeError("child died")
        return {"ok": True}

    spawns = [
        SubAgentSpawn(payload={"boom": True}, budget_tokens=10, worktree=tmp_path),
        SubAgentSpawn(payload={}, budget_tokens=10, worktree=tmp_path),
    ]
    results = run_subagents_graph(SubAgentRunner(), spawns, factory, thread_id="t-sub-crash")
    assert results[0]["killed"] is True
    assert results[1]["ok"] is True


# ── level-1 core must be untouched ───────────────────────────────────────────
def test_deterministic_core_still_loads() -> None:
    """The differentiators must not have been dragged onto LangGraph."""
    from gigaforge.bench import harness  # noqa: F401
    from gigaforge.core import breaker, budgets, contracts, dag, router  # noqa: F401
    from gigaforge.firewall import diff, slicer, validator  # noqa: F401
    from gigaforge.l0 import index, store  # noqa: F401

    assert dag.DagExecutor is not None
    assert contracts.NodeSpec is not None


def test_dag_contracts_still_enforced() -> None:
    from gigaforge.core.contracts import NodeSpec
    from gigaforge.core.dag import DagExecutor
    from gigaforge.core.errors import UnknownDependencyError

    # The executor validates in its constructor, so a malformed graph fails
    # before any node runs — that behaviour must survive the merge.
    with pytest.raises(UnknownDependencyError):
        DagExecutor([
            NodeSpec(name="a", fn=lambda ctx, inputs: {"x": 1}, provides=("x",)),
            NodeSpec(name="b", fn=lambda ctx, inputs: {"y": 2}, requires=("missing",),
                     provides=("y",)),
        ])


def test_goal_engine_switch_defaults_to_langgraph(monkeypatch: pytest.MonkeyPatch) -> None:
    from gigaforge.runtime import _langgraph_goal_enabled

    monkeypatch.delenv("GIGAFORGE_GOAL_ENGINE", raising=False)
    assert _langgraph_goal_enabled() is True
    monkeypatch.setenv("GIGAFORGE_GOAL_ENGINE", "classic")
    assert _langgraph_goal_enabled() is False
    monkeypatch.setenv("GIGAFORGE_GOAL_ENGINE", "langgraph")
    assert _langgraph_goal_enabled() is True


def test_chat_uses_langgraph_backend_for_goal_mode() -> None:
    from gigaforge.ui.chat import LangGraphBackend, default_engine

    engine = default_engine(
        store=None,  # type: ignore[arg-type]
        goal_mode=True,
        goal_runner_factory=lambda goal, **kw: {"status": "completed"},
    )
    assert isinstance(engine.backend, LangGraphBackend)


def test_chat_keeps_pipeline_backend_for_one_shot_runs() -> None:
    from gigaforge.ui.chat import PipelineBackend, default_engine

    engine = default_engine(store=None, goal_mode=False)  # type: ignore[arg-type]
    assert isinstance(engine.backend, PipelineBackend)

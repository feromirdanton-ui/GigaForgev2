"""A successful tool call must appear in the run record.

The successful path never appended to ``result.steps`` — only the blocked path
did — so the run record omitted every tool call that actually worked.  A live
probe read ``observation: None`` for a REPL step whose program had printed the
right answer, and any report built from ``result.steps`` described a run that
never touched a tool.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from gigaforge.agents.react import ReActRunner


class _Budgets:
    def check(self, *a, **k):
        return None

    def commit_task(self, *a, **k):
        return None

    def check_overshoot(self, *a, **k):
        return None


class _Router:
    def route(self, phase):
        return "m"


class _Tracer:
    def trace(self, *a, **k):
        return None


class _State:
    task_id = "t"

    def serialize_prefix(self):
        return {}


class _Ctx:
    def __init__(self, root: Path) -> None:
        self.worktree = root
        self.repo_root = root
        self.sandbox = None
        self.index = None


def _runner(client, max_steps: int = 3) -> ReActRunner:
    return ReActRunner(
        client=client, router=_Router(), budgets=_Budgets(), tracer=_Tracer(),
        max_steps=max_steps,
    )


def test_a_successful_tool_call_is_recorded_with_its_observation(tmp_path: Path) -> None:
    """The defect: only the blocked path wrote to result.steps."""

    class _Client:
        def __init__(self):
            self.calls = 0

        async def chat(self, *a, **k):
            from gigaforge.client.giga_client import ChatResult

            self.calls += 1
            if self.calls == 1:
                body = '{"thought":"look","action":"grep","action_input":"needle"}'
            else:
                body = '{"thought":"done","action":"finish","action_input":"ok"}'
            return ChatResult(content=body, usage={"prompt_tokens": 1, "completion_tokens": 1})

    class _Grep:
        name = "grep"

        async def run(self, action_input, *, ctx):
            return "src/a.py:1:needle here"

    runner = _runner(_Client())
    runner._registry.register(_Grep())
    result = asyncio.run(runner.run("find needle", _State(), ctx=_Ctx(tmp_path)))

    assert result.steps, "a successful tool call left no record in result.steps"
    first = result.steps[0]
    assert first["action"] == "grep"
    assert first["ok"] is True
    assert first["observation"], "the recorded step has no observation"
    assert "needle" in first["observation"]


def test_a_cached_step_is_marked_as_cached(tmp_path: Path) -> None:
    """A report must be able to tell a real call from a cache hit."""

    class _Client:
        def __init__(self):
            self.calls = 0

        async def chat(self, *a, **k):
            from gigaforge.client.giga_client import ChatResult

            self.calls += 1
            if self.calls <= 2:
                body = '{"thought":"look","action":"grep","action_input":"needle"}'
            else:
                body = '{"thought":"done","action":"finish","action_input":"ok"}'
            return ChatResult(content=body, usage={"prompt_tokens": 1, "completion_tokens": 1})

    class _Grep:
        name = "grep"
        calls = 0

        async def run(self, action_input, *, ctx):
            _Grep.calls += 1
            return "src/a.py:1:needle here"

    runner = _runner(_Client(), max_steps=4)
    runner._registry.register(_Grep())
    result = asyncio.run(runner.run("find needle", _State(), ctx=_Ctx(tmp_path)))

    assert _Grep.calls == 1
    assert result.steps[0]["cached"] is False
    assert result.steps[1]["cached"] is True

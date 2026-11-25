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
    def task_remaining_for(self, task_id):
        return 100_000

    def check(self, *a, **k):
        return None

    def commit_task(self, *a, **k):
        return None

    def check_overshoot(self, *a, **k):
        return None

    def reserve(self, *a, **k):
        from tests.budget_stubs import NullClaim

        return NullClaim()


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
        self.allow_file_writes = True
        self.read_files: set[str] = set()


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


def test_an_edited_file_is_reported_as_an_episode_write(tmp_path: Path) -> None:
    import json

    from gigaforge.client.giga_client import ChatResult

    path = tmp_path / "index.html"
    path.write_text("<h1>Old</h1>", encoding="utf-8")
    edit_payload = json.dumps(
        {
            "path": "index.html",
            "old_string": "<h1>Old</h1>",
            "new_string": "<h1>New</h1>",
        }
    )

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                body = (
                    '{"thought":"read the file first","action":"read_file",'
                    '"action_input":"index.html"}'
                )
            elif self.calls == 2:
                body = json.dumps(
                    {
                        "thought": "Update the headline",
                        "action": "edit_file",
                        "action_input": edit_payload,
                    }
                )
            else:
                body = '{"thought":"done","action":"finish","action_input":"ok"}'
            return ChatResult(
                content=body,
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )

    result = asyncio.run(
        _runner(_Client(), max_steps=4).run(
            "update the headline", _State(), ctx=_Ctx(tmp_path)
        )
    )

    assert path.read_text(encoding="utf-8") == "<h1>New</h1>"
    assert result.written_files == ["index.html"]

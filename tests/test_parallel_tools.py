"""Parallel exploration: several independent reads in one step.

A ReAct episode spends most of its steps on exploration, and exploration is
usually a set of *independent* reads — grep for one symbol, read the skeleton of
another file, run the suite.  One per step costs one model round trip each,
which is the dominant cost of a run.

The harness owns the safety question, not the model.  Only tools whose output is
a pure function of the tree may run concurrently, because two writes to the same
tree are not independent and their order is not defined.  A batch containing a
write is **refused**, not serialised: serialising it would let the model believe
it had asked for parallel work and received it, and the next batch would make
the same mistake.

What these tests pin:

1. A batch of read-only calls runs, and every call's observation reaches the
   trajectory — a batch that silently drops one of its results is worse than no
   batch at all.
2. The calls actually run concurrently, not one after another.  This is the
   whole point, and it is invisible in the output: a sequential implementation
   produces byte-identical observations.  The test measures wall-clock overlap.
3. A batch containing a write is refused, and nothing in it runs.
4. An unknown tool, an oversized batch and an unparseable payload are each
   reported as a refusal rather than crashing the episode.
5. One failing call does not sink the batch: the others still report.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from gigaforge.agents.react import (
    MAX_PARALLEL_CALLS,
    PARALLEL_ACTION,
    ReActRunner,
)


class _Budgets:
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
    def __init__(self) -> None:
        self.events: list[dict] = []

    def trace(self, task_id, step, kind, detail=None, tokens=None, latency_ms=None):
        self.events.append({"step": step, "kind": kind, "detail": detail or {}})

    def steps_named(self, needle: str) -> list[dict]:
        return [e for e in self.events if needle in e["step"]]


class _State:
    task_id = "parallel"

    def serialize_prefix(self):
        return {}


class _Ctx:
    def __init__(self, root: Path) -> None:
        self.worktree = root
        self.repo_root = root
        self.sandbox = None
        self.index = None


class _SlowRead:
    """A read-only tool that takes measurable time, so overlap is observable."""

    def __init__(self, name: str, delay: float = 0.15) -> None:
        self.name = name
        self.delay = delay
        self.calls: list[str] = []

    async def run(self, action_input, *, ctx):
        self.calls.append(str(action_input))
        await asyncio.sleep(self.delay)
        return f"{self.name} result for {action_input}"


class _Writer:
    """A tool that can change the tree — must never run in a batch."""

    name = "edit_function"

    def __init__(self) -> None:
        self.ran = False

    async def run(self, action_input, *, ctx):
        self.ran = True
        return "edited"


def _runner(client, tracer, *, max_steps: int = 4) -> ReActRunner:
    return ReActRunner(
        client=client,
        router=_Router(),
        budgets=_Budgets(),
        tracer=tracer,
        max_steps=max_steps,
    )


def _client_scripted(actions: list[dict]):
    """A client that emits the given actions in order, then finishes."""

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, model, messages, *a, **k):
            from gigaforge.client.giga_client import ChatResult

            self.calls += 1
            if self.calls <= len(actions):
                body = json.dumps(actions[self.calls - 1])
            else:
                body = json.dumps({"thought": "done", "action": "finish", "action_input": "[]"})
            return ChatResult(
                content=body, usage={"prompt_tokens": 10, "completion_tokens": 10}
            )

    return _Client()


def test_a_batch_runs_and_every_result_reaches_the_trajectory(tmp_path: Path) -> None:
    """A batch that drops one of its results is worse than no batch at all.

    The tool names are the two the harness actually classifies as read-only
    (``CACHEABLE_TOOLS``): ``grep`` and ``read_structure``.  ``repl`` is
    deliberately excluded there — it can execute arbitrary code, so it can
    write — and a batch containing it is refused, which is a different test.
    """
    tracer = _Tracer()
    runner = _runner(
        _client_scripted(
            [
                {
                    "thought": "explore",
                    "action": PARALLEL_ACTION,
                    "action_input": json.dumps(
                        [
                            {"tool": "read_structure", "args": "alpha.py"},
                            {"tool": "grep", "args": "beta"},
                        ]
                    ),
                }
            ]
        ),
        tracer,
    )
    runner._registry.register(_SlowRead("read_structure"))
    runner._registry.register(_SlowRead("grep"))

    result = asyncio.run(runner.run("explore", _State(), ctx=_Ctx(tmp_path)))

    parallel_steps = [s for s in result.steps if s.get("action") == PARALLEL_ACTION]
    assert parallel_steps, "the parallel action left no record in result.steps"
    step = parallel_steps[0]
    assert step["ok"] is True
    assert step["parallel_calls"] == ["read_structure", "grep"]
    # Both observations must be present, not just the first.  grep's body is
    # reduced to its envelope by the Firewall (correct for a real grep), so the
    # assertion is on the envelope's presence, not on the raw text.
    assert "read_structure result for alpha.py" in step["observation"]
    assert "Observation (grep) [grep]" in step["observation"]
    assert tracer.steps_named("_parallel"), "the batch was not traced"


def test_the_calls_actually_overlap(tmp_path: Path) -> None:
    """Sequential execution produces identical output — only time tells them apart.

    This is the assertion that makes the feature real.  A batch of four calls
    that each sleep 150 ms takes ~600 ms sequentially and ~150 ms concurrently;
    the test asserts the concurrent figure, so an implementation that merely
    loops over the calls fails it.
    """
    tracer = _Tracer()
    runner = _runner(
        _client_scripted(
            [
                {
                    "thought": "explore",
                    "action": PARALLEL_ACTION,
                    "action_input": json.dumps(
                        [
                            {"tool": "grep", "args": "a"},
                            {"tool": "grep", "args": "b"},
                            {"tool": "grep", "args": "c"},
                            {"tool": "grep", "args": "d"},
                        ]
                    ),
                }
            ]
        ),
        tracer,
    )
    tool = _SlowRead("grep", delay=0.15)
    runner._registry.register(tool)

    started = time.monotonic()
    asyncio.run(runner.run("explore", _State(), ctx=_Ctx(tmp_path)))
    elapsed = time.monotonic() - started

    assert len(tool.calls) == 4, f"expected 4 calls, got {tool.calls}"
    # Sequential would be >= 0.6 s.  Allow generous headroom for a loaded CI
    # box while still failing a sequential implementation by a wide margin.
    assert elapsed < 0.45, (
        f"four 150 ms calls took {elapsed:.3f}s — they ran one after another, "
        "not concurrently"
    )


def test_a_batch_with_a_write_is_refused_and_nothing_runs(tmp_path: Path) -> None:
    """Two writes to one tree are not independent; the harness refuses, not serialises."""
    tracer = _Tracer()
    writer = _Writer()
    runner = _runner(
        _client_scripted(
            [
                {
                    "thought": "explore and edit",
                    "action": PARALLEL_ACTION,
                    "action_input": json.dumps(
                        [
                            {"tool": "grep", "args": "alpha"},
                            {"tool": "edit_function", "args": {"name": "f"}},
                        ]
                    ),
                }
            ]
        ),
        tracer,
    )
    reader = _SlowRead("grep")
    runner._registry.register(reader)
    runner._registry.register(writer)

    result = asyncio.run(runner.run("explore", _State(), ctx=_Ctx(tmp_path)))

    assert writer.ran is False, "a write ran inside a parallel batch"
    assert reader.calls == [], "the read ran even though the batch was refused"
    refused = [s for s in result.steps if s.get("action") == PARALLEL_ACTION]
    assert refused and refused[0]["ok"] is False
    assert "cannot run in parallel" in refused[0]["error"]


def test_an_unknown_tool_is_refused(tmp_path: Path) -> None:
    tracer = _Tracer()
    runner = _runner(
        _client_scripted(
            [
                {
                    "thought": "explore",
                    "action": PARALLEL_ACTION,
                    "action_input": json.dumps([{"tool": "nope", "args": "x"}]),
                }
            ]
        ),
        tracer,
    )
    runner._registry.register(_SlowRead("grep"))
    result = asyncio.run(runner.run("explore", _State(), ctx=_Ctx(tmp_path)))
    refused = [s for s in result.steps if s.get("action") == PARALLEL_ACTION]
    assert refused and refused[0]["ok"] is False
    assert "unknown tool" in refused[0]["error"]


def test_an_oversized_batch_is_refused(tmp_path: Path) -> None:
    tracer = _Tracer()
    runner = _runner(
        _client_scripted(
            [
                {
                    "thought": "explore",
                    "action": PARALLEL_ACTION,
                    "action_input": json.dumps(
                        [{"tool": "grep", "args": str(i)} for i in range(MAX_PARALLEL_CALLS + 1)]
                    ),
                }
            ]
        ),
        tracer,
    )
    tool = _SlowRead("grep")
    runner._registry.register(tool)
    result = asyncio.run(runner.run("explore", _State(), ctx=_Ctx(tmp_path)))
    refused = [s for s in result.steps if s.get("action") == PARALLEL_ACTION]
    assert refused and refused[0]["ok"] is False
    assert "too many calls" in refused[0]["error"]
    assert tool.calls == [], "an oversized batch ran anyway"


def test_an_unparseable_payload_is_refused(tmp_path: Path) -> None:
    tracer = _Tracer()
    runner = _runner(
        _client_scripted(
            [
                {
                    "thought": "explore",
                    "action": PARALLEL_ACTION,
                    "action_input": "not json at all",
                }
            ]
        ),
        tracer,
    )
    runner._registry.register(_SlowRead("grep"))
    result = asyncio.run(runner.run("explore", _State(), ctx=_Ctx(tmp_path)))
    refused = [s for s in result.steps if s.get("action") == PARALLEL_ACTION]
    assert refused and refused[0]["ok"] is False
    assert "no usable calls" in refused[0]["error"]


def test_one_failing_call_does_not_sink_the_batch(tmp_path: Path) -> None:
    """The other calls' observations must still reach the model."""

    class _Boom:
        name = "read_structure"

        async def run(self, action_input, *, ctx):
            if action_input == "boom":
                raise RuntimeError("tool exploded")
            return f"read_structure result for {action_input}"

    tracer = _Tracer()
    runner = _runner(
        _client_scripted(
            [
                {
                    "thought": "explore",
                    "action": PARALLEL_ACTION,
                    "action_input": json.dumps(
                        [
                            {"tool": "read_structure", "args": "boom"},
                            {"tool": "grep", "args": "ok"},
                        ]
                    ),
                }
            ]
        ),
        tracer,
    )
    runner._registry.register(_Boom())
    runner._registry.register(_SlowRead("grep", delay=0.0))

    result = asyncio.run(runner.run("explore", _State(), ctx=_Ctx(tmp_path)))
    step = [s for s in result.steps if s.get("action") == PARALLEL_ACTION][0]
    assert step["ok"] is True, "one failing call sank the whole batch"
    assert "tool exploded" in step["observation"]
    assert "Observation (grep) [grep]" in step["observation"]


def test_a_tool_error_survives_a_compressing_kind(tmp_path: Path) -> None:
    """An error must reach the model verbatim, even for a compressing tool.

    The grep compressor turns *any* text into a ``{"matches": [...]}`` envelope.
    A tool that raised therefore reached the model as an empty result set, which
    reads as "the symbol is not in the tree" — the opposite of the truth.  The
    harness could not tell the two apart because the error text was already
    gone.

    This test uses ``grep`` specifically: ``read_structure`` maps to a
    pass-through compressor, so a test on it would pass even with the error
    guard removed.  Verified mutationally — returning ``False`` from
    ``_is_error_observation`` fails this test and only this test.
    """

    class _Boom:
        name = "grep"

        async def run(self, action_input, *, ctx):
            raise RuntimeError("tool exploded")

    tracer = _Tracer()
    runner = _runner(
        _client_scripted(
            [
                {
                    "thought": "look",
                    "action": "grep",
                    "action_input": "needle",
                }
            ]
        ),
        tracer,
    )
    runner._registry.register(_Boom())

    result = asyncio.run(runner.run("explore", _State(), ctx=_Ctx(tmp_path)))
    step = [s for s in result.steps if s.get("action") == "grep"][0]
    assert "tool exploded" in step["observation"], (
        "the error was compressed into an empty result set: "
        f"{step['observation']!r}"
    )
    assert "matches" not in step["observation"], (
        "the error was turned into a grep envelope"
    )

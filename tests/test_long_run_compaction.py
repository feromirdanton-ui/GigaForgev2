"""A long run that actually compacts — the one link the harness never proved.

Every previous iteration reported the same gap: "compaction at 85% was not
reproduced on a live long trajectory; the runs fit inside the window."  That is
an honest gap, but it is also the *only* recovery path in the harness, and a
recovery path nobody has executed is a recovery path that does not work.

The obstacle is arithmetic: the real window is 261 120 tokens, so a trajectory
that reaches 85% of it needs a quarter of a million tokens of text.  Building
that in a test is possible but slow and tells you nothing extra — the code path
is identical at 2 000 tokens and at 200 000.

So the guard is injected with a small window.  That is not a mock of the
behaviour under test: ``ContextWindowGuard`` is the real class, ``_compact`` is
the real method, ``compact_trajectory`` is the real compactor.  Only the
*number* is smaller, which is exactly the knob a deployment with a smaller
model would turn.

What these tests pin:

1. A trajectory that crosses the trigger is compacted and the run **continues**
   — it does not die with ``context_overflow``.  Detection without recovery was
   the original defect.
2. The compaction is **recorded** in the trace, because a run that silently
   rewrote its own history is a run whose token accounting needs a note.
3. The compaction is **bounded**: a trajectory that cannot be shrunk enough
   stops the run instead of oscillating between compact and overflow.
4. The **goal survives** compaction.  A compactor that drops the objective
   leaves the model solving nothing.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gigaforge.agents.react import ReActRunner
from gigaforge.core.context_window_guard import ContextWindowGuard


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
    """Records every trace call so the test can assert what was reported."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def trace(self, task_id, step, kind, detail=None, tokens=None, latency_ms=None):
        self.events.append({"step": step, "kind": kind, "detail": detail or {}})

    def steps_named(self, needle: str) -> list[dict]:
        return [e for e in self.events if needle in e["step"]]


class _State:
    task_id = "long-run"

    def serialize_prefix(self):
        return {}


class _Ctx:
    def __init__(self, root: Path) -> None:
        self.worktree = root
        self.repo_root = root
        self.sandbox = None
        self.index = None


class _BigRepl:
    """A tool whose observation is large enough to grow the trajectory fast.

    Deliberately NOT ``grep``: the grep compressor reduces any output to a
    ``{"matches": [...]}`` envelope, so a grep observation is ~14 characters no
    matter how much the tool printed.  That is the Firewall doing its job, but
    it means a grep-based fixture can never grow a trajectory.  ``repl`` maps to
    the ``builderr`` compressor, which bounds output at the observation budget
    instead of discarding it — which is what a real long run looks like.
    """

    name = "repl"

    def __init__(self, lines: int = 120) -> None:
        self.lines = lines
        self.calls = 0

    async def run(self, action_input, *, ctx):
        self.calls += 1
        # A distinct body per call, so the observation cache cannot collapse
        # the trajectory and hide the growth this test exists to create.
        return f"run {self.calls}\n" + "\n".join(
            f"line {i} of output from run {self.calls}" for i in range(self.lines)
        )


def _runner(client, tracer, *, window: int, max_steps: int = 12) -> ReActRunner:
    return ReActRunner(
        client=client,
        router=_Router(),
        budgets=_Budgets(),
        tracer=tracer,
        max_steps=max_steps,
        window_guard=ContextWindowGuard(context_window=window, trigger_fraction=0.85),
    )


def test_a_long_run_compacts_and_keeps_going(tmp_path: Path) -> None:
    """The run crosses the trigger, compacts, and still finishes.

    Before this, the guard's verdict *ended* the run: a long episode died at
    80% of the window with most of its trajectory being stale observation text
    nobody needed any more.
    """

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *a, **k):
            from gigaforge.client.giga_client import ChatResult

            self.calls += 1
            if self.calls < 10:
                body = json.dumps(
                    {"thought": "keep looking", "action": "repl", "action_input": f"q{self.calls}"}
                )
            else:
                body = json.dumps(
                    {
                        "thought": "enough",
                        "action": "finish",
                        "action_input": json.dumps(
                            [{"tool": "read_structure", "args": {"path": "a.py"}}]
                        ),
                    }
                )
            return ChatResult(
                content=body, usage={"prompt_tokens": 10, "completion_tokens": 10}
            )

    tracer = _Tracer()
    # The opening message (harness prompt + task) is ~1 400 tokens on its own,
    # and each repl observation adds ~400 after compression.  A 6 000-token
    # window with a 0.85 trigger is crossed around the ninth step, which is
    # early enough to compact and still finish inside the step budget.
    runner = _runner(_Client(), tracer, window=6000)
    runner._registry.register(_BigRepl(lines=120))

    result = asyncio.run(runner.run("find the needle", _State(), ctx=_Ctx(tmp_path)))

    compactions = tracer.steps_named("_compaction")
    assert compactions, (
        "the trajectory crossed the trigger but no compaction was recorded — "
        "the guard either never fired or the run died instead of recovering"
    )
    assert result.finish_reason != "context_overflow", (
        "the run stopped on overflow instead of compacting and continuing"
    )
    assert result.finished, "the run did not finish after compacting"
    # The compaction must be reported with before/after numbers, not just a flag.
    detail = compactions[0]["detail"]
    assert detail["before_tokens"] > detail["after_tokens"], detail
    assert detail["dropped_messages"] >= 1, detail


def test_the_goal_survives_compaction(tmp_path: Path) -> None:
    """A compactor that drops the objective leaves the model solving nothing."""

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, model, messages, *a, **k):
            from gigaforge.client.giga_client import ChatResult

            self.calls += 1
            # The goal must still be in the request on every single call.
            joined = " ".join(str(m.get("content", "")) for m in messages)
            assert "find the needle" in joined, (
                f"the goal was dropped from the request on call {self.calls}"
            )
            if self.calls < 10:
                body = json.dumps(
                    {"thought": "look", "action": "repl", "action_input": f"q{self.calls}"}
                )
            else:
                body = json.dumps({"thought": "done", "action": "finish", "action_input": "[]"})
            return ChatResult(
                content=body, usage={"prompt_tokens": 10, "completion_tokens": 10}
            )

    tracer = _Tracer()
    runner = _runner(_Client(), tracer, window=6000)
    runner._registry.register(_BigRepl(lines=120))
    asyncio.run(runner.run("find the needle", _State(), ctx=_Ctx(tmp_path)))
    assert tracer.steps_named("_compaction"), "this test needs a compaction to be meaningful"


def test_compaction_is_bounded(tmp_path: Path) -> None:
    """A trajectory that cannot be shrunk enough stops instead of oscillating.

    ``_MAX_COMPACTIONS`` exists so a pathological trajectory cannot spin
    between "compact" and "overflow" forever.  The bound is only real if the
    run actually stops when it is reached.
    """

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *a, **k):
            from gigaforge.client.giga_client import ChatResult

            self.calls += 1
            # Never finishes: every step is another huge observation.
            body = json.dumps(
                {"thought": "look", "action": "repl", "action_input": f"q{self.calls}"}
            )
            return ChatResult(
                content=body, usage={"prompt_tokens": 10, "completion_tokens": 10}
            )

    tracer = _Tracer()
    # A window smaller than the opening message alone: compaction cannot buy
    # room because the goal itself does not fit, so the run must stop rather
    # than compact forever.
    runner = _runner(_Client(), tracer, window=1200, max_steps=20)
    runner._registry.register(_BigRepl(lines=120))

    result = asyncio.run(runner.run("find the needle", _State(), ctx=_Ctx(tmp_path)))

    compactions = tracer.steps_named("_compaction")
    # Bounded: at most _MAX_COMPACTIONS (2) consecutive compactions, then a stop.
    assert len(compactions) <= 2, f"compaction ran {len(compactions)} times, unbounded"
    assert result.finish_reason in {"context_overflow", "max_steps", "forced_finish"}, (
        f"the run ended with {result.finish_reason!r} instead of stopping cleanly"
    )


def test_a_long_run_may_compact_more_than_the_streak_cap(tmp_path: Path) -> None:
    """The bound is on the *streak*, not on the total.

    This is the assertion that distinguishes the two readings, and it is the
    one the first version of this module was missing: a total cap of two and a
    streak cap of two behave identically on a run that compacts twice, so a
    test that only ever compacts twice cannot tell them apart.

    A long run legitimately compacts many times — it grows, compacts, grows
    again, compacts again.  Measured: a 20-step run at a 6 000-token window
    compacts three times, each time with a streak of one, and finishes.  Under
    the old total cap it died at the third compaction with
    ``context_overflow`` while it was still making progress.
    """

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *a, **k):
            from gigaforge.client.giga_client import ChatResult

            self.calls += 1
            if self.calls < 20:
                body = json.dumps(
                    {"thought": "keep looking", "action": "repl", "action_input": f"q{self.calls}"}
                )
            else:
                body = json.dumps({"thought": "done", "action": "finish", "action_input": "[]"})
            return ChatResult(
                content=body, usage={"prompt_tokens": 10, "completion_tokens": 10}
            )

    tracer = _Tracer()
    runner = _runner(_Client(), tracer, window=6000, max_steps=22)
    runner._registry.register(_BigRepl(lines=120))

    result = asyncio.run(runner.run("find the needle", _State(), ctx=_Ctx(tmp_path)))

    compactions = tracer.steps_named("_compaction")
    assert len(compactions) > 2, (
        f"this run only compacted {len(compactions)} time(s); it needs more than "
        "the streak cap to distinguish a streak bound from a total bound"
    )
    # Every compaction was isolated: a productive step separated them, so the
    # streak never exceeded one.
    assert all(c["detail"]["compaction_streak"] == 1 for c in compactions), (
        [c["detail"]["compaction_streak"] for c in compactions]
    )
    assert result.finish_reason != "context_overflow", (
        "a long run that keeps making progress was killed by the compaction cap"
    )
    assert result.finished, "the run did not finish"

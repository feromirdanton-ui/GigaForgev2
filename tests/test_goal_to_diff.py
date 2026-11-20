"""The test the review asked for: goal -> plan -> diff, end to end.

Three review rounds found the same shape of defect — a component that exists,
is tested, and that the product never reaches:

* review 1: ``response_format`` is passed, but nobody asks for it;
* review 2: ``plan_guard`` exists, but not on the path the product uses;
* review 3: the plan exists, but nobody reads it.

Each was invisible to the suite because every test stopped one layer short of
the next.  This module drives the whole chain — ``TaskPipeline.run(goal)`` with a
fake provider — and asserts on the *diff that comes out*, so a break anywhere
between the goal and the diff fails here.

The three findings it pins down at once:

* **N1** — the plan the episode produced must reach the coder's prompt.
* **N2** — the harness must dispatch through ``run_react``, so
  ``GIGAFORGE_REACT_ENGINE`` actually selects an engine.
* **N3** — the episode's trajectory must survive every return branch, so the
  salvage works on the ``finish_action`` path and not only on ``max_steps``.

Offline and deterministic: no GigaChat key, no network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


# ── a provider that answers by phase ────────────────────────────────────────
class _ScriptedProvider:
    """Answers each phase from a script, and records every prompt it saw.

    The ReAct phase and the code phase need different answers, and the whole
    point of these tests is what the *code* phase was told — so the prompts are
    kept, not just the call count.
    """

    def __init__(self, *, react_script: list[dict[str, Any]] | None = None,
                 diff: str = "") -> None:
        self.react_script = react_script or []
        self.diff = diff
        self.prompts: list[str] = []
        self.react_calls = 0

    async def chat(self, messages: Any, *, model: str, **kwargs: Any) -> Any:
        from gigaforge.client.giga_client import ChatResult

        text = "\n".join(
            str(m.get("content") or "") for m in messages if isinstance(m, dict)
        )
        self.prompts.append(text)
        is_react = "ReAct" in text or "action_input" in text
        if is_react and self.react_script:
            index = min(self.react_calls, len(self.react_script) - 1)
            self.react_calls += 1
            body = self.react_script[index]
        elif is_react:
            body = {"thought": "done", "action": "finish", "action_input": []}
        else:
            body = self.diff
        content = body if isinstance(body, str) else json.dumps(body)
        return ChatResult(
            content=content,
            usage={"prompt_tokens": 11, "completion_tokens": 7},
        )

    async def list_models(self) -> list[dict[str, Any]]:
        return []

    def capabilities(self) -> Any:
        return None


def _repo(tmp: Path) -> Path:
    """A tiny repository with a real bug and a real test."""
    (tmp / "math_utils.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a - b\n"
    )
    (tmp / "test_math_utils.py").write_text(
        "from math_utils import add\n\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    )
    return tmp


def _pipeline(tmp: Path, provider: Any) -> Any:
    from gigaforge.core.config import CoreConfig
    from gigaforge.core.pipeline import TaskPipeline
    from gigaforge.core.sync_adapter import SyncChatClient

    config = CoreConfig(
        db_path=str(tmp / "core.sqlite3"),
        trace_path=str(tmp / "traces" / "core.jsonl"),
        repo_root=str(tmp),
    )
    return TaskPipeline(config=config, client=SyncChatClient(provider))


# ── N1: the plan reaches the coder ──────────────────────────────────────────
def test_the_plan_reaches_the_coder_prompt(tmp_path: Path) -> None:
    """The coder must be told the plan the episode paid for.

    Before the fix ``_node_act`` declared ``plan`` as a required input and never
    read it, so the episode's 4-6 model calls were billed and discarded while
    the coder re-derived the target from the goal.

    The marker is the *rendered plan line*, not the symbol name: an earlier
    version of this test asserted on ``"add"``, which is a substring of the goal
    itself, so it passed with the plan block removed entirely.  A test whose
    marker is already in the input proves nothing.
    """
    from gigaforge.core.pipeline import RunContext
    from gigaforge.core.state import TaskState

    _repo(tmp_path)
    pipeline = _pipeline(tmp_path, _ScriptedProvider())

    ctx = RunContext(run_id="r1", state=TaskState(run_id="r1", goal="fix the helper"))
    ctx.extras["localization"] = {"target_function": "add", "target_file": "math_utils.py"}
    block = pipeline._plan_block(ctx, {"plan": ["edit_function: add", "run_tests"]})

    assert "edit_function: add" in block, block
    assert "math_utils.py" in block, block
    assert "## Plan" in block, block
    assert "## Localization" in block, block


def test_a_linear_plan_leaves_the_prompt_unchanged(tmp_path: Path) -> None:
    """No plan and no localisation must produce no block at all.

    The fix must not change the prompt for the strategies that have nothing to
    say — otherwise every cached prefix in the project is invalidated.
    """
    from gigaforge.core.pipeline import RunContext
    from gigaforge.core.state import TaskState

    _repo(tmp_path)
    pipeline = _pipeline(tmp_path, _ScriptedProvider())
    ctx = RunContext(run_id="r2", state=TaskState(run_id="r2", goal="g"))
    assert pipeline._plan_block(ctx, {}) == ""


# ── N2: the harness dispatches to the selected engine ───────────────────────
def test_the_harness_dispatches_through_run_react(tmp_path: Path) -> None:
    """``Harness.run(react=True)`` must call ``run_react``, not ``runner.run``.

    ``run_react`` is the only function that reads ``GIGAFORGE_REACT_ENGINE``, so
    a harness that called ``runner.run`` itself made the LangGraph ReAct graph
    unreachable while the ledger still recorded ``engine: react``.
    """
    from gigaforge.harness import Harness, HarnessPolicy

    seen: list[str] = []

    class _Runner:
        def __init__(self) -> None:
            self.state = type("S", (), {"task_id": "t1"})()

        async def run(self, task: str, state: Any, **kw: Any) -> Any:
            seen.append("runner.run")
            return type("R", (), {"finish_reason": "classic", "steps": [], "llm_calls": 0})()

    import gigaforge.agents.engine as engine

    original = engine.run_react

    def _spy(runner: Any, task: str, state: Any, **kw: Any) -> Any:
        seen.append(f"run_react:{kw.get('engine')}")
        return type("R", (), {"finish_reason": "react", "steps": [], "llm_calls": 0})()

    engine.run_react = _spy  # type: ignore[assignment]
    try:
        harness = Harness(policy=HarnessPolicy(sleep=lambda _s: None))
        outcome = harness.run(
            _Runner(), "goal", _Runner().state, run_id="r", engine="classic", react=True
        )
    finally:
        engine.run_react = original  # type: ignore[assignment]

    assert seen == ["run_react:classic"]
    assert outcome.ledger.engine == "classic"


def test_the_harness_still_calls_a_plain_runner_directly(tmp_path: Path) -> None:
    """``react=False`` keeps the old behaviour for non-ReAct runners."""
    from gigaforge.harness import Harness, HarnessPolicy

    seen: list[str] = []

    class _Runner:
        def __init__(self) -> None:
            self.state = type("S", (), {"task_id": "t1"})()

        async def run(self, task: str, state: Any, **kw: Any) -> Any:
            seen.append("runner.run")
            return type("R", (), {"finish_reason": "ok", "steps": [], "llm_calls": 0})()

    harness = Harness(policy=HarnessPolicy(sleep=lambda _s: None))
    harness.run(_Runner(), "goal", _Runner().state, run_id="r")
    assert seen == ["runner.run"]


# ── N3: the trajectory survives the finish branch ───────────────────────────
@pytest.mark.parametrize("engine", ["classic", "langgraph"])
def test_a_finish_with_an_unusable_plan_still_salvages(
    tmp_path: Path, engine: str, monkeypatch: Any
) -> None:
    """The live scenario: grep, find the files, finish with an untargeted edit.

    The guard correctly drops the untargeted step.  The salvage must then turn
    the observations into read steps — and it can only do that if the trajectory
    was carried out of the episode, which it was not on this branch.

    Parametrised over **both** engines on purpose.  The first version of this
    test ran only the default engine (``langgraph``), so mutating the classic
    loop's return path did not fail it — the test was green for a reason that
    had nothing to do with the code it claimed to cover.  N3 was a defect in
    ``react.py``; a test that never executes ``react.py`` cannot catch it.
    """
    from gigaforge.agents.engine import run_react

    monkeypatch.setenv("GIGAFORGE_REACT_ENGINE", engine)
    _repo(tmp_path)
    (tmp_path / "README.md").write_text("# TODO: document add\n")
    provider = _ScriptedProvider(
        react_script=[
            {"thought": "find it", "action": "grep", "action_input": "TODO"},
            {"thought": "done", "action": "finish",
             "action_input": [{"tool": "edit_function", "args": {}}]},
        ]
    )
    pipeline = _pipeline(tmp_path, provider)
    runner = pipeline._build_react_runner("find the TODO markers")
    result = run_react(
        runner, "find the TODO markers", runner.state, ctx=runner.ctx,
        thread_id=f"run-n3-{engine}", engine=engine,
    )

    assert result.finish_reason == "finish_action", result.finish_reason
    assert result.trajectory, (
        f"N3 ({engine}): the trajectory must survive the finish branch, or the "
        "salvage written for exactly this case can never run"
    )
    assert any("TODO" in str(m.get("content", "")) for m in result.trajectory), (
        "the observation the episode paid for must be in the trajectory"
    )


# ── the whole chain: goal -> plan -> diff ───────────────────────────────────
def test_goal_to_diff_end_to_end(tmp_path: Path) -> None:
    """Drive ``TaskPipeline.run(goal)`` and assert on the diff that comes out.

    This is the test whose absence let three rounds of review find the same
    defect: every other test stopped one layer short of the next, so a break in
    the *connection* between two working components was invisible.
    """
    _repo(tmp_path)
    diff = (
        "```diff\n"
        "--- a/math_utils.py\n"
        "+++ b/math_utils.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a: int, b: int) -> int:\n"
        "-    return a - b\n"
        "+    return a + b\n"
        "```\n"
    )
    provider = _ScriptedProvider(
        react_script=[
            {"thought": "locate add", "action": "grep", "action_input": "def add"},
            {"thought": "plan", "action": "finish",
             "action_input": {"steps": [
                 {"tool": "edit_function", "args": {"name": "add"}},
                 {"tool": "run_tests", "args": {}},
             ]}},
        ],
        diff=diff,
    )
    pipeline = _pipeline(tmp_path, provider)

    result = pipeline.run("the arithmetic helper returns the wrong result")

    # The diff is the deliverable: it must contain the corrected line.
    assert "return a + b" in (result.diff or ""), result.diff

    # N1: the coder was told the plan.  The marker is the rendered plan line,
    # which cannot appear in the goal — asserting on a symbol name that the goal
    # already contains would pass with the plan block removed.
    code_prompts = [p for p in provider.prompts if "action_input" not in p]
    assert code_prompts, "the coder was never called"
    assert any("edit_function: add" in p for p in code_prompts), (
        "N1: the plan never reached the coder"
    )

    # N2: the ReAct episode ran through the engine dispatcher.
    assert provider.react_calls >= 1, "the ReAct episode never ran"


def test_the_ledger_records_that_the_plan_was_used(tmp_path: Path) -> None:
    """A run that pays for exploration and discards it must be visible.

    ``plan_used`` is the column that makes N1 a reportable fact instead of a
    review finding: without it, a discarded plan looks exactly like a used one.
    """
    from gigaforge.harness.ledger import RunLedger

    ledger = RunLedger(run_id="r", goal="g", engine="react")
    ledger.note_plan(used=True, steps=3, salvaged=False)
    payload = ledger.to_dict()
    assert payload["plan_used"] is True
    assert payload["plan_steps_executed"] == 3
    assert payload["salvaged"] is False
    assert "plan used" in ledger.summary_line()


@pytest.mark.parametrize("fraction", ["0.5", "0.9"])
def test_compact_at_is_read_from_the_environment(monkeypatch: Any, fraction: str) -> None:
    """``GIGAFORGE_COMPACT_AT`` was documented and printed, and never read."""
    from gigaforge.core.context_window_guard import ContextWindowGuard

    monkeypatch.setenv("GIGAFORGE_COMPACT_AT", fraction)
    assert ContextWindowGuard().trigger_fraction == float(fraction)


def test_an_unparseable_compact_at_keeps_the_default(monkeypatch: Any) -> None:
    """A bad value must not raise: the guard is on the hot path of every call."""
    from gigaforge.core.context_window_guard import ContextWindowGuard

    monkeypatch.setenv("GIGAFORGE_COMPACT_AT", "not-a-number")
    assert ContextWindowGuard().trigger_fraction == 0.85

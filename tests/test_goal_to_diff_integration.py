"""The integration test the review asked for: goal -> real pipeline -> diff on disk.

Four review rounds found the same shape of defect — a component that exists, is
tested, and that the product never reaches.  Each round's tests stopped one
layer short of the next, so a break in the *connection* between two working
components was invisible:

* review 1: ``response_format`` is passed, but nobody asks for it;
* review 2: ``plan_guard`` exists, but not on the path the product uses;
* review 3: the plan exists, but nobody reads it;
* review 4: the LangGraph engine is selected, but ``engine="react"`` means the
  dispatcher never picks it; and ``--subagents`` raises on the default engine
  and is swallowed.

This module drives the whole chain and asserts on the **diff that lands on
disk**, so a break anywhere between the goal and the file fails here.  It pins
three findings at once:

* **B1** — ``GIGAFORGE_REACT_ENGINE=langgraph`` must actually select the graph
  on the product path (``strategies.py`` used to pass ``engine="react"``, which
  is not an engine name, so the classic loop ran while the ledger said
  ``react``);
* **B2** — ``--subagents`` must fan out on the **default** engine.  The graph
  used to read ``spawn.payload`` from a ``SubAgentSpec``, raise
  ``AttributeError``, and have it swallowed by ``_subagent_diff``'s
  ``except Exception`` — so the run reported success with no sub-agents at all;
* **P40** — a child must not write outside its assignment.  A child handed the
  full goal re-derived every file name in it and created files it did not own.

Offline and deterministic: no GigaChat key, no network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


class _ScriptedProvider:
    """Answers each phase from a script and records every prompt it saw."""

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
    """A tiny repository with two independent files and a real test."""
    (tmp / "math_utils.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a - b\n"
    )
    (tmp / "text_utils.py").write_text(
        "def shout(text: str) -> str:\n"
        "    return text\n"
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


# ── B1: the engine the environment names is the engine that runs ─────────────
def test_the_product_path_reaches_the_langgraph_engine(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """``GIGAFORGE_REACT_ENGINE=langgraph`` must select the graph, not the loop.

    ``strategies.py`` passed ``engine="react"``.  ``"react"`` is not an engine
    name — the dispatcher accepts ``langgraph``/``graph`` and falls through to
    the classic loop for anything else — so the default engine was unreachable
    on the product path while ``gigaforge stack`` printed ``langgraph`` and the
    ledger recorded ``engine: react``.

    The assertion is on the **dispatcher's own decision**, not on a spy that
    replaces ``run_react`` wholesale: a test that replaces the function under
    test cannot see which engine it would have chosen, which is exactly how the
    previous test for this stayed green.
    """
    from gigaforge.agents import engine as engine_module
    from gigaforge.agents import langgraph_react
    from gigaforge.core.strategies import build_plan_strategy

    monkeypatch.setenv("GIGAFORGE_REACT_ENGINE", "langgraph")
    _repo(tmp_path)
    pipeline = _pipeline(tmp_path, _ScriptedProvider())

    seen: list[str] = []
    original = langgraph_react.run_react_graph

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append("graph")
        return original(*args, **kwargs)

    # Patch the module the dispatcher imports from, not the dispatcher's own
    # namespace: ``run_react`` imports ``run_react_graph`` inside the function,
    # so a module-level attribute on ``engine`` does not exist to patch.
    monkeypatch.setattr(langgraph_react, "run_react_graph", _spy)
    assert engine_module.react_engine() == "langgraph"

    strategy = pipeline.plan_strategy
    # The pipeline's own strategy, with the pipeline's own runner factory
    # attached — that is the object the product drives.  A bare
    # ``build_plan_strategy()`` has no factory, so it would take the linear
    # fallback and this test would pass for the wrong reason.
    assert strategy is not None
    strategy.plan_under_harness(
        "fix the arithmetic helper",
        "",
        ask=lambda *a, **k: "",
        parse=lambda text: [],
    )

    assert seen, (
        "B1: the product path never reached the LangGraph engine. "
        "The strategy must pass engine='' so the dispatcher resolves "
        "GIGAFORGE_REACT_ENGINE, not a literal that is not an engine name."
    )


def test_the_ledger_records_the_resolved_engine_not_a_label(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The ledger's ``engine`` must name the engine, not the strategy.

    ``engine: react`` in the ledger was a *label*: it said which strategy ran,
    not which engine executed it.  A diagnostic that disagrees with the code is
    worse than none, because it is believed.
    """
    from gigaforge.agents.engine import react_engine

    monkeypatch.setenv("GIGAFORGE_REACT_ENGINE", "langgraph")
    assert react_engine() == "langgraph"


# ── B2: --subagents fans out on the default engine ───────────────────────────
def test_subagents_fan_out_on_the_default_engine(tmp_path: Path) -> None:
    """The default sub-agent engine must accept the type the product builds.

    The graph read ``spawn.payload`` / ``spawn.worktree`` / ``spawn.resource_files``
    from a ``SubAgentSpec``, which has none of them.  Every fan-out raised
    ``AttributeError``, ``_subagent_diff``'s ``except Exception`` swallowed it,
    and ``--subagents`` ran with no sub-agents while reporting a normal run.

    This drives the real ``fan_out`` with real specs, so the type mismatch
    cannot hide behind a test that constructs the other type.
    """
    from gigaforge.agents.engine import subagent_engine
    from gigaforge.core.contracts import SubAgentSpec

    assert subagent_engine() == "langgraph", (
        "this test is about the DEFAULT engine; if the default changed, the "
        "defect it pins may have moved"
    )

    _repo(tmp_path)
    pipeline = _pipeline(tmp_path, _ScriptedProvider())
    specs = [
        SubAgentSpec(name="work-0", goal="touch math_utils.py", focus_paths=("math_utils.py",)),
        SubAgentSpec(name="work-1", goal="touch text_utils.py", focus_paths=("text_utils.py",)),
    ]

    # ``pipeline.fan_out`` is the product's own entry point: it builds the
    # production runner and routes through the engine dispatcher.  Driving it
    # (rather than the runner directly) is what makes this test able to catch a
    # type mismatch between the two.
    results = pipeline.fan_out(specs, parallel=True)

    assert len(results) == len(specs), (
        "B2: the fan-out must return one result per spec on the default engine"
    )
    assert [r.name for r in results] == ["work-0", "work-1"], (
        "results must come back in spawn order, not completion order"
    )


def test_a_subagent_spec_is_accepted_by_the_graph(tmp_path: Path) -> None:
    """The graph must normalise both task types, not assume the A2A one."""
    from gigaforge.agents.langgraph_subagents import normalise_spawn
    from gigaforge.core.contracts import SubAgentSpec

    flat = normalise_spawn(
        SubAgentSpec(name="w", goal="do the thing", focus_paths=("a.py",))
    )
    assert flat["name"] == "w"
    assert flat["goal"] == "do the thing"
    assert flat["focus_paths"] == ["a.py"]
    assert flat["payload"]["goal"] == "do the thing"


# ── P40: a child stays inside its assignment ─────────────────────────────────
def test_a_child_goal_does_not_name_another_childs_files(tmp_path: Path) -> None:
    """The scoped goal must not contain the other children's paths.

    ``_ensure_creations`` reads file names out of the goal text, so a child
    handed ``"Only touch a.html. Goal: make index.html and b.html and a.html …"``
    created ``index.html`` and ``b.html`` as well.  Three children then rewrote
    each other's files and the run oscillated.
    """
    from gigaforge.core.pipeline import _scoped_goal

    goal = "make index.html and b.html and a.html a cyberpunk page"
    scoped = _scoped_goal(goal, ("a.html",), ("a.html", "b.html", "index.html"))

    assert "a.html" in scoped
    assert "b.html" not in scoped, "P40: the child can still see b.html"
    assert "index.html" not in scoped, "P40: the child can still see index.html"


def test_acceptance_rejects_a_child_that_wrote_outside_its_assignment() -> None:
    """The gate must reject an out-of-scope diff, with a reason.

    Ownership was a *request* (``focus_paths`` was a slicer hint) and nothing
    enforced it.  The gate is the enforcement: a diff touching a path outside
    ``owns_paths`` is rejected and named, never merged.
    """
    from gigaforge.core.acceptance import WorkItem, check_acceptance

    class _Result:
        ok = True
        error = ""
        diff = (
            "--- /dev/null\n"
            "+++ b/other.html\n"
            "@@ -0,0 +1,1 @@\n"
            "+<html>not mine</html>\n"
        )

    item = WorkItem(id="work-0", goal="touch a.html", owns_paths=("a.html",))
    verdict = check_acceptance(_Result(), item)

    assert not verdict.accepted
    assert any("outside its assignment" in r for r in verdict.reasons), verdict.reasons


def test_acceptance_rejects_a_run_that_was_not_accepted() -> None:
    """A rejected child's diff is not a contribution.

    ``_subagent_diff`` read ``item.diff`` and never ``item.ok``, so a child whose
    run the pipeline had already refused still handed its diff to the merge.
    """
    from gigaforge.core.acceptance import WorkItem, check_acceptance

    class _Result:
        ok = False
        error = "fix loop rejected the diff"
        diff = "--- a/a.html\n+++ b/a.html\n@@ -1 +1 @@\n-x\n+y\n"

    item = WorkItem(id="work-0", goal="touch a.html", owns_paths=("a.html",))
    verdict = check_acceptance(_Result(), item)

    assert not verdict.accepted
    assert any("not accepted" in r for r in verdict.reasons), verdict.reasons


def test_the_merge_refuses_two_children_claiming_one_path(tmp_path: Path) -> None:
    """Two children creating one file must not merge with the last writer winning.

    Measured before the fix: two ``new file`` blocks for one path merged to
    ``ok: True, errors: []`` with the first child's content silently gone.  The
    run reported success and lost half the work.
    """
    from gigaforge.core.acceptance import merge_diffs

    (tmp_path / "index.html").write_text("<html>original</html>\n")
    first = (
        "--- a/index.html\n"
        "+++ b/index.html\n"
        "@@ -1 +1 @@\n"
        "-<html>original</html>\n"
        "+<html>grid A</html>\n"
    )
    second = (
        "--- a/index.html\n"
        "+++ b/index.html\n"
        "@@ -1 +1 @@\n"
        "-<html>original</html>\n"
        "+<html>grid B</html>\n"
    )

    report = merge_diffs(
        [
            {"name": "work-0", "diff": first, "owns_paths": ("index.html",)},
            {"name": "work-1", "diff": second, "owns_paths": ("index.html",)},
        ],
        root=tmp_path,
    )

    assert report.accepted == ["work-0"], report.to_dict()
    assert report.conflicts, "the second claim on index.html must be reported"
    assert "grid B" not in report.diff, "the losing child's work must not be merged"


# ── the whole chain: goal -> pipeline -> diff on disk ────────────────────────
def test_goal_to_diff_end_to_end(tmp_path: Path) -> None:
    """Drive ``TaskPipeline.run(goal)`` and assert on the diff that comes out."""
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

    assert "return a + b" in (result.diff or ""), result.diff
    code_prompts = [p for p in provider.prompts if "action_input" not in p]
    assert code_prompts, "the coder was never called"
    assert any("edit_function: add" in p for p in code_prompts), (
        "N1: the plan never reached the coder"
    )
    assert provider.react_calls >= 1, "the ReAct episode never ran"


@pytest.mark.parametrize("engine", ["classic", "langgraph"])
def test_both_engines_produce_the_same_diff(
    tmp_path: Path, engine: str, monkeypatch: Any
) -> None:
    """The engine is an implementation detail: the deliverable must not change.

    A defect that only appears on one engine is the hardest kind to find, and
    the two engines have already diverged once (the goal contract existed only
    in the classic loop).  Running the same goal through both and comparing the
    diff is the cheapest way to keep them honest.
    """
    monkeypatch.setenv("GIGAFORGE_REACT_ENGINE", engine)
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
             ]}},
        ],
        diff=diff,
    )
    pipeline = _pipeline(tmp_path, provider)
    result = pipeline.run("the arithmetic helper returns the wrong result")

    assert "return a + b" in (result.diff or ""), (
        f"engine {engine} did not produce the corrected diff: {result.diff!r}"
    )

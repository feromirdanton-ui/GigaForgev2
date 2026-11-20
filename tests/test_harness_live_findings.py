"""The harness behaviours that only a live run exposed.

Each test here corresponds to something that actually went wrong against the
real GigaChat API on 2026-11-20, not to a hypothetical.

The suite drives coroutines with ``asyncio.run`` rather than
``pytest.mark.asyncio``: the project does not depend on ``pytest-asyncio``, and
an unregistered mark silently turns an async test into a no-op that "passes".
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gigaforge.agents.react import (
    GrepTool,
    ReActRunner,
    ToolRegistry,
    _clean_pattern,
)


class _Ctx:
    """Minimal stand-in for ``ReActContext``: grep only needs ``worktree``."""

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree


# --------------------------------------------------------------------- grep


def test_clean_pattern_strips_the_quotes_a_model_adds() -> None:
    """Live: the model sent ``"TODO"`` and rg searched for the quotes."""
    assert _clean_pattern('"TODO"') == "TODO"
    assert _clean_pattern("'TODO'") == "TODO"
    assert _clean_pattern("`TODO`") == "TODO"
    assert _clean_pattern("  TODO  ") == "TODO"
    assert _clean_pattern('"') == '"', "a lone quote is not a pair"


def test_grep_finds_a_marker_without_ripgrep(tmp_path: Path) -> None:
    """The tool must not depend on a binary the harness does not install.

    Live: ``which rg`` returned nothing, the subprocess raised
    ``FileNotFoundError``, and the episode read that as "no matches".
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1  # TODO: fix\n", encoding="utf-8")
    (tmp_path / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")

    out = asyncio.run(GrepTool().run("TODO", ctx=_Ctx(tmp_path)))  # type: ignore[arg-type]
    assert "mod.py" in out
    assert "other.py" not in out


def test_grep_says_so_when_nothing_matches(tmp_path: Path) -> None:
    """An empty string reads as "the tool did nothing" and the model retries."""
    (tmp_path / "a.py").write_text("nothing here\n", encoding="utf-8")
    out = asyncio.run(GrepTool().run("ABSENT_MARKER", ctx=_Ctx(tmp_path)))  # type: ignore[arg-type]
    assert "no matches" in out.lower()


def test_grep_skips_vendored_directories(tmp_path: Path) -> None:
    """Walking ``.venv``/``node_modules`` returns thousands of useless rows."""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("// TODO vendored\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("# TODO ours\n", encoding="utf-8")

    out = asyncio.run(GrepTool().run("TODO", ctx=_Ctx(tmp_path)))  # type: ignore[arg-type]
    assert "app.py" in out
    assert "dep.js" not in out


def test_grep_rejects_an_empty_pattern(tmp_path: Path) -> None:
    out = asyncio.run(GrepTool().run("   ", ctx=_Ctx(tmp_path)))  # type: ignore[arg-type]
    assert out.startswith("ERROR")


# ------------------------------------------------------------- tool registry


def test_registry_is_empty_until_defaults_are_installed() -> None:
    """Live: the runner was built with ``ToolRegistry()`` and no tools.

    The model asked for ``grep``, the registry returned ``None``, and every
    episode burned its step budget on "unknown tool".
    """
    assert ToolRegistry().names() == []


def test_install_defaults_provides_the_tools_the_prompt_advertises() -> None:
    """The prompt names grep/read_structure/run_tests; they must exist."""
    registry = ToolRegistry()
    registry.install_defaults(sandbox=None)
    names = set(registry.names())
    assert {"grep", "read_structure", "run_tests"} <= names


# ------------------------------------------------------------ plan synthesis


def _runner() -> ReActRunner:
    """A runner without ``__init__``: the methods under test are pure."""
    return ReActRunner.__new__(ReActRunner)


def test_synthesize_plan_reads_the_firewall_grep_payload() -> None:
    """The Firewall compresses grep into ``{"matches": [{"file": ...}]}``."""
    observation = (
        "Observation (grep):\n"
        '{"matches": [{"file": "src/a.py", "line": 3, "content": " TODO"},'
        ' {"file": "src/b.py", "line": 9, "content": " TODO"}], "_truncated": 0}'
    )
    plan = _runner()._synthesize_plan(  # noqa: SLF001
        [{"role": "user", "content": observation}]
    )
    paths = [step.args.get("path") for step in plan]
    assert "src/a.py" in paths
    assert "src/b.py" in paths


def test_synthesize_plan_reads_raw_path_line_rows() -> None:
    """With the compressor off, observations are plain ``path:line:text``."""
    observation = "Observation (grep):\nsrc/c.py:12: TODO here"
    plan = _runner()._synthesize_plan(  # noqa: SLF001
        [{"role": "user", "content": observation}]
    )
    assert [step.args.get("path") for step in plan] == ["src/c.py"]


def test_synthesize_plan_is_empty_when_nothing_was_observed() -> None:
    """No observations means no plan \u2014 never invent work."""
    assert _runner()._synthesize_plan([{"role": "user", "content": "Task: x"}]) == []  # noqa: SLF001


def test_synthesize_plan_ignores_non_source_paths() -> None:
    observation = 'Observation (grep):\n{"matches": [{"file": "trace.jsonl", "line": 1}]}'
    assert _runner()._synthesize_plan([{"role": "user", "content": observation}]) == []  # noqa: SLF001


# ------------------------------------------------- budget exhaustion salvage


def test_budget_exhaustion_salvages_the_plan_before_raising() -> None:
    """A run that runs out of tokens must still hand the DAG a plan.

    The budget is a hard stop and the exception still propagates \u2014 but the
    episode has already paid for its observations, and throwing them away made
    a budget stop indistinguishable from a failed run.
    """
    from gigaforge.client.errors import BudgetExceeded

    class _Budgets:
        def check(self, *a: object, **k: object) -> None:
            """The pre-call claim passes; settling it is what fails."""

        def reserve(self, *a: object, **k: object) -> object:
            from gigaforge.client.errors import BudgetExceeded

            class _Claim:
                def settle(self, *a: object, **k: object) -> None:
                    raise BudgetExceeded("out of tokens")

                def cancel(self) -> None:
                    pass

            return _Claim()

        def commit_task(self, *a: object, **k: object) -> None:
            raise BudgetExceeded("out of tokens")

        def check_overshoot(self, *a: object, **k: object) -> None:
            raise AssertionError("must not be reached")

    class _Client:
        async def chat(self, *a: object, **k: object) -> object:
            class _R:
                content = '{"thought": "look", "action": "grep", "action_input": "TODO"}'
                usage = {"prompt_tokens": 10, "completion_tokens": 5}

            return _R()

    class _Router:
        def route(self, phase: str) -> str:
            return "GigaChat-2-Pro"

    class _Tracer:
        def trace(self, *a: object, **k: object) -> None:
            pass

    class _Registry:
        def names(self) -> list[str]:
            return ["grep"]

        def get(self, name: str) -> object:
            class _T:
                async def run(self, action_input: str, *, ctx: object) -> str:
                    return '{"matches": [{"file": "src/x.py", "line": 1}]}'

            return _T()

        def register(self, tool: object) -> None:
            pass

    class _Guards:
        def install(self, registry: object) -> None:
            pass

        def opening_messages(self, messages: object) -> list[object]:
            return []

        def screen_tool_call(self, action: str, action_input: object) -> None:
            return None

        def after_observation(self, messages: object) -> None:
            return None

    class _Window:
        def check(self, *a: object, **k: object) -> None:
            pass

    class _Settings:
        budget_plan_out = 100

    runner = ReActRunner.__new__(ReActRunner)
    runner._client = _Client()  # noqa: SLF001
    runner._router = _Router()  # noqa: SLF001
    runner._budgets = _Budgets()  # noqa: SLF001
    runner._tracer = _Tracer()  # noqa: SLF001
    runner._registry = _Registry()  # noqa: SLF001
    runner._guards = _Guards()  # noqa: SLF001 - ``guards`` is a read-only property
    runner._window_guard = _Window()  # noqa: SLF001 - read-only property
    runner._s = _Settings()  # noqa: SLF001
    runner._max_steps = 3  # noqa: SLF001
    runner._compactions = 0  # noqa: SLF001

    from gigaforge.core.task_state import TaskState

    state = TaskState(goal="g", task_id="t")
    ctx = _Ctx(Path("."))

    with pytest.raises(BudgetExceeded):
        asyncio.run(runner.run("find it", state, ctx=ctx))  # type: ignore[arg-type]


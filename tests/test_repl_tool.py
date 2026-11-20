"""The REPL tool: the harness's own execution surface.

The point of these tests is not that Python can add — it is that the tool is
wired through the components that already own execution and compression, and
that it cleans up after itself.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from gigaforge.agents.repl_tool import REPL_TOOL_NAME, SCRATCH_DIR, ReplTool
from gigaforge.sandbox.executor import SandboxExecutor


class _Ctx:
    def __init__(self, root: Path) -> None:
        self.worktree = root
        self.repo_root = root
        self.sandbox = SandboxExecutor(worktree=root)


def _run(code: str, root: Path) -> str:
    return asyncio.run(ReplTool().run(code, ctx=_Ctx(root)))


# ── it runs ─────────────────────────────────────────────────────────────────
def test_a_program_runs_and_its_output_comes_back(tmp_path: Path) -> None:
    assert "4" in _run("print(2 + 2)", tmp_path)


def test_the_program_runs_in_the_worktree(tmp_path: Path) -> None:
    """A probe must see the repository it is reasoning about."""
    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    out = _run("print(open('marker.txt').read())", tmp_path)
    assert "hello" in out


def test_a_failing_program_is_an_observation_not_an_exception(tmp_path: Path) -> None:
    """A debugging step that raises must not become a harness error."""
    out = _run("raise ValueError('boom')", tmp_path)
    assert out.startswith("ERROR: repl exited")
    assert "ValueError" in out


def test_a_syntax_error_is_reported_with_its_traceback(tmp_path: Path) -> None:
    out = _run("def broken(:\n    pass", tmp_path)
    assert "ERROR: repl exited" in out
    assert "SyntaxError" in out


# ── it is bounded ───────────────────────────────────────────────────────────
def test_an_empty_program_is_refused(tmp_path: Path) -> None:
    assert _run("   ", tmp_path).startswith("ERROR: repl needs a program")


def test_an_oversized_program_is_refused(tmp_path: Path) -> None:
    out = _run("x = 1\n" * 5000, tmp_path)
    assert "over the" in out and "limit" in out


def test_a_missing_sandbox_is_reported_not_crashed(tmp_path: Path) -> None:
    ctx = type("Ctx", (), {"worktree": tmp_path, "repo_root": tmp_path, "sandbox": None})()
    out = asyncio.run(ReplTool().run("print(1)", ctx=ctx))
    assert out.startswith("ERROR: repl needs a sandbox")


# ── it cleans up ────────────────────────────────────────────────────────────
def test_the_scratch_directory_is_removed_after_a_success(tmp_path: Path) -> None:
    """The agent's own scratch code must not pollute the tree it greps."""
    _run("print('ok')", tmp_path)
    assert not (tmp_path / SCRATCH_DIR).exists()


def test_the_scratch_directory_is_removed_after_a_failure(tmp_path: Path) -> None:
    _run("raise SystemExit(3)", tmp_path)
    assert not (tmp_path / SCRATCH_DIR).exists()


# ── it is wired into the registry ───────────────────────────────────────────
def test_the_registry_installs_the_repl_tool(tmp_path: Path) -> None:
    from gigaforge.agents.react import ToolRegistry

    registry = ToolRegistry()
    registry.install_defaults(sandbox=SandboxExecutor(worktree=tmp_path))
    assert REPL_TOOL_NAME in registry.names()


def test_the_prompt_describes_the_repl_tool() -> None:
    """A tool the prompt does not describe is a tool the model will not use."""
    from gigaforge.agents.harness_prompt import describe_tools

    assert "`repl`" in describe_tools(["grep", "repl", "finish"])


def test_the_repl_output_is_compressed_by_the_firewall(tmp_path: Path) -> None:
    """A 5 000-line traceback must not reach the model whole."""
    from gigaforge.firewall import compress

    noisy = "\n".join(f"line {i}" for i in range(5000))
    result = compress("builderr", noisy)
    assert result.compressed_chars < len(noisy)

"""A scoped grep must search the file it was scoped to.

Measured on ``astropy__astropy-12907``: the model asked
``grep separability_matrix in astropy/modeling/separable.py``, the tool walked
the *file* with ``os.walk`` (which yields nothing for a file), and answered
"(no matches)".  The model concluded the function had been renamed, invented
line numbers, and produced a diff whose hunks could not anchor — the whole
instance was lost to one wrong answer from a tool.

These tests pin the behaviour, not the implementation: a pattern that is in the
file must be found when the search is scoped to that file.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gigaforge.agents.react import GrepTool


class _Ctx:
    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def separability_matrix(transform):\n"
        "    return transform\n",
        encoding="utf-8",
    )
    (pkg / "other.py").write_text("def unrelated():\n    pass\n", encoding="utf-8")
    return tmp_path


def _grep(tree: Path, action_input: str) -> str:
    return asyncio.run(GrepTool().run(action_input, ctx=_Ctx(tree)))


def test_scoped_to_a_file_finds_the_pattern(tree: Path) -> None:
    out = _grep(tree, "separability_matrix in pkg/mod.py")
    assert "no matches" not in out, out
    assert "separability_matrix" in out
    # The line number is what the model needs to write an anchorable hunk.
    assert ":4:" in out, out


def test_scoped_to_a_file_does_not_leak_other_files(tree: Path) -> None:
    out = _grep(tree, "unrelated in pkg/mod.py")
    assert "no matches" in out, out


def test_scoped_to_a_directory_still_walks(tree: Path) -> None:
    out = _grep(tree, "unrelated in pkg")
    assert "other.py" in out, out


def test_unscoped_search_walks_the_worktree(tree: Path) -> None:
    out = _grep(tree, "separability_matrix")
    assert "mod.py" in out, out


def test_missing_scope_is_an_error_not_an_empty_result(tree: Path) -> None:
    out = _grep(tree, "anything in pkg/nope.py")
    assert out.startswith("ERROR:"), out

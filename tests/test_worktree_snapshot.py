"""A worktree must be a checkout of the tree as it is NOW, not of HEAD.

The pipeline applies a diff to the working tree and never commits.  A worktree
based on ``HEAD`` therefore hands a second cycle's sub-agents the state from
*before* the run started: they re-derive the original bug, and their diffs are
applied on top of the first cycle's accepted work.

Measured live, on a two-file bug-fix goal with ``--subagents``:

    cycle 1  accepted  files ['calc.py', 'math_utils.py']   <- both fixed
    cycle 2  accepted  files ['math_utils.py']              <- rewritten broken
    cycle 3  accepted  files ['calc.py']                    <- rewritten broken

and the run ended with the bug it started with.  The harness undid its own
accepted work, one file per cycle.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gigaforge.core.agents import WorktreeManager


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "a@b.c")
    _git(root, "config", "user.name", "a")
    (root / "calc.py").write_text("def mul(a, b):\n    return a + b\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def test_worktree_sees_an_uncommitted_edit(repo: Path):
    """The defect: the edit is on disk, and the worktree must contain it."""
    (repo / "calc.py").write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
    manager = WorktreeManager(str(repo))
    worktree = manager.create("w0")
    try:
        content = (Path(worktree.path) / "calc.py").read_text(encoding="utf-8")
        assert "a * b" in content, (
            "the worktree was based on HEAD and lost the uncommitted fix"
        )
    finally:
        manager.remove("w0")


def test_worktree_sees_an_untracked_file_created_by_a_previous_cycle(repo: Path):
    """``stash create`` covers tracked edits only; a new file is untracked."""
    (repo / "index.html").write_text("<h1>new</h1>", encoding="utf-8")
    manager = WorktreeManager(str(repo))
    worktree = manager.create("w0")
    try:
        assert (Path(worktree.path) / "index.html").is_file()
    finally:
        manager.remove("w0")


def test_snapshot_does_not_disturb_the_working_tree(repo: Path):
    """``stash create`` must not stash, reset or otherwise touch the tree."""
    (repo / "calc.py").write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
    manager = WorktreeManager(str(repo))
    manager.snapshot()
    assert "a * b" in (repo / "calc.py").read_text(encoding="utf-8")
    assert _git(repo, "stash", "list").strip() == "", "the stash list must stay empty"
    assert _git(repo, "status", "--porcelain").strip(), "the edit must still be pending"


def test_a_clean_tree_still_produces_a_worktree(repo: Path):
    """A clean tree has nothing to snapshot; HEAD is then the right base."""
    manager = WorktreeManager(str(repo))
    worktree = manager.create("w0")
    try:
        assert (Path(worktree.path) / "calc.py").is_file()
    finally:
        manager.remove("w0")

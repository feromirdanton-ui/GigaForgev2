"""The episode's own writes must reach the pipeline as a diff.

The defect: the exploration episode's ``write_file`` tool edits the worktree
directly, but the pipeline's coder is asked to *produce a diff*.  A file the
episode already wrote was therefore invisible to the rest of the run.  Measured
live: a 4 KB page was written, the coder returned nothing, the fix loop rejected
three times with "diff could not be parsed", and the run settled
``accepted: false, files: 0`` with the file sitting on disk.

The harness computes the diff — that is a computation, not a judgement call.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gigaforge.core.pipeline import TaskPipeline


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("# scratch\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


class _Cfg:
    def __init__(self, root: Path) -> None:
        self.repo_root = str(root)


class _Ctx:
    def __init__(self, root: Path, written: list[str]) -> None:
        self.run_id = "test-run"
        self.extras = {"episode_written_files": written}
        self.state = None


def _pipeline(root: Path) -> TaskPipeline:
    p = TaskPipeline.__new__(TaskPipeline)
    p.config = _Cfg(root)
    p._event = lambda *a, **k: None  # type: ignore[method-assign]
    return p


def test_a_new_file_becomes_a_creation_diff(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "index.html").write_text("<html><body>hi</body></html>\n")
    diff = _pipeline(root)._episode_diff(_Ctx(root, ["index.html"]))
    assert "+++ b/index.html" in diff
    assert "--- /dev/null" in diff
    assert "+<html><body>hi</body></html>" in diff


def test_a_modified_tracked_file_becomes_a_modification_diff(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "README.md").write_text("# scratch\nmore\n")
    diff = _pipeline(root)._episode_diff(_Ctx(root, ["README.md"]))
    assert "+++ b/README.md" in diff
    assert "+more" in diff


def test_no_writes_means_no_diff(tmp_path: Path) -> None:
    """A run that never used the tool must be byte-identical to before."""
    root = _repo(tmp_path)
    assert _pipeline(root)._episode_diff(_Ctx(root, [])) == ""


def test_a_missing_file_is_skipped_not_crashed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert _pipeline(root)._episode_diff(_Ctx(root, ["nope.html"])) == ""


def test_a_non_git_worktree_yields_no_diff(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.html").write_text("x")
    assert _pipeline(plain)._episode_diff(_Ctx(plain, ["a.html"])) == ""


def test_the_diff_applies_cleanly(tmp_path: Path) -> None:
    """The point of the diff is that the rest of the pipeline can apply it."""
    root = _repo(tmp_path)
    (root / "index.html").write_text("<html>neon</html>\n")
    diff = _pipeline(root)._episode_diff(_Ctx(root, ["index.html"]))
    patch = tmp_path / "p.diff"
    patch.write_text(diff)
    # Reset the worktree, then apply the computed diff to prove it is real.
    (root / "index.html").unlink()
    subprocess.run(["git", "apply", str(patch)], cwd=root, check=True)
    assert (root / "index.html").read_text() == "<html>neon</html>\n"

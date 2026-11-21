"""The routing answer must not depend on WHERE the pipeline runs.

**The defect, measured live.**  A goal loop does not run against the user's
project: it runs against a copy of it,
``<project>/.gigaforge/workspaces/<goal>/project``, and the copy excludes
``.git`` on purpose — a nested repository would make every git command in the
run answer about the wrong tree.

So ``git -C <copy> rev-parse --show-toplevel`` walked *up* to the user's
repository, and the copy's own path
(``.gigaforge/workspaces/.../project/index.html``) is not in that repository's
HEAD.  ``_tracked_in_head`` therefore answered "not committed" for a file that
was committed, and the chain ran:

    "Update index.html to add a pricing section"
      -> artifact (create), not artifact_edit
      -> the existing page was never read
      -> the run regenerated it from scratch
      -> the hero and the grid the user already had were gone

The same class of bug hides in the pathspec: ``git -C <dir> ls-tree -- <path>``
interprets ``<path>`` relative to ``<dir>``, not to the repository root, so even
a correct toplevel gives the wrong answer from a subdirectory.

These tests build the real shape — a repository, a copy inside it, a manifest
that records where the copy came from — and assert the verdict is the same from
both places.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gigaforge.harness.task_kind import _repo_anchor, _tracked_in_head, classify_task

PAGE = "<!DOCTYPE html><html><body><h1>GigaForge</h1></body></html>"


def _git(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def _commit(root: Path, message: str = "c") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A repository with a committed page."""
    _git(tmp_path)
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    _commit(tmp_path, "page")
    return tmp_path


@pytest.fixture()
def copy_of(project: Path) -> Path:
    """The goal workspace's copy: inside the repo, without ``.git``."""
    state = project / ".gigaforge" / "workspaces" / "goal-1"
    root = state / "project"
    root.mkdir(parents=True)
    (root / "index.html").write_text(PAGE, encoding="utf-8")
    (state / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "goal_id": "goal-1",
                "destination": str(project),
                "workspace": str(root),
            }
        ),
        encoding="utf-8",
    )
    return root


class TestTheAnchor:
    def test_a_plain_repository_anchors_on_itself(self, project: Path) -> None:
        assert _repo_anchor(project) == (project.resolve(), project.resolve())

    def test_a_copy_anchors_on_its_destination(self, project: Path, copy_of: Path) -> None:
        """The copy has no ``.git``; its manifest says where it came from."""
        assert _repo_anchor(copy_of) == (project.resolve(), project.resolve())

    def test_a_directory_with_no_repository_has_no_anchor(self, tmp_path: Path) -> None:
        loose = tmp_path / "loose"
        loose.mkdir()
        assert _repo_anchor(loose) is None


class TestTheVerdictIsTheSameFromBothPlaces:
    def test_a_committed_page_is_tracked_from_the_project(self, project: Path) -> None:
        assert _tracked_in_head(project, "index.html") is True

    def test_a_committed_page_is_tracked_from_the_copy(
        self, project: Path, copy_of: Path
    ) -> None:
        """This is the assertion the live defect failed."""
        assert _tracked_in_head(copy_of, "index.html") is True

    def test_an_absent_page_is_untracked_from_the_copy(
        self, project: Path, copy_of: Path
    ) -> None:
        assert _tracked_in_head(copy_of, "about.html") is False

    def test_the_routing_verdict_matches(self, project: Path, copy_of: Path) -> None:
        goal = "Update index.html to add a pricing section"
        from_project = classify_task(goal, project)
        from_copy = classify_task(goal, copy_of)
        assert from_project.kind == "artifact_edit"
        assert from_copy.kind == from_project.kind, (
            "the copy must not change the verdict: "
            f"{from_copy.kind} != {from_project.kind}"
        )

    def test_a_subdirectory_pathspec_is_resolved_against_the_toplevel(
        self, project: Path
    ) -> None:
        """``git -C <dir> ls-tree -- <path>`` is relative to ``<dir>``.

        A page committed at ``pages/about.html`` must be found when the question
        is asked from ``pages/`` with the path ``about.html``.
        """
        (project / "pages").mkdir()
        (project / "pages" / "about.html").write_text(PAGE, encoding="utf-8")
        _commit(project, "about")
        assert _tracked_in_head(project / "pages", "about.html") is True

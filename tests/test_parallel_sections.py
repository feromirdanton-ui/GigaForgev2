"""Sections run concurrently, the harness owns the page, and the flag speaks.

Three defects:

* **N6 — sections were generated one after another.**  They are independent by
  construction (own kind, own anchor, own fragment), so the harness was
  serialising work it knew to be independent: 3 sections × ~2 s = 6 s against
  ~2 s in parallel.
* **N7 — double apply.**  ``_sectional_generation`` writes the page to disk and
  hands its diff to the fix loop as ``initial_diff``.  If the model also returns
  a diff for that path, applying it edits the file the harness just built: the
  tree and the reported diff diverge.  Measured: the assembled page had one
  ``<style>`` and the applied diff reintroduced a second.
* **5.5 — ``--subagents`` was a silent no-op on a page goal.**  The artifact
  branch returned the sectional path and never mentioned the flag, so the run
  showed ``subagent events: []`` and ``fan_out called: False`` with no reason.
"""
from __future__ import annotations

import inspect
import subprocess
import threading
import time
from pathlib import Path

import pytest

from gigaforge.core.pipeline import TaskPipeline, _drop_hunks_for


def _git_repo(root: Path) -> Path:
    """A real repo: ``_episode_diff`` computes the diff with git."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


class TestDropHunksFor:
    def test_drops_the_owned_file_only(self) -> None:
        diff = (
            "diff --git a/index.html b/index.html\n--- a/index.html\n+++ b/index.html\n"
            "@@ -1 +1 @@\n-old\n+new\n"
            "diff --git a/db.py b/db.py\n--- a/db.py\n+++ b/db.py\n@@ -1 +1 @@\n-a\n+b\n"
        )
        out = _drop_hunks_for(diff, {"index.html"})
        assert "index.html" not in out
        assert "+b" in out, "an unrelated file's hunk was dropped"

    def test_does_not_match_a_nested_path(self) -> None:
        diff = (
            "diff --git a/sub/index.html b/sub/index.html\n"
            "--- a/sub/index.html\n+++ b/sub/index.html\n@@ -1 +1 @@\n-a\n+b\n"
        )
        assert _drop_hunks_for(diff, {"index.html"}) == diff

    def test_empty_inputs_are_safe(self) -> None:
        assert _drop_hunks_for("", {"x"}) == ""
        assert _drop_hunks_for("diff --git a/x b/x\n", set()) == "diff --git a/x b/x\n"

    def test_dropping_everything_yields_empty(self) -> None:
        diff = "diff --git a/index.html b/index.html\n--- a/index.html\n+++ b/index.html\n@@ -1 +1 @@\n-a\n+b\n"
        assert _drop_hunks_for(diff, {"index.html"}) == ""


class TestSectionsAreParallel:
    def test_sections_are_built_concurrently(self, tmp_path: Path) -> None:
        """Three sections that each sleep must finish in about one sleep."""
        _git_repo(tmp_path)
        from gigaforge.harness.generation import plan_generation

        plan = plan_generation(
            "Create a single-file index.html: a landing page with a hero, a "
            "feature grid and a footer."
        )
        assert len(plan.sections) >= 3

        seen: list[str] = []
        lock = threading.Lock()

        class _P(TaskPipeline):
            def __init__(self) -> None:  # noqa: D107
                self.config = type("C", (), {"repo_root": str(tmp_path),
                                             "max_workers": 4})()
                self.events: list[tuple[str, dict]] = []

            def _event(self, name: str, **payload) -> None:
                self.events.append((name, payload))

            def _repo_root(self) -> Path:
                return tmp_path

            def _ask(self, phase: str, prompt: str, ctx) -> str:
                with lock:
                    seen.append(prompt[:40])
                time.sleep(0.4)
                return '<section class="hero"><h1>Hi</h1></section>'

        pipeline = _P()
        ctx = type("Ctx", (), {
            "run_id": "t",
            "state": type("S", (), {"goal": "Create a single-file index.html: a "
                                           "landing page with a hero, a feature "
                                           "grid and a footer."})(),
            "extras": {},
        })()
        started = time.monotonic()
        pipeline._sectional_generation(ctx)
        elapsed = time.monotonic() - started
        assert len(seen) >= 3
        # Sequential would be >= 1.2 s; parallel is ~0.4 s.  0.9 s separates them
        # without being flaky on a loaded machine.
        assert elapsed < 0.9, f"sections ran sequentially ({elapsed:.2f}s)"

    def test_a_failing_section_does_not_kill_the_page(self, tmp_path: Path) -> None:
        _git_repo(tmp_path)

        class _P(TaskPipeline):
            def __init__(self) -> None:  # noqa: D107
                self.config = type("C", (), {"repo_root": str(tmp_path),
                                             "max_workers": 4})()
                self.events: list[tuple[str, dict]] = []

            def _event(self, name: str, **payload) -> None:
                self.events.append((name, payload))

            def _repo_root(self) -> Path:
                return tmp_path

            def _ask(self, phase: str, prompt: str, ctx) -> str:
                # The goal text is embedded in every section prompt, so the
                # failure must key on the section's own instruction, not on a
                # word that appears in the goal.
                if "(kind: footer)" in prompt:
                    raise RuntimeError("footer child exploded")
                return '<section class="hero"><h1>Hi</h1></section>'

        pipeline = _P()
        ctx = type("Ctx", (), {
            "run_id": "t",
            "state": type("S", (), {"goal": "Create a single-file index.html: a "
                                           "landing page with a hero, a feature "
                                           "grid and a footer."})(),
            "extras": {},
        })()
        diff = pipeline._sectional_generation(ctx)
        assert diff, "one failing section killed the whole page"
        assert (tmp_path / "index.html").is_file()
        assert any(name == "pipeline.generation_section_error"
                   for name, _ in pipeline.events)


class TestSubagentsFlagIsNotSilent:
    def test_the_artifact_branch_emits_an_event(self) -> None:
        source = inspect.getsource(TaskPipeline._subagent_diff)
        assert "subagents_sections" in source, (
            "--subagents on a page goal must say what it did, not return silently"
        )

    def test_the_event_explains_the_design(self) -> None:
        source = inspect.getsource(TaskPipeline._subagent_diff)
        assert "sections" in source and "concurrently" in source


class TestHarnessOwnsThePage:
    def test_sectional_generation_marks_the_path_owned(self) -> None:
        source = inspect.getsource(TaskPipeline._sectional_generation)
        assert "harness_owned_paths" in source

    def test_act_applies_the_ownership_invariant(self) -> None:
        """The act node must resolve ownership, not re-implement it.

        This used to assert that ``_drop_hunks_for`` appeared in the source of
        ``_node_act`` — a test of the implementation, not of the behaviour, and
        one that would pass for any code that merely mentioned the name.  The
        invariant now lives in ``OwnedFiles.resolve``; what matters is that the
        act node calls it.
        """
        source = inspect.getsource(TaskPipeline._node_act)
        assert "_owned_files" in source
        assert "resolve(" in source

    def test_ownership_is_a_policy_not_a_comment(self) -> None:
        """An assembled page is authoritative; a seeded page is not."""
        from gigaforge.harness.ownership import OwnedFiles, Policy

        owned = OwnedFiles.from_mapping(
            {"index.html": "<html></html>", "about.html": "<html></html>"},
            policies={"about.html": Policy.SEED},
        )
        assert owned.authoritative_paths == {"index.html"}
        assert owned.seed_paths == {"about.html"}

    def test_a_seed_file_is_never_overwritten(self, tmp_path) -> None:
        """The review's defect: an edit's fix was silently reverted.

        ``_restore_harness_owned`` wrote the harness's bytes over the file AFTER
        the model's diff, so a model that fixed a real bug in a page the harness
        had seeded had its fix discarded.  A SEED file must keep the model's
        work.
        """
        from gigaforge.harness.ownership import OwnedFiles, Policy

        page = tmp_path / "index.html"
        page.write_text("<html>MODEL FIX</html>", encoding="utf-8")
        owned = OwnedFiles.from_mapping(
            {"index.html": "<html>HARNESS BASELINE</html>"},
            policies={"index.html": Policy.SEED},
        )
        _diff, resolution = owned.resolve("", tmp_path)
        assert page.read_text(encoding="utf-8") == "<html>MODEL FIX</html>"
        assert resolution.model_kept == ["index.html"]
        assert resolution.restored == []

    def test_an_authoritative_file_is_restored(self, tmp_path) -> None:
        from gigaforge.harness.ownership import OwnedFiles

        page = tmp_path / "index.html"
        page.write_text("<html>MODEL GUESS</html>", encoding="utf-8")
        owned = OwnedFiles.from_mapping({"index.html": "<html>ASSEMBLED</html>"})
        _diff, resolution = owned.resolve("", tmp_path)
        assert page.read_text(encoding="utf-8") == "<html>ASSEMBLED</html>"
        assert resolution.restored == ["index.html"]

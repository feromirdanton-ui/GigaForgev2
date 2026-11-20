"""The repair must be reachable, and the diff must match the tree.

Two defects, both measured:

* **N2 — the repair was unreachable.**  It sat at the top of
  ``_check_goal_contract``, after ``if not requirements: return None``.
  ``extract_requirements`` returns zero requirements for "Create a single-file
  index.html", "Build a landing page" and "Make a website for my bakery" — so
  for three of the four ordinary ways to ask for a page, the repair never ran
  and a broken page passed.
* **N8 — ``final_diff`` described the pre-repair file.**  The repair wrote the
  file on disk, but the diff had already been computed.  Measured: the tree had
  1 section where ``final_diff`` had 4.

The tests drive the real node with a real file on disk, because the defect was
about *reachability on the product path*, not about the repair function — that
one was already covered by 265 lines of tests and still never ran.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gigaforge.core.pipeline import TaskPipeline, _merge_repaired_diff

BROKEN_PAGE = (
    "<!DOCTYPE html>\n<html><head><style>body{color:#fff;background:#000}</style></head>\n"
    "<body>\n"
    "body{color:#fff;background:#000}\n"
    '<section class="hero"><h1>GigaForge</h1></section>\n'
    '<section class="features"><div class="feature-card"><h2>Plan</h2></div></section>\n'
    '<section class="features"><div class="feature-card"><h2>Plan</h2></div></section>\n'
    "</body></html>\n"
)


class _Validation:
    def __init__(self, files: list[str]) -> None:
        self.accepted = True
        self.changed_files = files


class _Ctx:
    def __init__(self, goal: str = "Create a single-file index.html") -> None:
        self.run_id = "test-run"
        self.state = type("S", (), {"goal": goal, "artifacts": {}, "add_note": lambda *a, **k: None})()
        self.extras: dict = {}


class _Stub(TaskPipeline):
    def __init__(self, root: Path) -> None:  # noqa: D107 - bypass the constructor
        self.config = type("C", (), {"repo_root": str(root)})()
        self.events: list[tuple[str, dict]] = []

    def _event(self, name: str, **payload) -> None:
        self.events.append((name, payload))

    def _repo_root(self) -> Path:
        return Path(self.config.repo_root)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


class TestRepairIsReachable:
    def test_repair_runs_for_a_goal_with_no_requirements(self, repo: Path) -> None:
        """'Create a single-file index.html' yields zero requirements — the old
        early return meant the repair never ran for it."""
        from gigaforge.harness.goal_contract import extract_requirements

        assert extract_requirements("Create a single-file index.html") == []

        (repo / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx("Create a single-file index.html")
        out = pipeline._node_repair(ctx, {"validation": _Validation(["index.html"])})

        assert out["repair_report"]["files"] == ["index.html"]
        assert out["repair_report"]["actions"], "the repair did nothing"
        page = (repo / "index.html").read_text(encoding="utf-8")
        assert page.count('class="features"') == 1, "the duplicate section survived"
        assert "body{color:#fff;background:#000}\n<section" not in page

    def test_repair_runs_for_a_bare_landing_page_goal(self, repo: Path) -> None:
        (repo / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx("Build a landing page")
        out = pipeline._node_repair(ctx, {"validation": _Validation(["index.html"])})
        assert out["repair_report"]["actions"]

    def test_repair_is_a_noop_on_a_clean_page(self, repo: Path) -> None:
        clean = (
            "<!DOCTYPE html><html><head><style>body{color:#fff}</style></head>"
            '<body><section class="hero"><h1>Hi</h1></section></body></html>'
        )
        (repo / "index.html").write_text(clean, encoding="utf-8")
        pipeline = _Stub(repo)
        out = pipeline._node_repair(_Ctx(), {"validation": _Validation(["index.html"])})
        assert out["repair_report"]["actions"] == []
        assert (repo / "index.html").read_text(encoding="utf-8") == clean

    def test_repair_ignores_non_html_files(self, repo: Path) -> None:
        (repo / "db.py").write_text("X = 1\n", encoding="utf-8")
        pipeline = _Stub(repo)
        out = pipeline._node_repair(_Ctx(), {"validation": _Validation(["db.py"])})
        assert out["repair_report"]["files"] == []

    def test_repair_node_is_wired_before_settle(self) -> None:
        import inspect

        source = inspect.getsource(TaskPipeline.build)
        assert 'name="repair"' in source
        assert source.index('name="repair"') < source.index('name="settle"')
        # settle must depend on it, or the DAG may run them in either order.
        assert '"repair_report"' in source


class TestDiffMatchesTheTree:
    def test_final_diff_is_recomputed_after_repair(self, repo: Path) -> None:
        (repo / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx()
        pipeline._node_repair(ctx, {"validation": _Validation(["index.html"])})
        recomputed = ctx.extras.get("repaired_diff") or ""
        assert recomputed, "the diff was not recomputed after the repair"
        # The recomputed diff must describe the repaired file, not the broken one.
        assert recomputed.count('class="features"') == 1

    def test_settle_uses_the_repaired_diff(self, repo: Path) -> None:
        (repo / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx()
        pipeline._node_repair(ctx, {"validation": _Validation(["index.html"])})
        stale = "diff --git a/index.html b/index.html\n--- a/index.html\n+++ b/index.html\n"
        out = pipeline._node_settle(ctx, {
            "validation": _Validation(["index.html"]),
            "proposal": type("P", (), {"diff": stale})(),
            "review_verdict": {},
        })
        assert out["final_diff"] != stale
        assert out["final_diff"].count('class="features"') == 1


class TestMergeRepairedDiff:
    def test_replaces_only_the_repaired_file(self) -> None:
        original = (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
            "diff --git a/index.html b/index.html\n--- a/index.html\n+++ b/index.html\n@@ -1 +1 @@\n-old\n"
        )
        repaired = (
            "diff --git a/index.html b/index.html\n--- a/index.html\n+++ b/index.html\n"
            "@@ -1 +1 @@\n+new\n"
        )
        merged = _merge_repaired_diff(original, repaired)
        assert "+y" in merged, "an unrelated file's hunk was lost"
        assert "+new" in merged
        assert "-old" not in merged

    def test_empty_repaired_keeps_the_original(self) -> None:
        assert _merge_repaired_diff("diff --git a/x b/x\n", "") == "diff --git a/x b/x\n"

    def test_empty_original_returns_the_repaired(self) -> None:
        assert _merge_repaired_diff("", "diff --git a/x b/x\n") == "diff --git a/x b/x\n"

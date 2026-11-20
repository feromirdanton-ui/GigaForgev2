"""Routing by fact, not by substring.

The defect these tests pin: ``is_page_goal`` was
``any(hint in low for hint in _PAGE_HINTS)`` with ``"site"`` and ``"html"`` in
the tuple.  ``"site" in "fix the composite index"`` is True, so a code goal was
routed to landing-page generation, the model's correct diff was discarded, and
``final_diff`` described a stray ``index.html``.

Every case below is a goal that the old implementation mis-routed.  They are
here as a group because the defect was a *class* — one substring, seven
different words that contain it — and a single negative example (the old test
suite had exactly one) cannot catch a class.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.harness.generation import is_page_goal
from gigaforge.harness.task_kind import classify_task

#: Goals that contain "site" or "html" as a *substring* of another word.  The
#: old implementation called every one of them a page goal.
SUBSTRING_TRAPS = [
    "Fix the composite index in db.py",
    "Add parasite resistance to enemy.py",
    "Refactor opposite() in math_utils.py",
    "Make the html parser faster",
    "Apply the site-wide config change",
    "The opposite of a composite key is a parasite index",
]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "db.py").write_text("INDEX = 1\n", encoding="utf-8")
    (tmp_path / "enemy.py").write_text("HP = 10\n", encoding="utf-8")
    (tmp_path / "math_utils.py").write_text("def opposite(x):\n    return -x\n", encoding="utf-8")
    (tmp_path / "parser.py").write_text("def parse():\n    pass\n", encoding="utf-8")
    (tmp_path / "config.py").write_text("SITE = 'x'\n", encoding="utf-8")
    return tmp_path


class TestSubstringTraps:
    @pytest.mark.parametrize("goal", SUBSTRING_TRAPS)
    def test_not_a_page_goal_with_a_repo(self, goal: str, repo: Path) -> None:
        assert not is_page_goal(goal, repo)

    @pytest.mark.parametrize("goal", SUBSTRING_TRAPS)
    def test_not_a_page_goal_without_a_repo(self, goal: str) -> None:
        # Without a tree the decision falls back to explicit page phrases, and
        # none of these contains one.  The conservative direction matters: a
        # wrong "artifact" verdict writes a stray page and discards real work.
        assert not is_page_goal(goal)

    def test_composite_goal_is_code(self, repo: Path) -> None:
        kind = classify_task("Fix the composite index in db.py", repo)
        assert kind.kind == "code"
        assert kind.code_paths == ("db.py",)
        assert "db.py" in kind.evidence


class TestArtifactDetection:
    def test_absent_html_path_is_an_artifact_goal(self, repo: Path) -> None:
        kind = classify_task("Create a single-file index.html about the harness", repo)
        assert kind.kind == "artifact"
        assert kind.artifact_paths == ("index.html",)

    def test_existing_html_path_is_an_edit_not_code(self, repo: Path) -> None:
        """An existing page is EDITED, not treated as a code change.

        This test used to assert ``code``, and that assertion was the P0 defect
        written down: routing an existing ``.html`` to the code path skipped
        ``_sectional_generation``, so there was no section plan, so
        ``requirements_from_plan`` never ran, so the contract fell back to two
        bare regex requirements and graded an edit more weakly than a creation.
        """
        (repo / "index.html").write_text("<html></html>", encoding="utf-8")
        kind = classify_task("Fix the broken footer in index.html", repo)
        assert kind.kind == "artifact_edit"
        assert kind.edit_paths == ("index.html",)
        assert kind.is_artifact and kind.is_edit
        assert "already exist" in kind.evidence

    def test_editing_a_page_keeps_the_artifact_workflow(self, repo: Path) -> None:
        """The three ordinary ways to ask for an edit all route to artifact_edit."""
        (repo / "index.html").write_text("<html></html>", encoding="utf-8")
        for goal in (
            "Update index.html to add a footer and a hero section",
            "Add a pricing section to index.html",
            "Redesign the landing page in index.html",
        ):
            kind = classify_task(goal, repo)
            assert kind.kind == "artifact_edit", f"{goal!r} -> {kind.kind}"
            assert kind.is_artifact, f"{goal!r} lost the artifact workflow"

    def test_page_phrase_without_a_path_is_an_artifact_goal(self, repo: Path) -> None:
        kind = classify_task("Build a website for my bakery", repo)
        assert kind.kind == "artifact"
        assert "website" in kind.evidence

    def test_landing_page_phrase_is_an_artifact_goal(self, repo: Path) -> None:
        assert classify_task("Build a landing page", repo).kind == "artifact"

    def test_both_kinds_is_mixed(self, repo: Path) -> None:
        kind = classify_task("Fix db.py and add a report.html", repo)
        assert kind.kind == "mixed"
        assert kind.artifact_paths == ("report.html",)
        assert kind.code_paths == ("db.py",)

    def test_absent_code_path_is_still_code(self, repo: Path) -> None:
        kind = classify_task("Create gigaforge/new_module.py with a helper", repo)
        assert kind.kind == "code"

    def test_no_path_no_phrase_defaults_to_code(self, repo: Path) -> None:
        kind = classify_task("Make the tests pass", repo)
        assert kind.kind == "code"
        assert "defaulting to code" in kind.evidence

    def test_evidence_is_always_present(self, repo: Path) -> None:
        for goal in SUBSTRING_TRAPS + ["Build a website", "Fix db.py"]:
            assert classify_task(goal, repo).evidence


class TestSiteGoalStillRoutes:
    """The real page goal must keep working — the fix must not break the case."""

    def test_cyberpunk_site_goal_is_an_artifact_goal(self, repo: Path) -> None:
        goal = (
            "Create a single-file index.html: a futuristic cyberpunk landing page "
            "about the GigaForge agent harness. Requirements: neon cyan and magenta "
            "palette, a hero section, a glitch effect on the title, a feature grid, "
            "and a footer. Self-contained, no external assets."
        )
        kind = classify_task(goal, repo)
        assert kind.kind == "artifact"
        assert kind.artifact_paths == ("index.html",)

    def test_site_goal_without_a_repo_still_routes(self) -> None:
        assert is_page_goal("Create a single-file index.html about the harness")


class TestTaskKindRecord:
    def test_to_dict_is_json_safe(self, repo: Path) -> None:
        import json

        payload = classify_task("Fix db.py and add report.html", repo).to_dict()
        assert json.loads(json.dumps(payload))["kind"] == "mixed"

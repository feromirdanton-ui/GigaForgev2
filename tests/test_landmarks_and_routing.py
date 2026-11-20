"""Two defects a live run found that the suite could not.

**1. Routing looked at the working tree, not the repository.**  A goal loop runs
several cycles against the same tree, and cycle 1 writes the page.  On cycle 2
the file exists on disk, so ``classify_task`` said "this path exists → code
work" and the run stopped generating sections.  Measured live: cycle 1 built a
page section by section; cycle 2 re-classified the goal as code and the model
rewrote the whole file in one answer, losing the grid and the palette.  The
question the routing asks is "is this path part of the repository the user gave
me", and the answer is the committed state.

**2. A page had two footers and the contract passed it.**  The page was
assembled from three sections and carried two ``<div class="footer">`` blocks —
the footer child's, and a second the model added while "finishing".  Nothing
caught it: ``duplicate_blocks`` compares *content* and the two footers had
different text, and the tag was ``div``, which is not a block tag at all.  A
page has one footer; the check has to be on the role.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gigaforge.harness.goal_contract import (
    Requirement,
    check_artifacts,
    requirements_from_plan,
)
from gigaforge.harness.generation import plan_generation
from gigaforge.harness.html_structure import (
    audit_html_structure,
    duplicate_landmarks,
    repair_html_structure,
)
from gigaforge.harness.task_kind import classify_task

GOAL = (
    "Create a single-file index.html: a futuristic, cutting-edge cyberpunk "
    "landing page about the GigaForge agent harness. Requirements: neon cyan "
    "and magenta palette, a hero section with a glitch effect on the title, a "
    "feature grid with four cards, and a footer. Self-contained, no external "
    "assets."
)


def _git(root: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


class TestRoutingIgnoresTheRunsOwnOutput:
    def test_a_page_written_by_cycle_one_is_still_an_artifact_goal(
        self, tmp_path: Path
    ) -> None:
        _git(tmp_path)
        # Cycle 1 wrote the page.  It is on disk but not committed.
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        kind = classify_task(GOAL, tmp_path)
        assert kind.kind == "artifact", (
            "the run's own output made the goal look like a code change"
        )
        assert kind.artifact_paths == ("index.html",)

    def test_a_committed_page_is_an_edit_goal(self, tmp_path: Path) -> None:
        """A committed page is still a page: the run is asked to change it.

        This used to assert ``code``.  The reasoning was "the file is there, so
        this is a change to a file" — but a change to a *page* is an artifact
        edit, and routing it to the code path graded it more weakly than a
        creation (no section plan, no plan-derived contract).
        """
        _git(tmp_path)
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "page"], cwd=tmp_path, check=True)
        kind = classify_task("Fix the broken footer in index.html", tmp_path)
        assert kind.kind == "artifact_edit"
        assert kind.edit_paths == ("index.html",)

    def test_a_non_git_tree_falls_back_to_the_working_tree(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
        kind = classify_task("Fix the broken footer in index.html", tmp_path)
        assert kind.kind == "artifact_edit"

    def test_the_composite_trap_still_holds(self, tmp_path: Path) -> None:
        _git(tmp_path)
        (tmp_path / "db.py").write_text("X = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "db"], cwd=tmp_path, check=True)
        assert classify_task("Fix the composite index in db.py", tmp_path).kind == "code"


TWO_FOOTERS = (
    "<!DOCTYPE html><html><head><style>body{color:#fff}</style></head><body>"
    '<section class="hero"><h1>GigaForge</h1></section>'
    '<div class="feature-grid">'
    '<div class="card"><p>One</p></div><div class="card"><p>Two</p></div>'
    '<div class="card"><p>Three</p></div></div>'
    '<div class="footer"><p>&copy; 2023 GigaForge Inc. All rights reserved.</p></div>'
    '<div class="footer">&copy; 2023 GigaForge Inc. Powered by technology.</div>'
    "</body></html>"
)


class TestDuplicateLandmarks:
    def test_two_footers_are_reported(self) -> None:
        assert duplicate_landmarks(TWO_FOOTERS) == [("footer", 2)]

    def test_one_footer_is_fine(self) -> None:
        page = TWO_FOOTERS.replace(
            '<div class="footer">&copy; 2023 GigaForge Inc. Powered by technology.</div>',
            "",
        )
        assert duplicate_landmarks(page) == []

    def test_a_footer_tag_and_a_footer_class_are_the_same_role(self) -> None:
        page = (
            "<html><body><footer><p>One</p></footer>"
            '<div class="footer"><p>Two</p></div></body></html>'
        )
        assert duplicate_landmarks(page) == [("footer", 2)]

    def test_the_audit_reports_it(self) -> None:
        report = audit_html_structure(TWO_FOOTERS)
        assert not report.ok
        assert any("footer" in issue for issue in report.issues())

    def test_the_repair_keeps_exactly_one(self) -> None:
        repaired, actions = repair_html_structure(TWO_FOOTERS)
        assert repaired.count('class="footer"') == 1
        assert any("duplicate footer" in a for a in actions)
        assert duplicate_landmarks(repaired) == []

    def test_the_repair_keeps_the_first_footer(self) -> None:
        """The assembler writes sections in plan order, so the first footer is
        the one the footer child produced."""
        repaired, _ = repair_html_structure(TWO_FOOTERS)
        assert "All rights reserved" in repaired
        assert "Powered by technology" not in repaired

    def test_the_repair_does_not_leave_a_dangling_close(self) -> None:
        repaired, _ = repair_html_structure(TWO_FOOTERS)
        assert repaired.count("<div") == repaired.count("</div>")

    def test_the_repair_does_not_touch_the_cards(self) -> None:
        repaired, _ = repair_html_structure(TWO_FOOTERS)
        assert repaired.count('class="card"') == 3

    def test_a_nested_footer_is_removed_whole(self) -> None:
        page = (
            "<html><body>"
            '<div class="footer"><div class="inner"><p>One</p></div></div>'
            '<div class="footer"><p>Two</p></div>'
            "</body></html>"
        )
        repaired, _ = repair_html_structure(page)
        assert repaired.count('class="footer"') == 1
        # The first footer is kept, and it is removed *whole* — its nested
        # ``<div class="inner">`` goes with it, leaving no dangling close.
        assert "One" in repaired
        assert "Two" not in repaired
        assert repaired.count("<div") == repaired.count("</div>")


class TestContractChecksLandmarks:
    def test_the_plan_asks_for_a_single_footer(self) -> None:
        plan = plan_generation(GOAL)
        reqs = requirements_from_plan(plan)
        assert any(r.kind == "singleton" and "footer" in r.label for r in reqs)

    def test_two_footers_fail_the_contract(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text(TWO_FOOTERS, encoding="utf-8")
        plan = plan_generation(GOAL)
        verdict = check_artifacts(requirements_from_plan(plan), tmp_path,
                                  files=["index.html"])
        assert not verdict.ok
        assert any("footer" in u for u in verdict.unmet)

    def test_one_footer_passes_the_singleton_check(self, tmp_path: Path) -> None:
        page = TWO_FOOTERS.replace(
            '<div class="footer">&copy; 2023 GigaForge Inc. Powered by technology.</div>',
            "",
        )
        (tmp_path / "index.html").write_text(page, encoding="utf-8")
        verdict = check_artifacts(
            [Requirement("singleton", "exactly one footer on the page", ("footer",))],
            tmp_path, files=["index.html"],
        )
        assert verdict.ok

    def test_a_footer_tag_counts_as_the_role(self, tmp_path: Path) -> None:
        page = (
            "<html><body><footer><p>One</p></footer>"
            '<div class="footer"><p>Two</p></div></body></html>'
        )
        (tmp_path / "index.html").write_text(page, encoding="utf-8")
        verdict = check_artifacts(
            [Requirement("singleton", "exactly one footer on the page", ("footer",))],
            tmp_path, files=["index.html"],
        )
        assert not verdict.ok

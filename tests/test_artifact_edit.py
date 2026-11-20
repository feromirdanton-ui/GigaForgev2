"""Editing an existing page must be graded as strictly as creating one.

**The defect.**  ``classify_task`` returned ``code`` for an existing ``.html``
path, on the reasoning that "the file is there, so this is a change to a file".
The consequence chained through the pipeline:

* ``_sectional_generation`` never ran, so there was no section plan;
* ``ctx.extras["generation"]`` stayed empty, so ``requirements_from_plan`` was
  never called;
* the contract fell back to ``extract_requirements``, which returns two bare
  requirements for "add a footer and a hero section" and none of
  ``min_size``/``substance``/``heading`` (those are only added at three or
  more).

So a page being *edited* was graded more weakly than a page being *created* —
and editing is the common case: you create a page once and change it many
times.

These tests pin the three things that make an edit a first-class artifact run:
the routing verdict, the plan (union of what is there and what is asked for),
and the assembly (what the goal did not name is kept, not regenerated).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.harness.generation import (
    assemble_page,
    existing_sections,
    plan_generation,
)
from gigaforge.harness.goal_contract import requirements_from_plan
from gigaforge.harness.task_kind import classify_task

EXISTING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head><title>GigaForge</title><style>body{background:#05060a;color:#e8f6ff}</style></head>
<body>
  <nav class="nav"><a href="#top">GigaForge</a></nav>
  <section class="hero"><h1>GigaForge</h1><p>An agent harness.</p></section>
  <section class="features">
    <div class="feature-grid">
      <div class="feature-card"><h3>Plan</h3><p>It plans.</p></div>
      <div class="feature-card"><h3>Act</h3><p>It acts.</p></div>
      <div class="feature-card"><h3>Verify</h3><p>It verifies.</p></div>
    </div>
  </section>
</body>
</html>
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text(EXISTING_PAGE, encoding="utf-8")
    return tmp_path


class TestRouting:
    def test_an_existing_page_is_an_edit(self, repo: Path) -> None:
        kind = classify_task("Update index.html to add a footer", repo)
        assert kind.kind == "artifact_edit"
        assert kind.is_artifact, "an edit must keep the artifact workflow"
        assert kind.edit_paths == ("index.html",)

    def test_an_absent_page_is_a_creation(self, repo: Path) -> None:
        kind = classify_task("Create about.html", repo)
        assert kind.kind == "artifact"
        assert kind.artifact_paths == ("about.html",)

    def test_a_page_and_a_module_is_mixed(self, repo: Path) -> None:
        (repo / "db.py").write_text("X = 1\n", encoding="utf-8")
        kind = classify_task("Add a footer to index.html and fix db.py", repo)
        assert kind.kind == "mixed"
        assert kind.edit_paths == ("index.html",)
        assert kind.code_paths == ("db.py",)


class TestPlanForAnEdit:
    def test_the_plan_is_the_union_of_present_and_asked(self) -> None:
        plan = plan_generation("Add a footer to index.html", EXISTING_PAGE)
        kinds = plan.kinds()
        # What the page already has is kept...
        assert "hero" in kinds
        assert "features" in kinds
        # ...and what the goal asks for is added.
        assert "footer" in kinds

    def test_an_edit_keeps_the_pages_palette(self) -> None:
        """A goal that names no colour must not repaint the page."""
        page = EXISTING_PAGE.replace("#05060a", "cyan")
        plan = plan_generation("Add a footer", page)
        assert "cyan" in plan.palette

    def test_an_edit_keeps_the_pages_title(self) -> None:
        plan = plan_generation("Add a footer", EXISTING_PAGE)
        assert plan.title == "GigaForge"

    def test_the_plan_names_the_existing_path(self) -> None:
        plan = plan_generation("Add a footer to index.html", EXISTING_PAGE)
        assert plan.path == "index.html"


class TestExistingSections:
    def test_sections_are_found_by_class(self) -> None:
        found = existing_sections(EXISTING_PAGE)
        assert "hero" in found
        assert "features" in found
        assert "nav" in found

    def test_a_nested_section_does_not_end_the_outer_one(self) -> None:
        page = (
            '<section class="features"><div><section class="inner">x</section>'
            "</div></section>"
        )
        found = existing_sections(page)
        assert "features" in found
        assert found["features"].count("</section>") == 2

    def test_an_empty_page_has_no_sections(self) -> None:
        assert existing_sections("") == {}


class TestAssemblyKeepsWhatWasThere:
    def test_an_unnamed_section_is_reused_verbatim(self) -> None:
        """The goal asked for a footer; the hero must survive untouched."""
        plan = plan_generation("Add a footer to index.html", EXISTING_PAGE)
        present = existing_sections(EXISTING_PAGE)
        page = assemble_page(
            plan,
            {"footer": '<footer class="footer"><p>GigaForge 2026</p></footer>'},
            existing=present,
        )
        assert "An agent harness." in page, "the hero was regenerated away"
        assert "It verifies." in page, "the feature grid was lost"
        assert "GigaForge 2026" in page, "the new footer is missing"

    def test_the_edit_has_one_of_each_landmark(self) -> None:
        plan = plan_generation("Add a footer to index.html", EXISTING_PAGE)
        present = existing_sections(EXISTING_PAGE)
        page = assemble_page(
            plan,
            {"footer": '<footer class="footer"><p>GigaForge 2026</p></footer>'},
            existing=present,
        )
        assert page.count("<footer") == 1
        assert page.count("<h1") == 1
        assert page.count("<style") == 1


class TestTheContractIsNotWeakerForAnEdit:
    def test_an_edit_gets_the_full_contract(self) -> None:
        """The edit contract must carry the checks a creation gets.

        Before the fix, an edit fell through to ``extract_requirements``, which
        for "add a footer and a hero section" returns two bare requirements and
        none of ``min_size``/``substance``/``heading``.
        """
        plan = plan_generation("Add a footer to index.html", EXISTING_PAGE)
        reqs = requirements_from_plan(plan)
        kinds = {req.kind for req in reqs}
        assert {"heading", "min_size", "substance", "self_contained"} <= kinds
        assert "section 'footer'" in {req.label for req in reqs if req.kind == "section"}

    def test_the_edit_contract_checks_the_grid(self) -> None:
        plan = plan_generation("Add a footer to index.html", EXISTING_PAGE)
        reqs = requirements_from_plan(plan)
        assert "card_count" in {req.kind for req in reqs}

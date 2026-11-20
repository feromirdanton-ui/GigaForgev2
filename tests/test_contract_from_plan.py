"""The contract comes from the plan, and a child's CSS survives assembly.

Two defects:

* **5.2 — the gate was absent where it was needed.**  ``extract_requirements``
  returns zero requirements for "Create a single-file index.html", "Build a
  landing page" and "Make a website for my bakery".  The contract therefore did
  not exist for three of the four ordinary ways to ask for a page, and a page
  with no hero, no palette and four "Feature Card 1" divs passed.
* **N5 — a child's CSS was thrown away.**  ``_strip_document_wrapper`` deleted
  every ``<style>`` block, while the child's prompt said "draw with CSS".  A
  child's ``@keyframes glitch`` never reached the page, so the report's "glitch
  in the render" claim had no mechanism behind it.  The same defect explained
  "cards: 0": children wrote ``.feature-item`` and the base sheet styled
  ``.feature-card``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.harness.generation import (
    _extract_css,
    _scope_css,
    assemble_page,
    plan_generation,
)
from gigaforge.harness.goal_contract import (
    check_artifacts,
    extract_requirements,
    requirements_from_plan,
)

SITE_GOAL = (
    "Create a single-file index.html: a futuristic cyberpunk landing page about "
    "the GigaForge agent harness. Requirements: neon cyan and magenta palette, a "
    "hero section, a glitch effect on the title, a feature grid, and a footer. "
    "Self-contained, no external assets."
)

BARE_GOALS = [
    "Create a single-file index.html",
    "Build a landing page",
    "Make a website for my bakery",
]


class TestContractExistsForEveryPageGoal:
    @pytest.mark.parametrize("goal", BARE_GOALS)
    def test_extract_requirements_is_empty(self, goal: str) -> None:
        """The premise: the old source of requirements has nothing to say."""
        assert extract_requirements(goal) == []

    @pytest.mark.parametrize("goal", BARE_GOALS)
    def test_requirements_from_plan_is_not_empty(self, goal: str) -> None:
        reqs = requirements_from_plan(plan_generation(goal))
        assert reqs, "a page goal must always have a contract"
        labels = [r.label for r in reqs]
        assert "a headline (<h1>)" in labels
        assert "no external assets" in labels

    def test_contract_checks_the_sections_the_plan_ordered(self) -> None:
        plan = plan_generation(SITE_GOAL)
        labels = [r.label for r in requirements_from_plan(plan)]
        for kind in plan.kinds():
            assert f"section '{kind}'" in labels

    def test_contract_checks_the_palette_the_plan_read(self) -> None:
        plan = plan_generation(SITE_GOAL)
        labels = [r.label for r in requirements_from_plan(plan)]
        assert "colour 'cyan'" in labels
        assert "colour 'magenta'" in labels

    def test_contract_does_not_invent_a_palette(self) -> None:
        plan = plan_generation("Build a landing page")
        labels = [r.label for r in requirements_from_plan(plan)]
        assert not [label for label in labels if label.startswith("colour")]

    def test_a_skeleton_page_is_rejected(self, tmp_path: Path) -> None:
        page = (
            "<!DOCTYPE html><html><head><style>body{color:#fff}</style></head><body>"
            '<section class="hero"><h1>GigaForge</h1></section>'
            '<section class="features">'
            '<div class="feature-card"><h2>Feature Card 1</h2></div>'
            '<div class="feature-card"><h2>Feature Card 2</h2></div>'
            '<div class="feature-card"><h2>Feature Card 3</h2></div>'
            "</section><footer><p>Footer Content</p></footer></body></html>"
        )
        (tmp_path / "index.html").write_text(page, encoding="utf-8")
        reqs = requirements_from_plan(plan_generation(SITE_GOAL))
        verdict = check_artifacts(reqs, tmp_path, files=["index.html"])
        assert not verdict.ok
        assert any("placeholder" in u for u in verdict.unmet)

    def test_a_real_page_passes(self, tmp_path: Path) -> None:
        plan = plan_generation(SITE_GOAL)
        fragments = {
            "hero": '<section class="hero"><h1 class="glitch">GigaForge</h1>'
                    "<p>A harness that takes on the work so the model can decide.</p></section>",
            "features": '<section class="features">' + "".join(
                f'<div class="feature-card"><h2>Capability {i}</h2>'
                f"<p>The harness computes what has one correct answer, and the "
                f"model spends its tokens on judgement instead of arithmetic.</p></div>"
                for i in range(1, 5)
            ) + "</section>",
            "footer": "<footer><p>GigaForge — built to carry the work, not to wrap a chat.</p></footer>",
        }
        page = assemble_page(plan, fragments)
        (tmp_path / "index.html").write_text(page, encoding="utf-8")
        verdict = check_artifacts(requirements_from_plan(plan), tmp_path, files=["index.html"])
        assert verdict.ok, verdict.unmet


class TestCardCount:
    def test_feature_item_spelling_is_counted(self, tmp_path: Path) -> None:
        """Measured live: 'cards: 0' while four cards were on screen."""
        page = (
            "<!DOCTYPE html><html><head><style>body{color:#fff}</style></head><body>"
            '<section class="hero"><h1>GigaForge</h1></section>'
            '<section class="features">'
            + "".join(f'<div class="feature-item"><h2>F{i}</h2></div>' for i in range(4))
            + "</section></body></html>"
        )
        (tmp_path / "index.html").write_text(page, encoding="utf-8")
        reqs = [r for r in requirements_from_plan(plan_generation(SITE_GOAL))
                if r.kind == "card_count"]
        assert reqs
        verdict = check_artifacts(reqs, tmp_path, files=["index.html"])
        assert verdict.ok, verdict.unmet

    def test_two_cards_is_not_a_grid(self, tmp_path: Path) -> None:
        page = (
            "<!DOCTYPE html><html><body>"
            '<section class="features">'
            '<div class="feature-card"><h2>A</h2></div>'
            '<div class="feature-card"><h2>B</h2></div>'
            "</section></body></html>"
        )
        (tmp_path / "index.html").write_text(page, encoding="utf-8")
        reqs = [r for r in requirements_from_plan(plan_generation(SITE_GOAL))
                if r.kind == "card_count"]
        verdict = check_artifacts(reqs, tmp_path, files=["index.html"])
        assert not verdict.ok
        assert "found 2 card(s)" in verdict.unmet[0]


class TestChildCssSurvives:
    def test_extract_css_returns_both_parts(self) -> None:
        markup, css = _extract_css(
            '<section class="hero"><h1>Hi</h1></section>'
            "<style>@keyframes glitch { 0% { opacity: 0; } }</style>"
        )
        assert "<h1>Hi</h1>" in markup
        assert "<style>" not in markup
        assert "@keyframes glitch" in css

    def test_keyframes_reach_the_assembled_page(self) -> None:
        plan = plan_generation(SITE_GOAL)
        page = assemble_page(plan, {
            "hero": '<section class="hero"><h1 class="glitch">GigaForge</h1></section>'
                    "<style>@keyframes glitch { 0% { transform: translate(0); } "
                    "50% { transform: translate(2px, -2px); } }</style>",
            "features": '<section class="features"><div class="feature-card"><h2>A</h2></div></section>',
            "footer": "<footer><p>Built by GigaForge.</p></footer>",
        })
        assert "@keyframes glitch" in page, "the child's keyframes were dropped"

    def test_child_css_is_scoped_to_its_section(self) -> None:
        scoped = _scope_css("h2 { color: cyan; }", "hero")
        assert ".hero h2" in scoped

    def test_page_level_selectors_are_not_scoped(self) -> None:
        scoped = _scope_css("body { margin: 0; }", "hero")
        assert "body {" in scoped
        assert ".hero body" not in scoped

    def test_at_rules_are_left_alone(self) -> None:
        scoped = _scope_css("@keyframes glitch { 0% { opacity: 0; } }", "hero")
        assert scoped.startswith("@keyframes")

    def test_the_page_has_exactly_one_style_block(self) -> None:
        plan = plan_generation(SITE_GOAL)
        page = assemble_page(plan, {
            "hero": '<section class="hero"><h1>Hi</h1></section><style>h1{color:cyan}</style>',
            "features": '<section class="features"><div class="feature-card"><h2>A</h2></div></section>'
                        "<style>.feature-card{color:magenta}</style>",
            "footer": "<footer><p>Bye</p></footer>",
        })
        assert page.count("<style>") == 1
        assert page.count("</style>") == 1

    def test_feature_item_is_styled_by_the_base_sheet(self) -> None:
        plan = plan_generation(SITE_GOAL)
        page = assemble_page(plan, {
            "hero": '<section class="hero"><h1>Hi</h1></section>',
            "features": '<section class="features"><div class="feature-item"><h2>A</h2></div></section>',
            "footer": "<footer><p>Bye</p></footer>",
        })
        assert ".feature-item" in page, "children write .feature-item; the sheet must style it"

"""A planned section must not vanish because a child returned nothing.

Measured live on ``subagents``: the footer child returned nothing usable, the
footer was dropped from the page, and the run still reported ``accepted: True``
— the contract had no footer requirement to fail on, so nothing noticed.

The plan is the harness's own promise.  If a child cannot fill a section, the
harness fills it from the plan rather than shipping a page that is missing a
part it said it would have, and it says in the file that it did.
"""
from __future__ import annotations

from gigaforge.harness.generation import assemble_page, plan_generation

GOAL = (
    "Create a single-file index.html: a cyberpunk landing page about GigaForge. "
    "Requirements: neon cyan and magenta palette, a hero section with a glitch "
    "effect, a feature grid with four cards, and a footer."
)

HERO = '<section class="hero"><h1>GigaForge</h1></section>'
FEATURES = (
    '<section class="features"><div class="feature-grid">'
    '<div class="feature-card">A</div><div class="feature-card">B</div>'
    '<div class="feature-card">C</div></div></section>'
)


class TestMissingSectionsAreFilled:
    def test_the_footer_survives_a_silent_child(self) -> None:
        page = assemble_page(plan_generation(GOAL), {"hero": HERO, "features": FEATURES})
        assert "<footer" in page, "a planned section was dropped"

    def test_the_file_says_the_harness_filled_it(self) -> None:
        page = assemble_page(plan_generation(GOAL), {"hero": HERO, "features": FEATURES})
        assert "filled by the harness" in page

    def test_a_child_fragment_is_preferred_when_present(self) -> None:
        footer = '<footer class="footer"><p>Mine</p></footer>'
        page = assemble_page(
            plan_generation(GOAL), {"hero": HERO, "features": FEATURES, "footer": footer}
        )
        assert "Mine" in page
        assert "filled by the harness" not in page

    def test_every_planned_section_is_present(self) -> None:
        plan = plan_generation(GOAL)
        page = assemble_page(plan, {})
        for section in plan.sections:
            assert section.kind in page or section.anchor in page, (
                f"planned section {section.kind} is missing from the page"
            )

    def test_the_page_is_still_structurally_single(self) -> None:
        page = assemble_page(plan_generation(GOAL), {})
        assert page.count("<html") == 1
        assert page.count("<body") == 1
        assert page.count("<style>") == 1

"""Sectional generation: the fan-out a "build me a page" goal needs.

The defect these tests pin: ``--subagents`` was gated on the goal naming two or
three *existing* files, so a generation goal — which names none — silently fell
back to one model call that wrote the whole page.  Measured live:
``pipeline.subagents_skipped {"files": 1}`` and a 2564-byte page with no grid.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.harness.generation import (
    assemble_page,
    is_page_goal,
    plan_generation,
    section_prompt,
)
from gigaforge.harness.html_structure import audit_html_structure

SITE_GOAL = (
    "Create a single-file index.html: a futuristic cyberpunk landing page about "
    "the GigaForge agent harness. Requirements: neon cyan and magenta palette, a "
    "hero section, a glitch effect on the title, a feature grid, and a footer. "
    "Self-contained, no external assets."
)

CODE_GOAL = "Fix the failing test in tests/test_calc.py: mul returns the wrong value."


class TestIsPageGoal:
    def test_site_goal_is_a_page_goal(self) -> None:
        assert is_page_goal(SITE_GOAL)

    def test_code_goal_is_not_a_page_goal(self) -> None:
        assert not is_page_goal(CODE_GOAL)


class TestPlan:
    def test_plan_names_the_sections_the_brief_names(self) -> None:
        plan = plan_generation(SITE_GOAL)
        kinds = plan.kinds()
        assert "hero" in kinds
        assert "features" in kinds
        assert "footer" in kinds

    def test_plan_keeps_catalogue_order(self) -> None:
        kinds = plan_generation(SITE_GOAL).kinds()
        assert kinds.index("hero") < kinds.index("features") < kinds.index("footer")

    def test_plan_reads_the_palette_from_the_goal(self) -> None:
        assert plan_generation(SITE_GOAL).palette == ("cyan", "magenta")

    def test_plan_reads_the_path_from_the_goal(self) -> None:
        assert plan_generation(SITE_GOAL).path == "index.html"

    def test_plan_does_not_invent_sections(self) -> None:
        plan = plan_generation("Create a landing page with a hero and a footer.")
        assert "pricing" not in plan.kinds()
        assert "testimonials" not in plan.kinds()

    def test_a_goal_naming_no_section_gets_the_minimum_shape(self) -> None:
        plan = plan_generation("Build a website for my bakery.")
        assert plan.kinds() == ["hero", "features", "footer"]

    def test_a_footer_is_always_planned(self) -> None:
        plan = plan_generation("Create a landing page with a hero section.")
        assert "footer" in plan.kinds()


class TestSectionPrompt:
    def test_prompt_forbids_a_second_document(self) -> None:
        plan = plan_generation(SITE_GOAL)
        prompt = section_prompt(plan, plan.sections[0], SITE_GOAL)
        assert "do NOT emit <!DOCTYPE>" in prompt
        assert "<style>" in prompt

    def test_prompt_names_the_palette(self) -> None:
        plan = plan_generation(SITE_GOAL)
        prompt = section_prompt(plan, plan.sections[0], SITE_GOAL)
        assert "cyan" in prompt and "magenta" in prompt


class TestAssembly:
    def _fragments(self) -> dict[str, str]:
        return {
            "hero": '<section class="hero"><h1>GigaForge</h1><p>An agent harness.</p></section>',
            "features": (
                '<section class="features">'
                '<div class="feature-card"><h2>Plan</h2><p>It plans.</p></div>'
                "</section>"
            ),
            "footer": "<footer><p>Built by GigaForge.</p></footer>",
        }

    def test_assembled_page_is_structurally_valid(self) -> None:
        plan = plan_generation(SITE_GOAL)
        page = assemble_page(plan, self._fragments())
        assert audit_html_structure(page).ok, audit_html_structure(page).issues()

    def test_assembled_page_has_exactly_one_document(self) -> None:
        page = assemble_page(plan_generation(SITE_GOAL), self._fragments())
        assert page.count("<!DOCTYPE") == 1
        assert page.count("<html") == 1
        assert page.count("<body") == 1
        assert page.count("<style") == 1

    def test_each_section_appears_once(self) -> None:
        page = assemble_page(plan_generation(SITE_GOAL), self._fragments())
        assert page.count('class="hero"') == 1
        assert page.count('class="features"') == 1
        assert page.count("<footer") == 1

    def test_a_child_that_returns_a_whole_document_is_reduced(self) -> None:
        """The exact failure that shipped: a child writes a full page."""
        plan = plan_generation(SITE_GOAL)
        fragments = self._fragments()
        fragments["hero"] = (
            "<!DOCTYPE html><html><head><style>body{color:red;background:#000;}</style>"
            "</head><body><section class=\"hero\"><h1>GigaForge</h1></section></body></html>"
        )
        page = assemble_page(plan, fragments)
        assert page.count("<!DOCTYPE") == 1
        assert page.count("<style") == 1
        assert audit_html_structure(page).ok, audit_html_structure(page).issues()

    def test_a_fenced_answer_is_unwrapped(self) -> None:
        plan = plan_generation(SITE_GOAL)
        fragments = self._fragments()
        fragments["footer"] = "```html\n<footer><p>Built by GigaForge.</p></footer>\n```"
        page = assemble_page(plan, fragments)
        assert "```" not in page
        assert "<footer>" in page

    def test_assembly_is_deterministic(self) -> None:
        plan = plan_generation(SITE_GOAL)
        first = assemble_page(plan, self._fragments())
        second = assemble_page(plan, self._fragments())
        assert first == second

    def test_a_missing_section_does_not_break_the_page(self) -> None:
        plan = plan_generation(SITE_GOAL)
        fragments = self._fragments()
        fragments.pop("features")
        page = assemble_page(plan, fragments)
        assert audit_html_structure(page).ok
        assert "<footer>" in page


class TestPipelineWiring:
    """The generation path must be reachable from the subagent entry point."""

    def test_page_goal_routes_to_sectional_generation(self) -> None:
        import inspect

        from gigaforge.core.pipeline import TaskPipeline

        source = inspect.getsource(TaskPipeline._subagent_diff)
        assert "_sectional_generation" in source, (
            "a page goal must reach the sectional path, not the file fan-out"
        )
        assert "is_page_goal" in source

    def test_sectional_generation_writes_the_page(self, tmp_path: Path) -> None:
        """Drive the real method with a stub model and check the file on disk."""
        from gigaforge.core.pipeline import TaskPipeline

        class StubPipeline(TaskPipeline):
            def __init__(self) -> None:  # noqa: D107 - bypass the real constructor
                self.config = type("C", (), {"repo_root": str(tmp_path)})()
                self.events: list[tuple[str, dict]] = []

            def _ask(self, phase: str, prompt: str, ctx, **_kw) -> str:
                if "Hero" in prompt:
                    return '<section class="hero"><h1>GigaForge</h1></section>'
                if "Features" in prompt:
                    return (
                        '<section class="features"><div class="feature-card">'
                        "<h2>Plan</h2><p>It plans.</p></div></section>"
                    )
                return "<footer><p>Built by GigaForge.</p></footer>"

            def _event(self, name: str, **payload) -> None:
                self.events.append((name, payload))

            def _repo_root(self) -> Path:
                return tmp_path

        class Ctx:
            run_id = "test-run"
            state = type("S", (), {"goal": SITE_GOAL})()
            extras: dict = {}

        pipeline = StubPipeline()
        diff = pipeline._sectional_generation(Ctx())
        page = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert audit_html_structure(page).ok, audit_html_structure(page).issues()
        assert "GigaForge" in page
        assert diff, "the assembled page must be turned into a diff"
        assert Ctx.extras["generation"]["written"] == ["features", "footer", "hero"]

    def test_a_failing_section_is_reported_not_fatal(self, tmp_path: Path) -> None:
        from gigaforge.core.pipeline import TaskPipeline

        class StubPipeline(TaskPipeline):
            def __init__(self) -> None:  # noqa: D107
                self.config = type("C", (), {"repo_root": str(tmp_path)})()

            def _ask(self, phase: str, prompt: str, ctx, **_kw) -> str:
                if "Features" in prompt:
                    raise RuntimeError("model unavailable")
                if "Hero" in prompt:
                    return '<section class="hero"><h1>GigaForge</h1></section>'
                return "<footer><p>Built by GigaForge.</p></footer>"

            def _event(self, name: str, **payload) -> None:
                return

            def _repo_root(self) -> Path:
                return tmp_path

        class Ctx:
            run_id = "test-run"
            state = type("S", (), {"goal": SITE_GOAL})()
            extras: dict = {}

        pipeline = StubPipeline()
        pipeline._sectional_generation(Ctx())
        page = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "GigaForge" in page
        assert Ctx.extras["generation"]["failed"] == ["features"]


class TestPlanStability:
    """The plan must come from the user's goal, not the harness's repair notes."""

    def test_feedback_block_does_not_change_the_plan(self) -> None:
        from gigaforge.core.pipeline import _original_goal

        feedback = (
            "\n\nLONG-LIVED GOAL MODE — CONTINUE THE EXISTING TASK. "
            "The previous cycle did not complete. Inspect the current workflow "
            "and fix the reported pipeline failure."
        )
        plain = plan_generation(SITE_GOAL).kinds()
        with_feedback = plan_generation(_original_goal(SITE_GOAL + feedback)).kinds()
        assert plain == with_feedback, (
            "the feedback prose mentions 'workflow', which the catalogue reads as "
            "a 'how it works' section — the plan drifted between cycles"
        )

    def test_original_goal_strips_only_the_feedback_block(self) -> None:
        from gigaforge.core.pipeline import _original_goal

        assert _original_goal(SITE_GOAL) == SITE_GOAL
        assert _original_goal(SITE_GOAL + "\n\nLONG-LIVED GOAL MODE — x") == SITE_GOAL

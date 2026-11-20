"""Plan validation: the step between "a plan exists" and "run it".

Live motivation: the ReAct episode returned ``edit_function: react.py`` \u2014 a
step naming the file it had been reading, with no symbol and no verification \u2014
and the pipeline executed it.  ``score_steps`` rated the plan; nothing checked
that it could run.
"""
from __future__ import annotations

from gigaforge.core.task_state import PlanStep
from gigaforge.harness.plan_guard import (
    MAX_STEPS,
    PlanVerdict,
    validate_plan,
)


def _step(tool: str, **args: object) -> PlanStep:
    return PlanStep(tool=tool, args=dict(args))


def test_unknown_tool_is_dropped() -> None:
    verdict = validate_plan(
        [_step("edit_function", name="f"), _step("teleport", name="x")],
        known_tools={"edit_function", "tests"},
    )
    assert [s.tool for s in verdict.steps] == ["edit_function", "tests"]
    assert verdict.dropped_unknown == ["teleport"]


def test_edit_without_a_target_is_dropped() -> None:
    """"edit something" cannot be executed or checked."""
    verdict = validate_plan(
        [_step("edit_function"), _step("edit_function", name="real")],
        known_tools={"edit_function"},
    )
    assert [s.args.get("name") for s in verdict.steps] == ["real", None]
    assert verdict.dropped_untargeted == ["edit_function"]


def test_duplicate_steps_collapse() -> None:
    verdict = validate_plan(
        [_step("edit_function", name="f"), _step("edit_function", name="f")],
        known_tools={"edit_function"},
    )
    assert len(verdict.steps) == 2, "one edit plus the appended verification"
    assert verdict.deduped == 1


def test_verification_is_appended_when_the_plan_only_edits() -> None:
    """A plan that edits and never checks is a plan that hopes."""
    verdict = validate_plan([_step("edit_function", name="f")], known_tools={"edit_function"})
    assert verdict.appended_verification is True
    assert verdict.steps[-1].tool == "tests"


def test_verification_is_not_appended_when_already_present() -> None:
    verdict = validate_plan(
        [_step("edit_function", name="f"), _step("tests")],
        known_tools={"edit_function", "tests"},
    )
    assert verdict.appended_verification is False
    assert [s.tool for s in verdict.steps] == ["edit_function", "tests"]


def test_verification_can_be_disabled() -> None:
    verdict = validate_plan(
        [_step("edit_function", name="f")],
        known_tools={"edit_function"},
        require_verification=False,
    )
    assert verdict.appended_verification is False
    assert len(verdict.steps) == 1


def test_long_plans_are_truncated() -> None:
    """Twenty steps for a one-line fix is drift, not thoroughness."""
    steps = [_step("edit_function", name=f"f{i}") for i in range(MAX_STEPS + 5)]
    verdict = validate_plan(steps, known_tools={"edit_function"})
    assert len(verdict.steps) == MAX_STEPS
    # 17 edits + the appended verification = 18, capped at 12.
    assert verdict.truncated == 6


def test_empty_plan_stays_empty_and_gains_nothing() -> None:
    verdict = validate_plan([], known_tools={"edit_function"})
    assert verdict.steps == []
    assert verdict.appended_verification is False
    assert verdict.changed is False


def test_known_tools_none_skips_the_tool_check() -> None:
    verdict = validate_plan([_step("anything", name="x")], known_tools=None)
    assert [s.tool for s in verdict.steps] == ["anything"]


def test_verdict_reports_whether_it_changed_anything() -> None:
    clean = validate_plan(
        [_step("edit_function", name="f"), _step("tests")],
        known_tools={"edit_function", "tests"},
    )
    assert clean.changed is False
    assert clean.to_dict()["changed"] is False

    dirty = validate_plan([_step("nope")], known_tools={"edit_function"})
    assert dirty.changed is True
    assert isinstance(dirty, PlanVerdict)

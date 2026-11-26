"""The improvement harness: lessons that survive one task and guide the next.

Each test names the failure it prevents.  The central one is that a reflection
step with no evidence produces advice that cannot be confirmed or removed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.lessons import (  # noqa: E402
    ImprovementHarness,
    Lesson,
    reflect,
)


# ── reflection is derived from counts, not from fluency ────────────────────
def test_one_phase_failure_is_an_incident_not_a_lesson() -> None:
    """A lesson drawn from a single failure sends the proposer after a phase
    that may have failed for a reason outside the harness."""
    lessons = reflect({"phase_failures": {"code": 1}})
    assert lessons == []


def test_two_phase_failures_are_a_pattern() -> None:
    lessons = reflect({"phase_failures": {"code": 2}}, task="toy-add-fix")
    assert len(lessons) == 1
    assert lessons[0].kind == "phase_failure"
    assert "code" in lessons[0].claim
    assert lessons[0].task == "toy-add-fix"


def test_a_lesson_carries_its_evidence() -> None:
    """A claim without a citation is a comment, and comments do not get probed."""
    lessons = reflect({"tool_errors": {"read_file": 3}})
    assert lessons[0].evidence
    assert "3" in lessons[0].evidence


def test_budget_exhaustion_is_a_lesson() -> None:
    lessons = reflect({"budget_exhausted": True})
    assert [l.kind for l in lessons] == ["budget_exhaustion"]


def test_a_single_gate_rejection_is_not_a_lesson() -> None:
    """One rejection is the gate working; two is the gate disagreeing with the
    harness about what "complete" means."""
    assert reflect({"gate_rejections": ["section 'Overview' has 0 chars"]}) == []
    assert len(reflect({"gate_rejections": ["a", "b"]})) == 1


def test_a_regression_names_the_task_it_broke() -> None:
    lessons = reflect({"regressions": ["toy-validate-email"]})
    assert lessons[0].kind == "regression"
    assert "toy-validate-email" in lessons[0].claim


def test_a_clean_run_produces_no_lessons() -> None:
    assert reflect({}) == []


# ── merging makes the counts meaningful ────────────────────────────────────
def test_the_same_lesson_from_two_tasks_is_one_lesson_confirmed_twice() -> None:
    """Three tasks with the same failure is one strong lesson, not three weak ones."""
    harness = ImprovementHarness()
    harness.add(reflect({"phase_failures": {"code": 2}}, task="a")[0])
    harness.add(reflect({"phase_failures": {"code": 2}}, task="b")[0])
    assert len(harness.lessons) == 1
    assert harness.lessons[0].confirmations == 2


def test_a_contradicted_lesson_is_retired() -> None:
    """A lesson that is right half the time is a coin flip, not a lesson."""
    harness = ImprovementHarness()
    harness.add(Lesson(kind="phase_failure", claim="phase 'code' fails repeatedly",
                       evidence="2 failures"))
    harness.contradict("phase_failure", "phase 'code' fails repeatedly")
    harness.contradict("phase_failure", "phase 'code' fails repeatedly")
    assert harness.lessons[0].retired is True
    assert harness.active() == []


def test_a_lesson_confirmed_more_than_contradicted_stays_active() -> None:
    harness = ImprovementHarness()
    harness.add(Lesson(kind="phase_failure", claim="c", evidence="e"))
    harness.add(Lesson(kind="phase_failure", claim="c", evidence="e"))
    harness.contradict("phase_failure", "c")
    assert harness.lessons[0].retired is False
    assert len(harness.active()) == 1


def test_active_lessons_are_ordered_by_support() -> None:
    harness = ImprovementHarness()
    weak = Lesson(kind="phase_failure", claim="weak", evidence="e")
    strong = Lesson(kind="phase_failure", claim="strong", evidence="e",
                    confirmations=5)
    harness.add(weak)
    harness.add(strong)
    assert harness.active()[0].claim == "strong"


# ── the bridge from reflection to proposal ─────────────────────────────────
def test_a_lesson_names_a_component_the_proposer_can_change() -> None:
    """A lesson with no component is a comment, and comments do not get probed."""
    harness = ImprovementHarness()
    harness.add(reflect({"phase_failures": {"code": 2}})[0])
    assert harness.components_to_try() == ["phase_prompts"]


def test_each_lesson_kind_maps_to_a_distinct_component() -> None:
    harness = ImprovementHarness()
    harness.add(reflect({"phase_failures": {"code": 2}})[0])
    harness.add(reflect({"tool_errors": {"read_file": 2}})[0])
    harness.add(reflect({"budget_exhausted": True})[0])
    components = harness.components_to_try()
    assert len(components) == len(set(components))
    assert "phase_prompts" in components
    assert "tool_set" in components
    assert "context_budget" in components


def test_a_retired_lesson_does_not_direct_the_proposer() -> None:
    harness = ImprovementHarness()
    harness.add(Lesson(kind="phase_failure", claim="c", evidence="e"))
    harness.contradict("phase_failure", "c")
    harness.contradict("phase_failure", "c")
    assert harness.components_to_try() == []


# ── adopted surfaces ───────────────────────────────────────────────────────
def test_the_best_known_surface_is_the_last_adopted() -> None:
    harness = ImprovementHarness()
    harness.adopt("aaa")
    harness.adopt("bbb")
    assert harness.best_known() == "bbb"


def test_adopting_the_same_surface_twice_does_not_duplicate_it() -> None:
    harness = ImprovementHarness()
    harness.adopt("aaa")
    harness.adopt("aaa")
    assert harness.adopted == ["aaa"]


def test_no_adopted_surface_is_empty_not_none() -> None:
    assert ImprovementHarness().best_known() == ""


# ── persistence ────────────────────────────────────────────────────────────
def test_the_improvement_harness_round_trips(tmp_path: Path) -> None:
    harness = ImprovementHarness()
    harness.add(reflect({"phase_failures": {"code": 2}}, task="a")[0])
    harness.adopt("abc123")
    path = harness.save(tmp_path / "improvement.json")
    loaded = ImprovementHarness.load(path)
    assert len(loaded.lessons) == 1
    assert loaded.lessons[0].task == "a"
    assert loaded.best_known() == "abc123"


def test_a_corrupt_improvement_harness_does_not_stop_a_run(tmp_path: Path) -> None:
    """The lessons are an optimisation; losing them costs a probe, not a result."""
    path = tmp_path / "improvement.json"
    path.write_text("{not json", encoding="utf-8")
    assert ImprovementHarness.load(path).lessons == []


def test_a_missing_improvement_harness_is_empty(tmp_path: Path) -> None:
    assert ImprovementHarness.load(tmp_path / "nope.json").lessons == []

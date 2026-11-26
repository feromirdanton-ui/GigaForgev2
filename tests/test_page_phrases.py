"""The phrase list must know the words users actually type.

**The defect this pins.**  Measured live: ``"Build a one-page landing site for
GigaForge ..."`` classified as ``code`` because :data:`PAGE_PHRASES` listed
"landing page" and not "landing site".  The run then went through the coder,
which wrote ``landing_page.html`` full of "Feature 1 / Description of feature 1"
and was rejected after 22 632 tokens.

The task class chooses the executor, so a missing spelling is not a cosmetic gap
— it routes the goal to the wrong pipeline.  This test pins the exact live goal
and the common synonyms a user reaches for.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.task_kind import PAGE_PHRASES, classify_task  # noqa: E402

LIVE_GOAL = (
    "Build a one-page landing site for GigaForge, an agent harness, in a "
    "futuristic cyberpunk style. It needs a hero section, a grid of exactly "
    "four feature cards, and a footer."
)


def test_the_exact_live_goal_is_an_artifact() -> None:
    kind = classify_task(LIVE_GOAL, None)
    assert kind.is_artifact, kind.evidence


@pytest.mark.parametrize(
    "goal",
    [
        "Build a landing page for our product.",
        "Build a landing site for our product.",
        "Create a one-page site for our product.",
        "Make a single-page site for our product.",
        "Create a marketing site for our product.",
        "Сделай одностраничный сайт для нашего продукта.",
        "Сделай лендинг для нашего продукта.",
    ],
)
def test_common_spellings_all_route_to_the_page_builder(goal: str) -> None:
    kind = classify_task(goal, None)
    assert kind.is_artifact, f"{goal!r} -> {kind.kind} ({kind.evidence})"


def test_the_phrase_list_has_no_duplicates() -> None:
    assert len(PAGE_PHRASES) == len(set(PAGE_PHRASES))


def test_a_code_goal_is_not_captured_by_a_page_phrase() -> None:
    """The wider list must not swallow ordinary code work."""
    kind = classify_task("Fix the bug in calc.py so mul returns the product.", None)
    assert kind.kind == "code", kind.evidence

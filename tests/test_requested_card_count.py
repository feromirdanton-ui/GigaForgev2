"""The contract must check the number the user wrote, not the harness's default.

**The defect this file exists for.**  A live goal said "a features grid with 4
cards".  The page shipped three.  The contract passed it, because the
requirement was a hard-coded "at least 3 feature cards in the grid" — the
harness was grading its own default instead of the request.

That is the same class of defect as the earlier "cards: 0 while four cards were
on screen": in both cases the contract measured something other than the
artifact against something other than the goal.  The fix is to read the number
from the goal, which is a fact the harness can extract without a model call.

The extraction has to be narrow, and the tests say how narrow: a number
attached to a card-ish noun counts; "4 sections" and "3 colours" do not; a year
("2026 cards") and a version ("v2.5 cards") do not.
"""
from __future__ import annotations

import pytest

from gigaforge.harness.goal_contract import (
    Requirement,
    check_artifacts,
    requested_card_count,
    requirements_from_plan,
)
from gigaforge.harness.generation import plan_generation


# ── reading the number ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "goal,expected",
    [
        ("a features grid with 4 cards", 4),
        ("4 feature cards", 4),
        ("a grid with 12 tiles", 12),
        ("six panels", 0),  # a word, not a digit: not guessed at
        ("a grid with 2 columns", 2),
    ],
)
def test_the_requested_count_is_read(goal: str, expected: int) -> None:
    assert requested_card_count(goal) == expected


@pytest.mark.parametrize(
    "goal",
    [
        "4 sections and 3 colours",
        "2026 cards",
        "v2.5 cards",
        "no number here",
        "",
    ],
)
def test_a_number_that_is_not_a_card_count_is_ignored(goal: str) -> None:
    """A year, a version or a section count must not become a card requirement."""
    assert requested_card_count(goal) == 0


# ── the requirement carries the number ──────────────────────────────────────
def test_the_plan_contract_uses_the_requested_count() -> None:
    plan = plan_generation("Create index.html: a landing page with a features grid with 4 cards")
    reqs = {r.kind: r for r in requirements_from_plan(plan)}
    assert "card_count" in reqs
    assert reqs["card_count"].minimum == 4
    assert "4" in reqs["card_count"].label


def test_a_goal_without_a_number_keeps_the_default() -> None:
    plan = plan_generation("Create index.html: a landing page with a features grid")
    reqs = {r.kind: r for r in requirements_from_plan(plan)}
    assert reqs["card_count"].minimum == 3


def test_the_plan_remembers_its_goal() -> None:
    """The contract reads the number from the plan, so the plan must carry it."""
    goal = "Create index.html: a landing page with a features grid with 4 cards"
    assert plan_generation(goal).goal == goal


# ── the check honours the number ────────────────────────────────────────────
def _page(cards: int) -> str:
    body = "\n".join(
        f'<div class="feature-card"><h3>Feature {i}</h3>'
        f"<p>Real content for feature number {i}, with enough words to count.</p></div>"
        for i in range(1, cards + 1)
    )
    return (
        "<!DOCTYPE html><html><head><title>GigaForge</title>"
        "<style>.feature-grid{display:grid;grid-template-columns:repeat(4,1fr)}</style>"
        "</head><body>"
        "<section id=\"hero\"><h1>GigaForge Agent Harness</h1>"
        "<p>A cyberpunk landing page about the harness, with real prose in it.</p></section>"
        f'<section id="features"><h2>Features</h2><div class="feature-grid">{body}</div></section>'
        "<footer id=\"footer\"><p>© 2026 GigaForge. All rights reserved.</p></footer>"
        "</body></html>"
    )


def test_three_cards_fail_a_four_card_goal(tmp_path) -> None:
    """The live failure: three cards passed a "4 cards" goal."""
    (tmp_path / "index.html").write_text(_page(3))
    reqs = [Requirement("card_count", "at least 4 feature cards in the grid",
                        ("feature-card",), minimum=4)]
    verdict = check_artifacts(reqs, tmp_path, ["index.html"])
    assert not verdict.ok
    assert any("card" in u for u in verdict.unmet)


def test_four_cards_pass_a_four_card_goal(tmp_path) -> None:
    (tmp_path / "index.html").write_text(_page(4))
    reqs = [Requirement("card_count", "at least 4 feature cards in the grid",
                        ("feature-card",), minimum=4)]
    verdict = check_artifacts(reqs, tmp_path, ["index.html"])
    assert verdict.ok


def test_three_cards_still_pass_a_three_card_goal(tmp_path) -> None:
    """The default must not become stricter than it was."""
    (tmp_path / "index.html").write_text(_page(3))
    reqs = [Requirement("card_count", "at least 3 feature cards in the grid",
                        ("feature-card",), minimum=3)]
    assert check_artifacts(reqs, tmp_path, ["index.html"]).ok

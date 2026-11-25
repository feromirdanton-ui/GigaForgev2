"""The harness counts cards; it does not ask the model to.

Measured live on ``subagents``: the features child returned
``<div class="feature-card">`` with no content, so the page shipped three cards
where the goal asked for four, and the first of them was blank.  Both are
countable facts about markup the harness already holds.
"""
from __future__ import annotations

from gigaforge.harness.generation import _normalise_cards, plan_generation

GOAL = (
    "Create a single-file index.html: a cyberpunk landing page about the "
    "GigaForge agent harness. A feature grid with four cards, and a footer."
)


class _Section:
    kind = "features"


def test_an_empty_card_is_dropped_and_the_count_is_restored() -> None:
    plan = plan_generation(GOAL)
    assert plan.card_count == 4

    fragment = (
        '<section class="features"><h2>Features</h2><div class="feature-grid">'
        '<div class="feature-card"></div>'
        '<div class="feature-card"><h3>A</h3><p>a</p></div>'
        '<div class="feature-card"><h3>B</h3><p>b</p></div>'
        "</div></section>"
    )
    out = _normalise_cards(fragment, _Section(), plan)
    assert out.count('class="feature-card"') == 4
    assert "<h3>A</h3>" in out and "<h3>B</h3>" in out


def test_a_plan_without_a_count_leaves_the_markup_alone() -> None:
    plan = plan_generation("Build a landing page")
    assert plan.card_count == 0
    fragment = '<section class="features"><div class="feature-grid"><div class="feature-card"></div></div></section>'
    assert _normalise_cards(fragment, _Section(), plan) == fragment

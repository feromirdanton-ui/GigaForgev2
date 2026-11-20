"""The contract must grade the artifact, not its own vocabulary.

Measured live: a section child wrote ``<div class="grid-item">`` for its four
cards.  The page had a grid with four children, and the contract failed it with
"at least 3 feature cards in the grid (found 0 card(s) in the markup)" — because
the count looked only for the class names the harness happened to guess
(``feature-card``, ``feature-item``, ``card``).

A card is whatever the grid holds.  The structural reading is the fact; the
class name is a convention the harness invented.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.harness.goal_contract import (
    Requirement,
    _count_grid_children,
    check_artifacts,
)

FOUR_GRID_ITEMS = (
    '<html><body><section class="hero"><h1>GigaForge</h1></section>'
    '<div class="feature-grid">'
    '<div class="grid-item"><h3>One</h3><p>First feature.</p></div>'
    '<div class="grid-item"><h3>Two</h3><p>Second feature.</p></div>'
    '<div class="grid-item"><h3>Three</h3><p>Third feature.</p></div>'
    '<div class="grid-item"><h3>Four</h3><p>Fourth feature.</p></div>'
    "</div></body></html>"
)

CARD_REQ = Requirement("card_count", "at least 3 feature cards in the grid",
                       ("feature-card", "feature-item", "card"))


class TestStructuralCount:
    def test_four_grid_items_are_four_cards(self) -> None:
        assert _count_grid_children(FOUR_GRID_ITEMS) == 4

    def test_nested_wrappers_are_not_double_counted(self) -> None:
        page = (
            '<div class="features">'
            '<div class="card"><div class="inner">x</div></div>'
            '<div class="card">y</div><div class="card">z</div></div>'
        )
        assert _count_grid_children(page) == 3

    def test_no_grid_is_zero(self) -> None:
        assert _count_grid_children('<section class="hero"><h1>x</h1></section>') == 0

    def test_an_unclosed_grid_does_not_hang(self) -> None:
        assert _count_grid_children('<div class="grid"><div>a</div>') == 1


class TestTheContractAcceptsThem:
    def test_four_grid_items_pass_the_card_requirement(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text(FOUR_GRID_ITEMS, encoding="utf-8")
        verdict = check_artifacts([CARD_REQ], tmp_path, files=["index.html"])
        assert verdict.ok, verdict.unmet

    def test_two_grid_items_still_fail(self, tmp_path: Path) -> None:
        page = (
            '<div class="feature-grid">'
            '<div class="grid-item">A</div><div class="grid-item">B</div></div>'
        )
        (tmp_path / "index.html").write_text(page, encoding="utf-8")
        verdict = check_artifacts([CARD_REQ], tmp_path, files=["index.html"])
        assert not verdict.ok

    def test_the_named_classes_still_pass(self, tmp_path: Path) -> None:
        page = (
            '<div class="feature-grid">'
            '<div class="feature-card">A</div><div class="feature-card">B</div>'
            '<div class="feature-card">C</div></div>'
        )
        (tmp_path / "index.html").write_text(page, encoding="utf-8")
        assert check_artifacts([CARD_REQ], tmp_path, files=["index.html"]).ok

    def test_a_page_with_no_grid_fails(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text(
            '<section class="hero"><h1>x</h1></section>', encoding="utf-8"
        )
        assert not check_artifacts([CARD_REQ], tmp_path, files=["index.html"]).ok

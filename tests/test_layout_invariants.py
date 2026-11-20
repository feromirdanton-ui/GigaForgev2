"""The harness owns the page's structure; a child owns its look.

Measured live on the ``subagents`` mode: a section child emitted

    .feature-grid { display: flex; gap: 20px; justify-content: center; }

with no ``flex-wrap``.  Four cards sat in one 1440px row, each squeezed to a
quarter of the width with its text clipped.  The page "had four cards" by every
count and was unreadable — and the contract passed it, because the count was
right.

A child is asked to style its section.  It is not asked to decide that the page
has one row.  The harness emits the structural rules last, so they win on equal
specificity, and a child can still change colours, borders, shadows and type.
"""
from __future__ import annotations

from gigaforge.harness.generation import (
    _layout_invariants,
    assemble_page,
    plan_generation,
)

GOAL = (
    "Create a single-file index.html: a cyberpunk landing page about GigaForge. "
    "Requirements: neon cyan and magenta palette, a hero section with a glitch "
    "effect, a feature grid with four cards, and a footer."
)

FLEX_CHILD = (
    '<section class="features"><h2>Features</h2>'
    '<div class="feature-grid">'
    '<div class="feature-card">A</div><div class="feature-card">B</div>'
    '<div class="feature-card">C</div><div class="feature-card">D</div>'
    "</div></section>"
    "<style>.feature-grid { display: flex; gap: 20px; justify-content: center; }</style>"
)


def _page() -> str:
    plan = plan_generation(GOAL)
    return assemble_page(plan, {
        "hero": '<section class="hero"><h1>GigaForge</h1></section>',
        "features": FLEX_CHILD,
        "footer": '<div class="footer"><p>Footer</p></div>',
    })


class TestLayoutInvariants:
    def test_the_grid_is_a_grid(self) -> None:
        css = _layout_invariants()
        assert "display: grid" in css

    def test_the_grid_wraps(self) -> None:
        assert "auto-fit" in _layout_invariants()

    def test_cards_have_a_minimum_width(self) -> None:
        assert "minmax(240px" in _layout_invariants()

    def test_cards_can_shrink_without_clipping(self) -> None:
        css = _layout_invariants()
        assert "min-width: 0" in css
        assert "overflow-wrap" in css


class TestTheInvariantWins:
    def test_it_is_emitted_after_the_child_css(self) -> None:
        page = _page()
        assert "harness layout invariants" in page
        assert page.index("display: flex") < page.index("harness layout invariants"), (
            "the child's flex rule comes after the invariant and would win"
        )

    def test_it_overrides_a_child_flex_row(self) -> None:
        page = _page()
        tail = page[page.index("harness layout invariants"):]
        assert "display: grid !important" in tail

    def test_the_child_keeps_its_own_styling(self) -> None:
        page = _page()
        assert "justify-content: center" in page, (
            "the child's rule must survive; only the structure is overridden"
        )

    def test_the_page_still_has_one_style_block(self) -> None:
        assert _page().count("<style>") == 1

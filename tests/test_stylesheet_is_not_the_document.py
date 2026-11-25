"""The stylesheet is not the document.

**The defect this file exists for.**  A live `run` (agent mode) goal asked for
"a features grid with 4 cards, and a footer".  The page shipped four cards and
**no footer element** — and the contract passed it, `accepted: True`.

Two separate causes, both found by rendering the artifact rather than by
reading the code:

1. the plan-derived contract was only built when ``generation.sections`` was
   populated, which only the sectional path does.  `run` therefore fell back to
   ``extract_requirements``, which returns almost nothing for a page goal — one
   artifact judged by two standards, and the weaker one was the one people use;
2. once the contract *was* built, the footer requirement still passed, because
   the bare-word fallback matched ``footer { border-top: … }`` in the CSS.  The
   contract certified a page that had no footer at all.

The rule these tests pin: a contract asks what the **document** contains.  A
stylesheet rule is not an element, and a class name on a ``<div>`` is not a
landmark.
"""
from __future__ import annotations

import pytest

from gigaforge.harness.goal_contract import (
    Requirement,
    _without_style_and_script,
    check_artifacts,
)


# ── the stylesheet is removed before the document is read ───────────────────
def test_style_blocks_are_removed() -> None:
    html = "<html><head><style>footer { color: red }</style></head><body></body></html>"
    assert "footer" not in _without_style_and_script(html)


def test_script_blocks_are_removed() -> None:
    html = "<html><body><script>var footer = 1;</script></body></html>"
    assert "footer" not in _without_style_and_script(html)


def test_the_markup_survives() -> None:
    html = "<html><body><footer id=\"f\">x</footer></body></html>"
    assert "<footer" in _without_style_and_script(html)


# ── a CSS rule is not a footer ──────────────────────────────────────────────
def test_a_css_rule_does_not_satisfy_a_footer_requirement() -> None:
    """The live failure: `footer { border-top: … }` certified a page with no footer."""
    html = (
        "<html><head><style>"
        "footer { border-top: 1px solid rgba(255,255,255,.1); }"
        "section, footer, nav { padding: 4rem 1.5rem; }"
        "</style></head><body>"
        "<section id=\"hero\"><h1>GigaForge</h1></section>"
        "</body></html>"
    )
    req = Requirement("section", "section 'footer'", ("<footer", "footer"))
    assert not req.satisfied_by(html)


def test_a_real_footer_element_satisfies_it() -> None:
    html = (
        "<html><head><style>footer { color: red }</style></head><body>"
        "<footer id=\"footer\"><p>© 2026 GigaForge</p></footer>"
        "</body></html>"
    )
    req = Requirement("section", "section 'footer'", ("<footer", "footer"))
    assert req.satisfied_by(html)


def test_a_div_with_a_footer_class_is_not_a_landmark() -> None:
    """A landmark is an element, not a class name on a div."""
    html = (
        "<html><body><section id=\"hero\"><h1>x</h1></section>"
        "<div class=\"footer\"><p>©</p></div></body></html>"
    )
    req = Requirement("section", "section 'footer'", ("<footer", "footer"))
    # The class-name path still accepts it for a *section* named footer, which
    # is the documented behaviour for non-landmark sections; what must not
    # happen is the CSS matching.  This test pins the distinction so a future
    # change to the landmark rule is a decision, not an accident.
    assert req.satisfied_by(html)


# ── the end-to-end verdict ──────────────────────────────────────────────────
def _page(with_footer: bool) -> str:
    footer = (
        "<footer id=\"footer\"><p>© 2026 GigaForge. All rights reserved.</p></footer>"
        if with_footer
        else ""
    )
    return (
        "<!DOCTYPE html><html><head><title>GigaForge</title>"
        "<style>footer { border-top: 1px solid #333; }</style></head><body>"
        "<section id=\"hero\"><h1>GigaForge Agent Harness</h1>"
        "<p>A cyberpunk landing page with real prose about the harness.</p></section>"
        f"{footer}</body></html>"
    )


def test_a_page_without_a_footer_fails_the_contract(tmp_path) -> None:
    (tmp_path / "index.html").write_text(_page(with_footer=False))
    reqs = [Requirement("section", "section 'footer'", ("<footer", "footer"))]
    verdict = check_artifacts(reqs, tmp_path, ["index.html"])
    assert not verdict.ok
    assert any("footer" in u for u in verdict.unmet)


def test_a_page_with_a_footer_passes(tmp_path) -> None:
    (tmp_path / "index.html").write_text(_page(with_footer=True))
    reqs = [Requirement("section", "section 'footer'", ("<footer", "footer"))]
    assert check_artifacts(reqs, tmp_path, ["index.html"]).ok

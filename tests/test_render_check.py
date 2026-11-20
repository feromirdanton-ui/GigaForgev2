"""A page is a rendered artifact; the pipeline must render it.

**The defect.**  The contract counted tags.  A browser shows what the tags
produce, and the two disagreed twice on real runs:

* 1956 characters of CSS printed as text in the body, with a stray ``</style>``
  and a doubled grid and footer — every one of them invisible to a tag counter
  and to a size check;
* four cards in a row that overflowed the viewport, and four cards with zero
  width because the grid rules never applied — the page "had four cards" and
  was unreadable.

``render_check`` was a script nobody called.  It is now a module the settle node
gates on for artifact goals, and a missing browser is a recorded degradation
rather than a silent pass.

These tests pin the verdict logic (which is deterministic and needs no browser)
and the degradation contract.  The browser itself is exercised by the live run,
not by CI — a CI job that needs Chromium is a CI job that fails for reasons
unrelated to the code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.harness.render_check import render_facts, render_verdict


def _facts(**overrides) -> dict:
    base = {
        "available": True,
        "h1": "GigaForge",
        "css_leak_chars": 0,
        "style_blocks": 1,
        "footers": 1,
        "cards": 4,
        "zero_width_cards": 0,
        "horizontal_overflow": False,
        "text_chars": 900,
    }
    base.update(overrides)
    return base


class TestTheVerdict:
    def test_a_good_page_has_no_complaints(self) -> None:
        assert render_verdict(_facts(), min_cards=3) == []

    def test_css_rendered_as_text_is_caught(self) -> None:
        """The exact defect that passed review twice."""
        unmet = render_verdict(_facts(css_leak_chars=1956))
        assert any("CSS are rendered as text" in item for item in unmet)

    def test_a_missing_h1_is_caught(self) -> None:
        assert any("no visible <h1>" in item for item in render_verdict(_facts(h1=None)))

    def test_two_style_blocks_are_caught(self) -> None:
        assert any("<style> blocks" in item
                   for item in render_verdict(_facts(style_blocks=2)))

    def test_two_footers_are_caught(self) -> None:
        assert any("footers rendered" in item
                   for item in render_verdict(_facts(footers=2)))

    def test_too_few_cards_are_caught(self) -> None:
        assert any("expected at least 3" in item
                   for item in render_verdict(_facts(cards=1), min_cards=3))

    def test_zero_width_cards_are_caught(self) -> None:
        """Four cards with no box are not a grid."""
        unmet = render_verdict(_facts(zero_width_cards=4))
        assert any("zero width" in item for item in unmet)

    def test_horizontal_overflow_is_caught(self) -> None:
        assert any("overflows horizontally" in item
                   for item in render_verdict(_facts(horizontal_overflow=True)))

    def test_a_near_empty_page_is_caught(self) -> None:
        assert any("characters of visible text" in item
                   for item in render_verdict(_facts(text_chars=40)))

    def test_an_unavailable_browser_makes_no_claim(self) -> None:
        """Not rendered is not the same as rendered and fine."""
        assert render_verdict({"available": False, "reason": "no chromium"}) == []


class TestTheFacts:
    def test_a_missing_page_is_unavailable_not_an_exception(self, tmp_path: Path) -> None:
        facts = render_facts(tmp_path / "nope.html")
        assert facts["available"] is False
        assert "no such page" in facts["reason"]

    def test_a_real_page_renders(self, tmp_path: Path) -> None:
        """The browser path, skipped when Chromium is not installed."""
        page = tmp_path / "index.html"
        page.write_text(
            "<!DOCTYPE html><html><head><style>"
            "body{background:#05060a;color:#e8f6ff;font-family:sans-serif}"
            "h1{color:cyan;font-size:72px}"
            ".feature-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}"
            ".feature-card{padding:16px;border:1px solid cyan}"
            "</style></head><body>"
            "<h1>GigaForge</h1>"
            "<p>" + ("An agent harness that plans, acts and verifies. " * 8) + "</p>"
            '<div class="feature-grid">'
            '<div class="feature-card"><h3>Plan</h3><p>It plans.</p></div>'
            '<div class="feature-card"><h3>Act</h3><p>It acts.</p></div>'
            '<div class="feature-card"><h3>Verify</h3><p>It verifies.</p></div>'
            "</div>"
            '<footer class="footer"><p>GigaForge 2026</p></footer>'
            "</body></html>",
            encoding="utf-8",
        )
        facts = render_facts(page)
        if not facts.get("available"):
            pytest.skip(f"no browser: {facts.get('reason')}")
        assert facts["h1"] == "GigaForge"
        assert facts["cards"] == 3
        assert facts["zero_width_cards"] == 0
        assert facts["css_leak_chars"] == 0
        assert facts["style_blocks"] == 1
        assert render_verdict(facts, min_cards=3) == []

    def test_a_broken_page_is_caught_by_the_browser(self, tmp_path: Path) -> None:
        """CSS in the body: the defect a tag counter cannot see."""
        page = tmp_path / "broken.html"
        page.write_text(
            "<!DOCTYPE html><html><head><title>x</title></head><body>"
            "<h1>GigaForge</h1>"
            "<p>body { background: #05060a; color: #e8f6ff; margin: 0; padding: 0; }</p>"
            "<p>" + ("Some real prose that is long enough to count. " * 6) + "</p>"
            "</body></html>",
            encoding="utf-8",
        )
        facts = render_facts(page)
        if not facts.get("available"):
            pytest.skip(f"no browser: {facts.get('reason')}")
        assert facts["css_leak_chars"] > 0
        assert any("CSS are rendered as text" in item
                   for item in render_verdict(facts))

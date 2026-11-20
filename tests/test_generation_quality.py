"""Two quality defects a headless render found that no test could.

**1. The grid rules never applied.**  The base stylesheet styled ``.features``
while the assembler writes ``<div class="feature-grid">``.  Measured in headless
Chromium: four cards laid out as ``684px 684px 0px 0px 0px`` — the third and
fourth collapsed to zero width and were invisible.  The page "had four cards"
by every count and showed two.

**2. A child's diff leaked into the stylesheet.**  A section child answered with
a unified diff instead of a fragment, and the ``+`` markers went into
``<style>``::

    .features +            .feature-card { +                background-color: ...

Invalid CSS, so the browser dropped the rule and the section's styling vanished
while the page still looked assembled.
"""
from __future__ import annotations

from gigaforge.harness.generation import (
    _clean_css,
    _clean_markup,
    _default_css,
    _extract_css,
    assemble_page,
    plan_generation,
)

GOAL = (
    "Create a single-file index.html: a futuristic, cutting-edge cyberpunk "
    "landing page about the GigaForge agent harness. Requirements: neon cyan "
    "and magenta palette, a hero section with a glitch effect on the title, a "
    "feature grid with four cards, and a footer. Self-contained, no external "
    "assets."
)


class TestTheGridSelectorMatchesTheMarkup:
    def test_the_default_sheet_styles_the_grid_class_the_assembler_writes(self) -> None:
        css = _default_css("cyan", "magenta")
        assert ".feature-grid" in css, (
            "the assembler writes <div class='feature-grid'>; styling only "
            "'.features' leaves the grid rules unapplied"
        )

    def test_the_default_sheet_still_styles_the_plain_class(self) -> None:
        assert ".features" in _default_css("cyan", "magenta")

    def test_the_cards_are_styled_under_both_wrappers(self) -> None:
        css = _default_css("cyan", "magenta")
        assert ".feature-card" in css
        assert ".feature-item" in css

    def test_the_assembled_page_styles_its_own_grid(self) -> None:
        plan = plan_generation(GOAL)
        fragments = {
            "hero": '<section class="hero"><h1>GigaForge</h1></section>',
            "features": (
                '<div class="feature-grid">'
                '<div class="feature-card"><h3>A</h3></div>'
                '<div class="feature-card"><h3>B</h3></div>'
                '<div class="feature-card"><h3>C</h3></div>'
                '<div class="feature-card"><h3>D</h3></div></div>'
            ),
            "footer": '<div class="footer"><p>Footer</p></div>',
        }
        page = assemble_page(plan, fragments)
        assert 'class="feature-grid"' in page
        assert ".feature-grid" in page, (
            "the page uses a class its own stylesheet does not style"
        )


class TestDiffArtefactsDoNotReachThePage:
    def test_plus_markers_are_stripped_from_css(self) -> None:
        dirty = (
            ".features +            .feature-card { +                "
            "background-color: rgba(0, 128, 255, 0.1);\n+                color: #fff;\n }"
        )
        clean = _clean_css(dirty)
        # The marker after ``{`` is gone; the sibling combinator between the two
        # selectors is valid CSS and must survive.
        assert "{ +" not in clean
        assert "background-color" in clean
        assert "color: #fff" in clean
        assert ".features +            .feature-card" in clean

    def test_a_sibling_combinator_is_not_a_marker(self) -> None:
        assert _clean_css(".a + .b { color: red; }") == ".a + .b { color: red; }"

    def test_hunk_headers_are_dropped(self) -> None:
        assert "@@" not in _clean_css("@@ -1,3 +1,4 @@\n.a { color: red; }")

    def test_file_headers_are_dropped(self) -> None:
        dirty = "--- a/x.css\n+++ b/x.css\n.a { color: red; }"
        clean = _clean_css(dirty)
        assert "---" not in clean and "+++" not in clean
        assert ".a { color: red; }" in clean

    def test_a_css_custom_property_is_not_mistaken_for_a_marker(self) -> None:
        assert "--accent: cyan;" in _clean_css("--accent: cyan;")

    def test_clean_css_is_idempotent(self) -> None:
        once = _clean_css(".a { color: red; }")
        assert _clean_css(once) == once

    def test_markup_markers_are_stripped(self) -> None:
        dirty = "+<section class=\"hero\">\n+  <h1>Hi</h1>\n+</section>"
        clean = _clean_markup(dirty)
        assert "+" not in clean
        assert "<h1>Hi</h1>" in clean

    def test_extract_css_cleans_both_halves(self) -> None:
        fragment = (
            "+<section class=\"hero\"><h1>Hi</h1></section>\n"
            "<style>\n+.hero { color: cyan; }\n+</style>"
        )
        markup, css = _extract_css(fragment)
        assert "+" not in markup
        assert "+" not in css
        assert ".hero { color: cyan; }" in css
        assert "<h1>Hi</h1>" in markup

    def test_a_clean_fragment_is_untouched(self) -> None:
        fragment = '<section class="hero"><h1>Hi</h1></section><style>.hero{color:cyan}</style>'
        markup, css = _extract_css(fragment)
        assert markup == '<section class="hero"><h1>Hi</h1></section>'
        assert css == ".hero{color:cyan}"

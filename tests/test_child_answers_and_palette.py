"""A child's answer is not always markup, and a page's colours are in its CSS.

Three defects, all found by running an *edit* live rather than by reading code.

**1. A child answered with a diff.**  The footer child returned

    index 8d7f96b..c3a8eaa 100644
    --- a/index.html
    +++ b/index.html
    @@ -1,4 +1,4 @@
    -  </div>
    +<section class="footer">…</section>

and the harness glued the diff header and the context lines into the page.  The
run then failed its own render check with "6164 characters of CSS are rendered
as text" — the page contained a patch.  A diff is not markup; the added lines
are.

**2. An unknown section was given an invented name.**  ``existing_sections``
keyed a section the catalogue did not know as ``section1``, ``section2``… by
position.  That name then travelled: it became a *planned* section, so the
contract demanded a section called ``section1``, and the assembler — which has
no fragment and no fallback for a name it invented — could not produce it.  The
run failed with "section 'section1'; section 'section6'" on a page that was
structurally fine.

**3. The palette was read from prose.**  ``plan_generation`` scanned the whole
document for colour words, so "a green future" in a paragraph became a contract
requirement.  The run failed with "colour 'green'" for a page whose stylesheet
declares cyan and magenta.  This is the same defect as counting ``cyan`` in a
CSS comment, one level up.
"""
from __future__ import annotations

from gigaforge.harness.generation import (
    _added_lines_of,
    _looks_like_a_diff,
    _palette_of,
    _strip_document_wrapper,
    existing_sections,
    plan_generation,
)

DIFF_ANSWER = """index 8d7f96b..c3a8eaa 100644
--- a/index.html
+++ b/index.html
@@ -1,4 +1,4 @@
   </div>
 </section>
+<section class="footer">
+  <p>GigaForge 2026</p>
+</section>
"""


class TestADiffIsNotMarkup:
    def test_a_diff_is_recognised(self) -> None:
        assert _looks_like_a_diff(DIFF_ANSWER)

    def test_a_page_with_a_horizontal_rule_is_not_a_diff(self) -> None:
        """One marker is not enough: a page may contain ``---`` in a comment."""
        page = "<section><p>a</p></section>\n<!-- --- -->\n<p>b</p>"
        assert not _looks_like_a_diff(page)

    def test_only_added_lines_are_kept(self) -> None:
        added = _added_lines_of(DIFF_ANSWER)
        assert '<section class="footer">' in added
        assert "index 8d7f96b" not in added, "the diff header was kept"
        assert "@@" not in added, "the hunk header was kept"
        assert "</div>" not in added, "a context line from the OLD file was kept"

    def test_the_stripped_fragment_is_markup(self) -> None:
        fragment = _strip_document_wrapper(DIFF_ANSWER)
        assert fragment.startswith("<section")
        assert "index " not in fragment
        assert "@@" not in fragment

    def test_a_normal_fragment_is_untouched(self) -> None:
        fragment = _strip_document_wrapper('<section class="hero"><h1>Hi</h1></section>')
        assert fragment == '<section class="hero"><h1>Hi</h1></section>'

    def test_a_whole_document_is_still_reduced_to_its_body(self) -> None:
        document = "<!DOCTYPE html><html><head><title>x</title></head><body><p>y</p></body></html>"
        assert _strip_document_wrapper(document) == "<p>y</p>"


class TestAnUnknownSectionIsNotNamed:
    def test_a_known_section_is_found(self) -> None:
        found = existing_sections('<section class="hero"><h1>x</h1></section>')
        assert "hero" in found

    def test_an_unknown_section_is_not_given_a_positional_name(self) -> None:
        """The invented name became a contract requirement nothing could meet."""
        found = existing_sections('<section class="mystery"><p>x</p></section>')
        assert found == {}, f"invented a name: {list(found)}"

    def test_a_known_section_after_an_unknown_one_is_still_found(self) -> None:
        page = (
            '<section class="mystery"><p>a</p></section>'
            '<section class="hero"><h1>b</h1></section>'
        )
        found = existing_sections(page)
        assert set(found) == {"hero"}

    def test_the_plan_never_contains_an_invented_section(self) -> None:
        page = (
            '<section class="mystery"><p>a</p></section>'
            '<section class="hero"><h1>b</h1></section>'
        )
        plan = plan_generation("Add a footer", page)
        assert not any(kind.startswith("section") for kind in plan.kinds()), plan.kinds()


class TestThePaletteComesFromTheStylesheet:
    def test_a_colour_in_css_is_found(self) -> None:
        page = "<style>:root{--accent:cyan}</style><body>x</body>"
        assert "cyan" in _palette_of(page)

    def test_a_colour_in_prose_is_not(self) -> None:
        """'a green future' is not a palette."""
        page = "<style>:root{--accent:cyan}</style><p>a green future</p>"
        assert "green" not in _palette_of(page)

    def test_a_colour_in_a_class_name_is_not(self) -> None:
        page = '<style>:root{--accent:cyan}</style><div class="green-badge">x</div>'
        assert "green" not in _palette_of(page)

    def test_a_page_with_no_style_has_no_palette(self) -> None:
        assert _palette_of("<p>a green future</p>") == ()

    def test_the_plan_uses_the_stylesheet_palette(self) -> None:
        page = "<style>:root{--accent:cyan;--secondary:magenta}</style><p>a green future</p>"
        plan = plan_generation("Add a footer", page)
        assert "cyan" in plan.palette
        assert "green" not in plan.palette


class TestTheContactSectionExists:
    def test_a_contact_section_is_planned(self) -> None:
        plan = plan_generation("Add a contact section to index.html")
        assert "contact" in plan.kinds()

    def test_pricing_and_contact_together(self) -> None:
        plan = plan_generation(
            "Update index.html to add a pricing section with three tiers and a contact section."
        )
        kinds = plan.kinds()
        assert "pricing" in kinds
        assert "contact" in kinds

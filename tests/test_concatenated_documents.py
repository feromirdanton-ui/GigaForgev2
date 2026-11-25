"""Two whole documents in one file, and the doctype split across lines.

**Measured live** on the cyberpunk landing page (goal mode, cycle 3)::

    BEFORE bytes 4102  <html 2  <style 2
    line 2: DOCTYPE html>          <- the "<!" was on line 1
    line 3: <html lang="ru">
    ...
    line 81: </html>
    line 82: <html lang="en">      <- a second whole document
    line 155: </html>

The model answered an "add a section" step with a complete new page, and the
assembler concatenated it.  Every structural check then failed on a page that
was two pages: "2 stray <style> tag(s) outside <head>", "2 <head> elements",
"1403 characters of CSS are rendered as text".

The first repair attempt did nothing (``actions: []``) for two reasons, both
now covered by a test:

1. it looked for ``<!DOCTYPE`` as a document boundary, and the doctype was
   split across two lines, so it found only one boundary;
2. it treated the text before the first boundary as a document of its own, so
   the "first document" had no ``<body>`` and the merge bailed out.

A test that only checked "the output has one ``<html>``" would have passed on
the *input* of the second bug (one boundary found -> one span -> no change), so
these tests assert the merge actually happened and that content survived.
"""
from __future__ import annotations

from gigaforge.harness.html_structure import (
    audit_html_structure,
    repair_html_structure,
)

FIRST = """<!DOCTYPE html>
<html lang="ru">
<head>
<title>GigaForge</title>
<style>
body { background: #05050a; color: #0ff; }
.hero { font-size: 72px; }
</style>
</head>
<body>
<header><h1 class="glitch">GigaForge</h1></header>
<section class="features"><div class="feature-card">One</div></section>
<footer class="footer">Footer</footer>
</body>
</html>
"""

SECOND = """<html lang="en">
<head>
<title>GigaForge</title>
<style>
body { background: #05050a; color: #f0f; }
</style>
</head>
<body>
<section class="pricing"><h2>Pricing</h2></section>
</body>
</html>
"""


def _count(html: str, tag: str) -> int:
    return html.lower().count(f"<{tag}")


class TestConcatenatedDocuments:
    def test_two_documents_become_one(self) -> None:
        repaired, actions = repair_html_structure(FIRST + SECOND)
        assert _count(repaired, "html") == 1
        assert _count(repaired, "style") == 1
        assert any("merged" in a for a in actions), actions

    def test_the_second_documents_content_survives(self) -> None:
        """The merge must not be a truncation: the new section is the point."""
        repaired, _ = repair_html_structure(FIRST + SECOND)
        assert "pricing" in repaired
        assert "Pricing" in repaired

    def test_the_first_documents_content_survives(self) -> None:
        repaired, _ = repair_html_structure(FIRST + SECOND)
        assert "glitch" in repaired
        assert "feature-card" in repaired
        assert "Footer" in repaired

    def test_the_first_stylesheet_is_the_one_kept(self) -> None:
        """A page has one stylesheet; the first document's is the page's."""
        repaired, _ = repair_html_structure(FIRST + SECOND)
        assert "#0ff" in repaired
        assert "#f0f" not in repaired

    def test_the_result_passes_the_structural_audit(self) -> None:
        repaired, _ = repair_html_structure(FIRST + SECOND)
        assert audit_html_structure(repaired).ok

    def test_a_single_document_is_untouched(self) -> None:
        repaired, actions = repair_html_structure(FIRST)
        assert repaired == FIRST
        assert actions == []

    def test_three_documents_merge_into_one(self) -> None:
        third = SECOND.replace("pricing", "contact").replace("Pricing", "Contact")
        repaired, _ = repair_html_structure(FIRST + SECOND + third)
        assert _count(repaired, "html") == 1
        assert "pricing" in repaired and "contact" in repaired


class TestSplitDoctype:
    def test_a_doctype_split_across_lines_is_repaired(self) -> None:
        """Measured live: ``<!`` on one line, ``DOCTYPE html>`` on the next."""
        broken = FIRST.replace("<!DOCTYPE html>", "<!\nDOCTYPE html>")
        repaired, actions = repair_html_structure(broken)
        assert "<!DOCTYPE html>" in repaired
        assert any("doctype" in a for a in actions), actions

    def test_the_split_doctype_does_not_block_the_merge(self) -> None:
        """The first repair attempt found one boundary and did nothing."""
        broken = (FIRST + SECOND).replace("<!DOCTYPE html>", "<!\nDOCTYPE html>")
        repaired, actions = repair_html_structure(broken)
        assert _count(repaired, "html") == 1
        assert "pricing" in repaired
        assert any("merged" in a for a in actions), actions

    def test_a_well_formed_doctype_is_not_touched(self) -> None:
        repaired, actions = repair_html_structure(FIRST)
        assert "<!DOCTYPE html>" in repaired
        assert not any("doctype" in a for a in actions)

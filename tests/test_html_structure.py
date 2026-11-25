"""Structural HTML audit and repair.

Every test here is anchored to a defect that shipped.  The fixture
``BROKEN_PAGE`` is the body of a real page a live goal run produced and the
harness accepted: its stylesheet appears twice (once in ``<style>``, once as
raw text in ``<body>``), a stray ``</style>`` sits in the body, and the feature
grid and footer are each emitted twice.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.harness.html_structure import (
    audit_html_structure,
    duplicate_blocks,
    duplicate_landmarks,
    leaked_css_text,
    looks_like_css,
    repair_html_structure,
    visible_text,
)

BROKEN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>GigaForge</title>
    <style>
        body { background-color: #000; color: #fff; }
        .feature-card { border: 1px solid cyan; }
    </style>
</head>
<body>
        body {
            background-color: #000;
            color: #fff;
        }

        .feature-card {
            border: 1px solid cyan;
        }
    </style>
    <header class="hero"><h1>GigaForge</h1></header>
    <section class="features-grid">
        <div class="feature-card"><h2>Automation</h2><p>Agents do the work.</p></div>
        <div class="feature-card"><h2>Scale</h2><p>Handles the load.</p></div>
    </section>
    <footer><p>Powered by GigaForge.</p></footer>
    <section class="features-grid">
        <div class="feature-card"><h2>Automation</h2><p>Agents do the work.</p></div>
        <div class="feature-card"><h2>Scale</h2><p>Handles the load.</p></div>
    </section>
    <footer><p>Powered by GigaForge.</p></footer>
</body>
</html>
"""

CLEAN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>GigaForge</title>
    <style>
        body { background-color: #000; color: #fff; }
        .feature-card { border: 1px solid cyan; }
    </style>
</head>
<body>
    <header class="hero"><h1>GigaForge</h1></header>
    <section class="features-grid">
        <div class="feature-card"><h2>Automation</h2><p>Agents do the work.</p></div>
        <div class="feature-card"><h2>Scale</h2><p>Handles the load.</p></div>
    </section>
    <footer><p>Powered by GigaForge.</p></footer>
</body>
</html>
"""


class TestLooksLikeCss:
    def test_a_stylesheet_is_recognised(self) -> None:
        assert looks_like_css("body { color: #fff; background: #000; }")

    def test_prose_is_not_a_stylesheet(self) -> None:
        assert not looks_like_css(
            "Our agents adapt instantly to dynamic environments and keep working."
        )

    def test_prose_mentioning_one_declaration_is_not_a_stylesheet(self) -> None:
        # A sentence that happens to quote a declaration must not be deleted.
        assert not looks_like_css(
            "You can set color: red; in the theme file if you prefer a warmer look."
        )

    def test_short_brace_fragment_is_not_a_stylesheet(self) -> None:
        assert not looks_like_css("{a: b;}")


class TestLeakedCss:
    def test_css_at_the_very_start_of_body_is_found(self) -> None:
        """The live page put its leaked sheet before any tag.

        The first implementation matched ``>text<`` and therefore never saw a
        text node that had no tag before it — the audit reported the page clean
        while a browser printed 2000 characters of CSS as prose.
        """
        leaked = leaked_css_text(BROKEN_PAGE)
        assert leaked, "CSS at the start of <body> must be detected"
        assert "background-color" in leaked[0]

    def test_clean_page_has_no_leak(self) -> None:
        assert leaked_css_text(CLEAN_PAGE) == []

    def test_css_inside_style_is_not_a_leak(self) -> None:
        assert leaked_css_text(CLEAN_PAGE) == []


class TestDuplicateBlocks:
    def test_doubled_section_and_footer_are_found(self) -> None:
        found = dict(duplicate_blocks(BROKEN_PAGE))
        assert found.get("section") == 2
        assert found.get("footer") == 2

    def test_clean_page_has_no_duplicates(self) -> None:
        assert duplicate_blocks(CLEAN_PAGE) == []

    def test_two_different_sections_are_not_duplicates(self) -> None:
        page = (
            "<html><body>"
            "<section><h2>One</h2><p>First body of copy here.</p></section>"
            "<section><h2>Two</h2><p>Second body of copy here.</p></section>"
            "</body></html>"
        )
        assert duplicate_blocks(page) == []


class TestAudit:
    def test_broken_page_reports_every_defect(self) -> None:
        report = audit_html_structure(BROKEN_PAGE)
        assert not report.ok
        text = " ".join(report.issues())
        assert "stylesheet text leaked" in text
        assert "stray <style>" in text
        assert "<section> emitted 2 times" in text
        assert "<footer> emitted 2 times" in text

    def test_clean_page_is_ok(self) -> None:
        report = audit_html_structure(CLEAN_PAGE)
        assert report.ok, report.issues()

    def test_semantic_element_with_matching_class_is_one_landmark(self) -> None:
        page = "<html><body><footer class=\"footer\">Done</footer></body></html>"
        assert duplicate_landmarks(page) == []

    def test_style_after_html_root_is_reported_as_stray(self) -> None:
        page = CLEAN_PAGE + (
            "\n<style>@media (max-width: 600px) { "
            "nav ul { flex-direction: column; } }</style>"
        )
        report = audit_html_structure(page)
        assert not report.ok
        assert report.stray_style_tags == 2

    def test_duplicate_ids_and_nested_heads_are_reported(self) -> None:
        page = (
            "<html><head><head><title>Club</title></head></head><body>"
            '<section id="models"></section><section id="models"></section>'
            '<p>text mentioning id="models" is not an attribute.</p>'
            '<script>const text = \'<div id="models"></div>\';</script>'
            "</body></html>"
        )

        report = audit_html_structure(page)

        assert report.duplicate_ids == [("models", 2)]
        assert report.duplicate_heads == 1
        assert any("id 'models' appears 2 times" in issue for issue in report.issues())


class TestRepair:
    def test_repair_makes_the_broken_page_clean(self) -> None:
        repaired, actions = repair_html_structure(BROKEN_PAGE)
        assert actions, "a broken page must produce repair actions"
        assert audit_html_structure(repaired).ok, audit_html_structure(repaired).issues()

    def test_repair_keeps_the_real_content(self) -> None:
        repaired, _ = repair_html_structure(BROKEN_PAGE)
        text = visible_text(repaired)
        assert "GigaForge" in text
        assert "Agents do the work." in text
        assert "Powered by GigaForge." in text

    def test_repair_does_not_print_css_as_prose(self) -> None:
        repaired, _ = repair_html_structure(BROKEN_PAGE)
        assert "background-color" not in visible_text(repaired)

    def test_repair_keeps_exactly_one_section_and_footer(self) -> None:
        repaired, _ = repair_html_structure(BROKEN_PAGE)
        assert repaired.count("<section") == 1
        assert repaired.count("<footer") == 1

    def test_repair_keeps_the_stylesheet(self) -> None:
        repaired, _ = repair_html_structure(BROKEN_PAGE)
        assert "background-color" in repaired
        assert repaired.count("<style") == 1

    def test_clean_page_is_untouched(self) -> None:
        repaired, actions = repair_html_structure(CLEAN_PAGE)
        assert actions == []
        assert repaired == CLEAN_PAGE

    def test_repair_is_idempotent(self) -> None:
        once, _ = repair_html_structure(BROKEN_PAGE)
        twice, actions = repair_html_structure(once)
        assert actions == []
        assert twice == once

    def test_novel_rules_are_moved_not_dropped(self) -> None:
        """CSS that is not already in <style> is content, not duplication."""
        page = (
            "<html><head><style>body { color: #fff; }</style></head><body>"
            ".extra { border: 1px solid cyan; padding: 4px; }\n"
            "<p>Hello there, this is the page.</p></body></html>"
        )
        repaired, actions = repair_html_structure(page)
        assert "border: 1px solid cyan" in repaired
        assert "moved leaked stylesheet rules" in " ".join(actions)
        assert audit_html_structure(repaired).ok

    def test_trailing_stylesheet_is_moved_into_head(self) -> None:
        page = CLEAN_PAGE + (
            "\n<!-- mobile adjustment -->\n"
            "<style>\n@media (max-width: 600px) {\n"
            "  nav ul { flex-direction: column; }\n}\n"
        )

        repaired, actions = repair_html_structure(page)

        assert "moved trailing stylesheet rules into <head>" in actions
        assert repaired.count("<style") == 1
        assert repaired.index("@media (max-width: 600px)") < repaired.index("</head>")
        assert repaired.endswith("</html>\n<!-- mobile adjustment -->")
        assert audit_html_structure(repaired).ok
        twice, second_actions = repair_html_structure(repaired)
        assert twice == repaired
        assert second_actions == []


class TestVerifierIntegration:
    """The verifier must fail the page the harness used to accept."""

    def test_verifier_rejects_the_broken_page(self, tmp_path: Path) -> None:
        from gigaforge.core.goal import WorkspaceVerifier

        (tmp_path / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        verifier = WorkspaceVerifier(require_browser=False)
        report = verifier.verify(tmp_path, ["index.html"], "make a landing page")
        html_checks = [c for c in report.checks if c.name.startswith("HTML structure")]
        assert html_checks, "the HTML structure check must run"
        assert html_checks[0].status == "failed", html_checks[0].summary
        assert "stylesheet text leaked" in html_checks[0].summary

    def test_verifier_accepts_the_clean_page(self, tmp_path: Path) -> None:
        from gigaforge.core.goal import WorkspaceVerifier

        (tmp_path / "index.html").write_text(CLEAN_PAGE, encoding="utf-8")
        verifier = WorkspaceVerifier(require_browser=False)
        report = verifier.verify(tmp_path, ["index.html"], "make a landing page")
        html_checks = [c for c in report.checks if c.name.startswith("HTML structure")]
        assert html_checks and html_checks[0].status == "passed", html_checks[0].summary

    def test_verifier_accepts_the_repaired_page(self, tmp_path: Path) -> None:
        from gigaforge.core.goal import WorkspaceVerifier

        repaired, _ = repair_html_structure(BROKEN_PAGE)
        (tmp_path / "index.html").write_text(repaired, encoding="utf-8")
        verifier = WorkspaceVerifier(require_browser=False)
        report = verifier.verify(tmp_path, ["index.html"], "make a landing page")
        html_checks = [c for c in report.checks if c.name.startswith("HTML structure")]
        assert html_checks and html_checks[0].status == "passed", html_checks[0].summary

    def test_verifier_rejects_internal_links_without_targets(self, tmp_path: Path) -> None:
        from gigaforge.core.goal import WorkspaceVerifier

        page = CLEAN_PAGE.replace(
            "<header class=\"hero\">",
            "<nav><a href=\"#missing\">Missing section</a></nav>"
            "<header class=\"hero\">",
        )
        (tmp_path / "index.html").write_text(page, encoding="utf-8")
        verifier = WorkspaceVerifier(require_browser=False)
        report = verifier.verify(tmp_path, ["index.html"], "make a landing page")
        html_checks = [c for c in report.checks if c.name.startswith("HTML structure")]
        assert html_checks and html_checks[0].status == "failed"
        assert "internal links target missing ids: #missing" in html_checks[0].summary


class TestMutation:
    """Removing the audit must make the verifier accept the broken page again.

    This is the check that the integration test above is not passing for an
    unrelated reason: with the structural audit disabled, the same page must
    pass, which is exactly the bug that shipped.
    """

    def test_without_the_audit_the_broken_page_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gigaforge.harness.html_structure as module

        monkeypatch.setattr(
            module,
            "audit_html_structure",
            lambda _html: module.StructureReport(),
        )
        from gigaforge.core.goal import WorkspaceVerifier

        (tmp_path / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        verifier = WorkspaceVerifier(require_browser=False)
        report = verifier.verify(tmp_path, ["index.html"], "make a landing page")
        html_checks = [c for c in report.checks if c.name.startswith("HTML structure")]
        assert html_checks and html_checks[0].status == "passed", (
            "with the audit disabled the broken page must pass — otherwise this "
            "test proves nothing about the audit"
        )

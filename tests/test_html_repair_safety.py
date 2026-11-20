"""The repair must not destroy legitimate content.

Two defects, both measured on the real module:

* ``looks_like_css`` used ``punctuation / len > 0.02``, so a paragraph about an
  API — ``Our API returns {status: ok; latency: 12ms;}`` — was classified as a
  stylesheet, deleted from the body and pasted into ``<style>``.  The reader
  lost the sentence and the browser threw the rules away.
* ``duplicate_blocks`` compared blocks with every tag and attribute stripped,
  so ``<nav class="mobile">`` and ``<nav class="desktop">`` were "identical"
  and one of them was deleted — the ordinary responsive pattern.

The module's own docstring already stated the principle ("the cost of a false
positive is deleting a paragraph of the user's copy").  These tests make the
implementation hold to it, and they are paired: each false positive has a true
positive next to it, so a fix that simply disables the check fails too.
"""
from __future__ import annotations

import pytest

from gigaforge.harness.html_structure import (
    audit_html_structure,
    duplicate_blocks,
    looks_like_css,
    repair_html_structure,
)

PROSE_WITH_BRACES = (
    "Our API returns {status: ok; latency: 12ms;} for every call, and "
    "{retries: 3; timeout: 5s;} on failure."
)


class TestProseIsNotCss:
    def test_prose_with_braces_is_not_css(self) -> None:
        assert not looks_like_css(PROSE_WITH_BRACES)

    def test_prose_survives_repair(self) -> None:
        page = (
            "<html><head><style>body{color:#fff}</style></head><body>"
            f"<p>{PROSE_WITH_BRACES}</p><p>Second paragraph stays.</p>"
            "</body></html>"
        )
        repaired, actions = repair_html_structure(page)
        assert PROSE_WITH_BRACES in repaired, "the paragraph was deleted from the body"
        assert "Second paragraph stays." in repaired
        assert actions == []

    def test_prose_is_not_reported_as_leaked_css(self) -> None:
        page = f"<html><body><p>{PROSE_WITH_BRACES}</p></body></html>"
        assert audit_html_structure(page).leaked_css == []

    def test_a_sentence_with_one_brace_pair_is_not_css(self) -> None:
        assert not looks_like_css(
            "The config object looks like {debug: true} in the docs, which is fine."
        )

    def test_json_in_prose_is_not_css(self) -> None:
        assert not looks_like_css(
            'The response body is {"status": "ok", "latency": 12} for every call.'
        )


class TestRealCssIsStillCaught:
    def test_a_duplicate_of_the_existing_sheet_is_css(self) -> None:
        sheet = "body{color:#fff;background:#000}"
        assert looks_like_css(sheet, known_css=sheet)

    def test_duplicate_sheet_is_removed_from_the_body(self) -> None:
        sheet = "body{color:#fff;background:#000}"
        page = (
            f"<html><head><style>{sheet}</style></head><body>"
            f"{sheet}<p>Hello there friend.</p></body></html>"
        )
        repaired, actions = repair_html_structure(page)
        body = repaired.split("<body>", 1)[1]
        assert sheet not in body
        assert "Hello there friend." in body
        assert any("stylesheet run" in a for a in actions)

    def test_a_plain_stylesheet_is_css(self) -> None:
        assert looks_like_css("body { color: #fff; background: #000; }")

    def test_a_media_query_is_css(self) -> None:
        assert looks_like_css(
            "@media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }"
        )

    def test_a_keyframe_block_is_css(self) -> None:
        assert looks_like_css(
            "@keyframes glitch { 0% { transform: translate(0); } "
            "50% { transform: translate(2px, -2px); } 100% { transform: translate(0); } }"
        )


class TestDuplicateBlocksKeepAttributes:
    def test_mobile_and_desktop_nav_are_not_duplicates(self) -> None:
        page = (
            '<html><body>'
            '<nav class="mobile"><a href="#a">Home</a><a href="#b">Docs</a></nav>'
            '<nav class="desktop"><a href="#a">Home</a><a href="#b">Docs</a></nav>'
            "<p>Body text here.</p></body></html>"
        )
        assert duplicate_blocks(page) == []
        repaired, actions = repair_html_structure(page)
        assert repaired.count("<nav") == 2, "a responsive menu was deleted"
        assert actions == []

    def test_a_true_duplicate_is_still_removed(self) -> None:
        block = '<nav class="x"><a href="#a">Home</a><a href="#b">Docs</a></nav>'
        page = f"<html><body>{block}{block}<p>Body text here.</p></body></html>"
        assert duplicate_blocks(page) == [("nav", 2)]
        repaired, actions = repair_html_structure(page)
        assert repaired.count("<nav") == 1
        assert any("duplicate <nav>" in a for a in actions)

    def test_same_text_different_id_is_not_a_duplicate(self) -> None:
        page = (
            '<html><body>'
            '<section id="one"><h2>Features</h2><p>Fast and small.</p></section>'
            '<section id="two"><h2>Features</h2><p>Fast and small.</p></section>'
            "</body></html>"
        )
        assert duplicate_blocks(page) == []

    def test_whitespace_only_difference_is_a_duplicate(self) -> None:
        page = (
            "<html><body>"
            '<section class="a"><h2>Features</h2><p>Fast and small.</p></section>'
            '<section class="a">\n  <h2>Features</h2>\n  <p>Fast and small.</p>\n</section>'
            "</body></html>"
        )
        assert duplicate_blocks(page) == [("section", 2)]


class TestMutation:
    """The checks must be load-bearing: disabling them must fail these tests."""

    def test_repair_is_not_a_noop(self) -> None:
        sheet = "body{color:#fff;background:#000}"
        page = (
            f"<html><head><style>{sheet}</style></head><body>"
            f"{sheet}<p>Hello there friend.</p></body></html>"
        )
        repaired, actions = repair_html_structure(page)
        assert actions, "the repair did nothing on a page that needs it"
        assert repaired != page

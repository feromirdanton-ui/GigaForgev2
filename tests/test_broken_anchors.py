"""In-page anchors that point at nothing, and the mode that never checked.

**Measured live** on the cyberpunk landing page, agent (``run``) mode: the footer
linked ``#about``, ``#contact`` and ``#terms`` while the page defined exactly one
id, ``top``.  Three dead links, accepted as a success.

Two independent reasons the run passed, both now covered:

1. ``run`` attached no verifier at all — its report carried
   ``verification: None`` — while ``goal`` had always verified.  The same
   artifact was graded by two different standards, and the weaker one was the
   command most people use.
2. Neither the verifier's HTML audit nor the harness's structural audit looked
   at anchors.  ``_HtmlAudit`` collected ``internal_targets`` but nothing ever
   compared them to the ids; ``audit_html_structure`` never looked.

``href="#"`` is deliberately *not* a defect: it is the conventional "top of
page" link, and treating it as broken would put a permanent warning on correct
pages.
"""
from __future__ import annotations

from gigaforge.core.pipeline import _broken_anchors


def _page(body: str) -> str:
    return f"<!DOCTYPE html><html><body>{body}</body></html>"


class TestBrokenAnchors:
    def test_a_link_to_a_missing_id_is_reported(self) -> None:
        issues = _broken_anchors(_page('<a href="#about">About</a>'))
        assert len(issues) == 1
        assert "#about" in issues[0]

    def test_a_link_to_an_existing_id_is_fine(self) -> None:
        page = _page('<div id="about"></div><a href="#about">About</a>')
        assert _broken_anchors(page) == []

    def test_the_bare_hash_is_not_a_defect(self) -> None:
        """The conventional top-of-page link must not fail the gate."""
        assert _broken_anchors(_page('<a href="#">Top</a>')) == []

    def test_single_quotes_are_read(self) -> None:
        page = _page("<div id='about'></div><a href='#about'>A</a>")
        assert _broken_anchors(page) == []

    def test_several_missing_targets_are_each_named_once(self) -> None:
        page = _page(
            '<a href="#about">A</a><a href="#about">A2</a><a href="#contact">C</a>'
        )
        issues = _broken_anchors(page)
        assert len(issues) == 1
        assert issues[0].count("#about") == 1
        assert "#contact" in issues[0]

    def test_a_page_with_no_links_is_clean(self) -> None:
        assert _broken_anchors(_page("<h1>Hello</h1>")) == []

    def test_case_of_the_attribute_name_does_not_matter(self) -> None:
        page = _page('<div ID="about"></div><a HREF="#about">A</a>')
        assert _broken_anchors(page) == []


class TestTheGateIsWiredIntoThePipeline:
    """A helper nothing calls is not a fix."""

    def test_html_structure_check_includes_anchors(self) -> None:
        import inspect

        from gigaforge.core.pipeline import TaskPipeline

        source = inspect.getsource(TaskPipeline._check_html_structure)
        assert "_broken_anchors" in source, (
            "the anchor check must run inside the per-file structural check, "
            "or no mode sees it"
        )

    def test_run_attaches_the_same_verifier_as_goal(self) -> None:
        """``run`` graded the same artifact more weakly than ``goal``."""
        import inspect

        from gigaforge.cli.app import run as run_command

        source = inspect.getsource(run_command)
        assert "WorkspaceVerifier(" in source, (
            "run must verify; without a verifier its report says "
            "verification: None and every HTML defect passes"
        )

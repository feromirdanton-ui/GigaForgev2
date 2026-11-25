"""A section is identified by its NAME, not by the attribute that carries it.

Measured live on the site goal (HEAD aa5ac71): the assembler wrote
``<section id="features">`` while the contract's needle was
``class="features"``.  A page with a complete, correct feature section failed
with "section 'features'", and because the failure was identical every cycle
the goal stopped with "the same output and failure repeated" after 51 719
tokens.

The defect is the same class as the card-count one: the harness graded its own
vocabulary instead of the artifact.  These tests pin the class, not the one
example — every attribute a model plausibly uses must satisfy the requirement.
"""

from __future__ import annotations

import pytest

from gigaforge.harness.goal_contract import Requirement, check_artifacts


def _page(body: str) -> str:
    return (
        "<!doctype html><html><head><title>GigaForge</title>"
        "<style>body{background:#05060a;color:#e8f6ff}</style></head><body>"
        f"{body}</body></html>"
    )


@pytest.mark.parametrize(
    "markup",
    [
        '<section class="features"><h2>Features</h2></section>',
        '<section id="features"><h2>Features</h2></section>',
        '<div class="feature-grid" id="features"></div>',
        '<section class="section features"><h2>Features</h2></section>',
        '<section data-section="features"><h2>Features</h2></section>',
    ],
)
def test_a_section_name_counts_in_any_attribute(tmp_path, markup):
    (tmp_path / "index.html").write_text(_page(markup), encoding="utf-8")
    req = Requirement("section", "section 'features'", ("features",))
    verdict = check_artifacts([req], tmp_path, ["index.html"])
    assert verdict.ok, verdict.unmet


def test_a_missing_section_still_fails(tmp_path):
    """The fix must not turn the check into a no-op."""
    (tmp_path / "index.html").write_text(
        _page('<section class="hero"><h1>GigaForge</h1></section>'), encoding="utf-8"
    )
    req = Requirement("section", "section 'features'", ("features",))
    verdict = check_artifacts([req], tmp_path, ["index.html"])
    assert not verdict.ok
    assert any("features" in item for item in verdict.unmet)


def test_the_live_page_that_failed_now_passes(tmp_path):
    """The exact shape the failing run produced: id, not class."""
    page = _page(
        '<section class="hero"><h1>GigaForge</h1></section>'
        '<section id="features"><h2>Features</h2>'
        '<div class="feature-grid">'
        + "".join(
            f'<div class="feature-card"><h3>Feature {i}</h3>'
            f"<p>{'Real description of a capability. ' * 6}</p></div>"
            for i in range(1, 5)
        )
        + "</div></section>"
        '<footer><p>GigaForge agent harness, 2026. Built for autonomous work.</p></footer>'
    )
    (tmp_path / "index.html").write_text(page, encoding="utf-8")
    reqs = [
        Requirement("section", "section 'hero'", ("hero",)),
        Requirement("section", "section 'features'", ("features",)),
        Requirement("section", "section 'footer'", ("<footer", "footer")),
    ]
    verdict = check_artifacts(reqs, tmp_path, ["index.html"])
    assert verdict.ok, verdict.unmet

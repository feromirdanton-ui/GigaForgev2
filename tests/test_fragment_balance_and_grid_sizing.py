"""A fragment cannot leak its nesting into the page, and a grid sizes its items.

Two defects measured live on the site goal at HEAD 48b8ecf, both invisible to
every structural check because the markup was *valid*:

1. The hero child returned ``<section class="hero"><div class="container">…``
   closed with ``</div></div>`` — the container twice, the section never.  The
   assembler pasted it in verbatim, so the ``<section>`` stayed open for the
   rest of the document: the features section and the footer became its
   children, the hero measured 6232px tall, and the grid laid out at 56px per
   card.  The page "had four cards" and was unreadable.

2. The child wrote ``.feature-card { width: calc(25% - 20px) }``.  Inside a
   grid that resolves against the *column*, so four cards measured 56px wide
   and 2708px tall.

Both are the same class: the harness owned the structure in prose but not in
the emitted CSS, and it trusted a child's fragment to be self-contained.
"""

from __future__ import annotations

import re

import pytest

from gigaforge.harness.generation import (
    _balance_fragment,
    _layout_invariants,
    _strip_document_wrapper,
)


def test_an_unclosed_section_is_closed():
    fragment = (
        '<section class="hero"><div class="container">'
        "<h1>GigaForge</h1></div></div>"
    )
    balanced = _balance_fragment(fragment)
    assert balanced.count("<section") == balanced.count("</section>")
    assert balanced.count("<div") == balanced.count("</div>")
    assert balanced.endswith("</section>")


def test_an_unmatched_closing_tag_is_dropped():
    """The exact live shape: one ``</div>`` too many, no ``</section>``."""
    fragment = (
        '<section class="hero">\n'
        '    <div class="container">\n'
        "        <h1>Welcome to GigaForge</h1>\n"
        "    </div>\n"
        "    \n"
        "</div>"
    )
    balanced = _balance_fragment(fragment)
    assert balanced.count("</div>") == 1
    assert balanced.count("</section>") == 1
    assert balanced.rstrip().endswith("</section>")


def test_a_well_formed_fragment_is_unchanged():
    fragment = (
        '<section class="features"><h2>Features</h2>'
        '<div class="grid"><div class="feature-card"><h3>A</h3></div></div>'
        "</section>"
    )
    assert _balance_fragment(fragment) == fragment


def test_void_elements_are_not_counted():
    fragment = '<div><img src="a.png"><br><hr><p>x</p></div>'
    assert _balance_fragment(fragment) == fragment


def test_a_missing_inner_close_is_repaired():
    fragment = '<section class="features"><div class="grid"><p>x</section>'
    balanced = _balance_fragment(fragment)
    assert balanced.count("<div") == balanced.count("</div>")
    assert balanced.count("<section") == balanced.count("</section>")


def test_the_wrapper_strip_balances_too():
    """The public entry point must apply the invariant, not only the helper."""
    fragment = (
        "<html><body><section class=\"hero\"><div class=\"container\">"
        "<h1>GigaForge</h1></div></div></body></html>"
    )
    stripped = _strip_document_wrapper(fragment)
    assert stripped.count("<section") == stripped.count("</section>")
    assert "<html" not in stripped and "<body" not in stripped


def test_the_invariants_size_grid_items():
    """A card is sized by the grid, not by its own width."""
    css = _layout_invariants()
    assert ".grid, .row" in css
    assert "display: grid !important" in css
    # The item rule must neutralise a child's own width.  There are two
    # ``.feature-grid > *`` blocks — the early one only sets min-width — so the
    # assertion is on the LAST one, which is the invariant that wins.
    item_rules = re.findall(r"\.feature-grid > \*[^{]*\{([^}]*)\}", css, re.S)
    assert item_rules, "no grid-item rule emitted"
    body = item_rules[-1]
    assert "width: auto !important" in body
    assert "max-width: none !important" in body


def test_the_invariants_are_emitted_last():
    """An invariant that a child's later rule can override is not an invariant."""
    import inspect

    from gigaforge.harness import generation

    source = inspect.getsource(generation.assemble_page)
    assert source.index("_layout_invariants()") > source.index("child_css")


def test_the_palette_survives_an_inline_style():
    """A child's ``<h1 style="color:#fff">`` must not repaint the headline.

    Measured live: the page's h1 rendered white while ``--accent: cyan`` sat
    unused in ``:root``.  A stylesheet rule with ``!important`` beats an inline
    style without it, so the palette is restored without editing the child's
    markup.
    """
    css = _layout_invariants()
    assert "h1 { color: var(--accent) !important; }" in css
    assert "h2 { color: var(--secondary) !important; }" in css

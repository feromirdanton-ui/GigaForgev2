"""A colour counts only where colour is used, not where the word appears.

The live run produced a page whose only colours were ``#fff`` and ``#000`` for a
goal that asked for "neon cyan/magenta".  The contract passed anyway, because
the word "cyan" appeared in a CSS comment and "neon" was treated as a colour
word.  A contract that can be satisfied by prose is not a contract.
"""
from __future__ import annotations

from gigaforge.harness.goal_contract import extract_requirements

GOAL = (
    "Create a cyberpunk page: neon cyan/magenta palette, dark background, "
    "glitch hero title, feature grid, and a footer."
)


def _colour_reqs():
    return [r for r in extract_requirements(GOAL) if r.kind == "colour"]


def test_neon_is_not_a_colour_requirement() -> None:
    """`neon` is a style modifier; treating it as a colour let prose satisfy it."""
    labels = [r.label for r in _colour_reqs()]
    assert "colour 'neon'" not in labels
    assert "colour 'cyan'" in labels
    assert "colour 'magenta'" in labels


def test_a_colour_in_a_comment_does_not_satisfy_the_contract() -> None:
    page = (
        "<style>\n"
        "  /* cyan and magenta would be nice here */\n"
        "  body { color: #fff; background: #000; }\n"
        "</style>\n"
    )
    unmet = [r.label for r in _colour_reqs() if not r.satisfied_by(page)]
    assert "colour 'cyan'" in unmet
    assert "colour 'magenta'" in unmet


def test_a_colour_in_a_declaration_satisfies_the_contract() -> None:
    page = (
        "<style>\n"
        "  body { color: #0ff; background: #000; }\n"
        "  h1 { text-shadow: 0 0 8px #ff00ff; }\n"
        "</style>\n"
    )
    unmet = [r.label for r in _colour_reqs() if not r.satisfied_by(page)]
    assert unmet == []


def test_the_live_artifact_is_now_rejected() -> None:
    """The exact page the live run produced: #fff/#000 only, 'cyan' in a rule."""
    page = (
        "<style>\n"
        "  body { color: #fff; background-color: #000; }\n"
        "  .feature-box h2 { color: cyan; }\n"
        "</style>\n"
    )
    # `color: cyan` *is* a declaration, so cyan is genuinely satisfied here.
    unmet = [r.label for r in _colour_reqs() if not r.satisfied_by(page)]
    assert "colour 'cyan'" not in unmet
    # ...but magenta never appears in a declaration, so it is still unmet.
    assert "colour 'magenta'" in unmet

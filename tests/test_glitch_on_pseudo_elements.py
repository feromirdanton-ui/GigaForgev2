"""A glitch effect lives on the pseudo-elements, and the check must look there.

**The defect this file exists for.**  A live goal produced a page whose hero
title has a real glitch effect — the standard technique, two offset copies on
``::before`` and ``::after`` that shift against each other.  The render check
read only the ``<h1>``'s own ``animationName``, got ``none``, and reported the
page as having no glitch.

Measured on the artifact:

    h1_animation:        none
    h1_animation_pseudo: glitch glitch
    h1_has_glitch:       True

That is a false negative in the *measurement*, and its cost is not cosmetic: a
contract that rejects a correct page asks the model to fix something that is
not broken, which is how a run spends a cycle and comes out worse.

The rule: the pseudo-elements are part of the element's rendered appearance.
A check that reads only the element's own computed style is reading half of it.
"""
from __future__ import annotations

import pytest

from gigaforge.harness.render_check import render_facts

pytestmark = pytest.mark.skipif(
    not __import__("importlib").util.find_spec("playwright"),
    reason="playwright is not installed",
)


_GLITCH_ON_PSEUDO = """<!DOCTYPE html><html><head><title>GigaForge</title><style>
body { background: #05050a; color: #e8f6ff; font-family: sans-serif; }
h1 { font-size: 64px; color: cyan; position: relative; }
h1::before, h1::after {
  content: attr(data-text); position: absolute; left: 0; top: 0;
  animation: glitch 2s infinite;
}
@keyframes glitch { 0%,100% { transform: translateX(0) } 50% { transform: translateX(3px) } }
</style></head><body>
<section id="hero"><h1 data-text="GigaForge">GigaForge</h1>
<p>A cyberpunk landing page about the GigaForge agent harness, with real prose
in it so the visible-text check has something to count.</p></section>
</body></html>"""

_GLITCH_ON_ELEMENT = """<!DOCTYPE html><html><head><title>GigaForge</title><style>
body { background: #05050a; color: #e8f6ff; }
h1 { font-size: 64px; color: cyan; animation: glitch 2s infinite; }
@keyframes glitch { 0%,100% { transform: translateX(0) } 50% { transform: translateX(3px) } }
</style></head><body>
<section id="hero"><h1>GigaForge</h1>
<p>A cyberpunk landing page about the GigaForge agent harness, with real prose
in it so the visible-text check has something to count.</p></section>
</body></html>"""

_NO_GLITCH = """<!DOCTYPE html><html><head><title>GigaForge</title><style>
body { background: #05050a; color: #e8f6ff; }
h1 { font-size: 64px; color: cyan; }
</style></head><body>
<section id="hero"><h1>GigaForge</h1>
<p>A cyberpunk landing page about the GigaForge agent harness, with real prose
in it so the visible-text check has something to count.</p></section>
</body></html>"""


def _facts(tmp_path, html: str) -> dict:
    page = tmp_path / "index.html"
    page.write_text(html)
    return render_facts(page)


def test_a_glitch_on_the_pseudo_elements_is_detected(tmp_path) -> None:
    """The live case: the h1's own animation is `none`, the effect is real."""
    facts = _facts(tmp_path, _GLITCH_ON_PSEUDO)
    if not facts.get("available"):
        pytest.skip(f"render unavailable: {facts.get('reason')}")
    assert facts["h1_animation"] == "none"
    assert facts["h1_has_glitch"] is True


def test_a_glitch_on_the_element_is_detected(tmp_path) -> None:
    facts = _facts(tmp_path, _GLITCH_ON_ELEMENT)
    if not facts.get("available"):
        pytest.skip(f"render unavailable: {facts.get('reason')}")
    assert facts["h1_has_glitch"] is True


def test_a_page_without_a_glitch_is_not_reported_as_having_one(tmp_path) -> None:
    """The fix must not turn the check into a rubber stamp."""
    facts = _facts(tmp_path, _NO_GLITCH)
    if not facts.get("available"):
        pytest.skip(f"render unavailable: {facts.get('reason')}")
    assert facts["h1_has_glitch"] is False

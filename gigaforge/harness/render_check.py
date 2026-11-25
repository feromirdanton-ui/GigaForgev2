"""Render a page in a real browser and report what a browser actually shows.

**Why this is in the pipeline and not a script.**  Judging a generated page by
its byte count is how a broken page passed review twice: 1956 characters of CSS
printed as text in the body, a stray ``</style>``, a doubled grid and footer —
all invisible to a size check and to a tag counter.  The contract could count
``<footer`` and still accept a page whose footer was rendered as prose.

A page is a *rendered* artifact.  The only honest check is to render it.

The module is importable and returns a plain dict, so the pipeline can gate on
it and a test can assert on it without spawning a subprocess.  When Chromium is
not available the check reports ``available: False`` and the run records a
degradation — it does not silently pass, and it does not fail a run for a
missing browser.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["render_facts", "render_verdict", "PROBE"]

PROBE = """
() => {
  const cs = (el) => el ? getComputedStyle(el) : null;
  const h1 = document.querySelector('h1');
  const grid = (() => {
    // The grid is the element that actually lays out as a grid, not merely the
    // first element whose class mentions "grid" — a section wrapper can carry
    // the class too, and reporting its columns says nothing about the cards.
    const candidates = document.querySelectorAll(
      '.feature-grid, .features-grid, .features, [class*="grid"]'
    );
    for (const el of candidates) {
      if (getComputedStyle(el).display === 'grid') return el;
    }
    return candidates[0] || null;
  })();
  const cards = document.querySelectorAll(
    '.feature-card, .feature-item, .card, [class*="card"]'
  );
  const body = document.body;
  const text = (body ? body.innerText : '') || '';
  // CSS rendered as prose: a text node whose content is mostly braces and
  // declarations.  This is the defect a size check cannot see.
  let cssLeak = 0;
  const walker = document.createTreeWalker(body || document.documentElement,
                                            NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = (walker.currentNode.nodeValue || '').trim();
    if (t.length > 30 && t.includes('{') && t.includes('}') &&
        (t.match(/[{};:]/g) || []).length / t.length > 0.05) {
      cssLeak += t.length;
    }
  }
  const styles = document.querySelectorAll('style');
  // A card is only a card if it has a box.  Four cards in a row that overflow
  // the viewport, or four cards with zero width, are not a grid.
  let zeroWidthCards = 0;
  let minCardWidth = Infinity;
  for (const card of cards) {
    const w = card.getBoundingClientRect().width;
    if (w < 1) zeroWidthCards += 1;
    minCardWidth = Math.min(minCardWidth, w);
  }
  return {
    title: document.title,
    h1: h1 ? h1.innerText.trim().slice(0, 80) : null,
    h1_color: h1 ? cs(h1).color : null,
    h1_size: h1 ? cs(h1).fontSize : null,
    h1_animation: h1 ? cs(h1).animationName : null,
    // A glitch effect is almost always built on ::before/::after — the two
    // offset copies that shift against each other.  Reading only the h1's own
    // animationName reported "none" for a page that HAS a glitch, which cost a
    // live goal a whole extra cycle: the contract rejected a correct page and
    // the model was asked to fix something that was not broken.  The pseudo
    // elements are part of the element's rendered appearance, so they are read.
    h1_animation_pseudo: h1 ? (
      (getComputedStyle(h1, '::before').animationName || '') + ' ' +
      (getComputedStyle(h1, '::after').animationName || '')
    ).trim() : null,
    h1_has_glitch: h1 ? (
      /glitch/i.test(cs(h1).animationName || '') ||
      /glitch/i.test(getComputedStyle(h1, '::before').animationName || '') ||
      /glitch/i.test(getComputedStyle(h1, '::after').animationName || '') ||
      /glitch/i.test((h1.closest('[class*="glitch"]') || {}).className || '')
    ) : false,
    body_bg: body ? cs(body).backgroundColor : null,
    grid_display: grid ? cs(grid).display : null,
    grid_columns: grid ? cs(grid).gridTemplateColumns : null,
    cards: cards.length,
    zero_width_cards: zeroWidthCards,
    min_card_width: isFinite(minCardWidth) ? Math.round(minCardWidth) : 0,
    style_blocks: styles.length,
    css_leak_chars: cssLeak,
    text_chars: text.length,
    sections: document.querySelectorAll('section, .hero, .features, .footer').length,
    footers: document.querySelectorAll('footer, .footer').length,
    headers: document.querySelectorAll('header, .header').length,
    scroll_height: document.documentElement.scrollHeight,
    viewport_width: window.innerWidth,
    horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 2,
  };
}
"""


def render_facts(page_path: Path | str, *, screenshot: Path | str | None = None,
                 viewport: tuple[int, int] = (1440, 900)) -> dict[str, Any]:
    """Render *page_path* and return what the browser computed.

    Returns ``{"available": False, "reason": ...}`` when Playwright or its
    Chromium build is missing.  That is a *degradation*, not a pass: the caller
    records it and the report says the page was not rendered.
    """
    path = Path(page_path).resolve()
    if not path.is_file():
        return {"available": False, "reason": f"no such page: {path}"}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - an optional dependency
        return {"available": False, "reason": f"playwright not installed: {exc}"}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(
                viewport={"width": viewport[0], "height": viewport[1]}
            )
            page.goto(path.as_uri(), wait_until="load")
            page.wait_for_timeout(600)
            facts = page.evaluate(PROBE)
            if screenshot is not None:
                shot = Path(screenshot)
                shot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot), full_page=True)
                facts["screenshot"] = str(shot)
            browser.close()
    except Exception as exc:  # noqa: BLE001 - a browser that will not start
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    facts["available"] = True
    facts["bytes"] = path.stat().st_size
    return facts


def render_verdict(facts: dict[str, Any], *, min_cards: int = 0,
                   require_h1: bool = True) -> list[str]:
    """Turn rendered facts into a list of unmet requirements.

    Empty list means the page renders as a page.  Each entry names the fact and
    the value, so a rejection says *what the browser saw* rather than "the page
    is bad".
    """
    if not facts.get("available"):
        return []
    unmet: list[str] = []
    if require_h1 and not facts.get("h1"):
        unmet.append("rendered page has no visible <h1>")
    if int(facts.get("css_leak_chars") or 0) > 0:
        unmet.append(
            f"{facts['css_leak_chars']} characters of CSS are rendered as text"
        )
    if int(facts.get("style_blocks") or 0) > 1:
        unmet.append(f"{facts['style_blocks']} <style> blocks (expected 1)")
    if int(facts.get("footers") or 0) > 1:
        unmet.append(f"{facts['footers']} footers rendered (expected 1)")
    if min_cards and int(facts.get("cards") or 0) < min_cards:
        unmet.append(
            f"{facts.get('cards', 0)} cards rendered, expected at least {min_cards}"
        )
    if int(facts.get("zero_width_cards") or 0) > 0:
        unmet.append(
            f"{facts['zero_width_cards']} cards have zero width — the grid did not lay out"
        )
    if facts.get("horizontal_overflow"):
        unmet.append("the page overflows horizontally at 1440px")
    if int(facts.get("text_chars") or 0) < 200:
        unmet.append(
            f"only {facts.get('text_chars', 0)} characters of visible text"
        )
    return unmet


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m gigaforge.harness.render_check page.html [outdir]``."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: render_check <page.html> [outdir]", file=sys.stderr)
        return 2
    page = Path(args[0])
    out_dir = Path(args[1]) if len(args) > 1 else page.parent
    shot = out_dir / f"{page.stem}_render.png"
    facts = render_facts(page, screenshot=shot)
    print(json.dumps(facts, indent=2, ensure_ascii=False))
    return 0 if facts.get("available") else 1


if __name__ == "__main__":
    raise SystemExit(main())

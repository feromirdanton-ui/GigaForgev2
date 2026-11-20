"""Render a page in headless Chromium and report what a browser actually shows.

Judging a generated page by its byte count is how a broken page passed review
twice: 1956 characters of CSS printed as text in the body, a stray ``</style>``,
a doubled grid and footer — all invisible to a size check and to a tag counter.

This reports the facts a browser can see: the computed colour and size of the
headline, how many cards the grid lays out, whether any CSS text is rendered as
prose, and the page's own text length.  It writes a screenshot next to the page.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROBE = """
() => {
  const cs = (el) => el ? getComputedStyle(el) : null;
  const h1 = document.querySelector('h1');
  const grid = document.querySelector('.feature-grid, .features, [class*="grid"]');
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
  return {
    title: document.title,
    h1: h1 ? h1.innerText.trim().slice(0, 80) : null,
    h1_color: h1 ? cs(h1).color : null,
    h1_size: h1 ? cs(h1).fontSize : null,
    h1_animation: h1 ? cs(h1).animationName : null,
    body_bg: body ? cs(body).backgroundColor : null,
    grid_display: grid ? cs(grid).display : null,
    grid_columns: grid ? cs(grid).gridTemplateColumns : null,
    cards: cards.length,
    style_blocks: styles.length,
    css_leak_chars: cssLeak,
    text_chars: text.length,
    sections: document.querySelectorAll('section, .hero, .features, .footer').length,
    footers: document.querySelectorAll('footer, .footer').length,
    headers: document.querySelectorAll('header, .header').length,
    scroll_height: document.documentElement.scrollHeight,
  };
}
"""


def main() -> int:
    page_path = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else page_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    shot = out_dir / f"{page_path.stem}_render.png"

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(page_path.as_uri(), wait_until="load")
        page.wait_for_timeout(600)
        facts = page.evaluate(PROBE)
        page.screenshot(path=str(shot), full_page=True)
        browser.close()

    facts["screenshot"] = str(shot)
    facts["bytes"] = page_path.stat().st_size
    print(json.dumps(facts, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

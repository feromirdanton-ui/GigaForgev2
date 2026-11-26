"""Measure WCAG contrast and responsiveness on a rendered page.

The render check already answers "is the layout right".  It does not answer
"can a person read it", and those are different questions: a page with a
correct 4-card grid and #00ffcc on #0a0a12 is fine, while the same grid with
#00ffcc on #ffffff is unreadable and passes every structural check.

This module is deterministic: it reads the computed colours out of a headless
Chromium, computes the WCAG 2.1 contrast ratio, and reports the pairs that
fail.  No model is involved, so the number is the same every time.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: WCAG 2.1 thresholds.  4.5 is normal text, 3.0 is large text (>=18.66px
#: bold or >=24px) and UI component boundaries.
AA_NORMAL = 4.5
AA_LARGE = 3.0

_JS = r"""
() => {
  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
  };
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
    return 0.2126*f(c.r) + 0.7152*f(c.g) + 0.0722*f(c.b);
  };
  const bgOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.05) return c;
      n = n.parentElement;
    }
    return {r: 255, g: 255, b: 255, a: 1};
  };
  const out = [];
  const seen = new Set();
  document.querySelectorAll('h1,h2,h3,h4,p,li,a,button,span,label,td,th').forEach(el => {
    const text = (el.textContent || '').trim();
    if (!text || text.length < 2) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.1) return;
    const fg = parse(cs.color);
    if (!fg) return;
    const bg = bgOf(el);
    const size = parseFloat(cs.fontSize) || 16;
    const bold = (parseInt(cs.fontWeight) || 400) >= 700;
    const large = size >= 24 || (bold && size >= 18.66);
    const key = `${cs.color}|${bg.r},${bg.g},${bg.b}|${large}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({
      tag: el.tagName.toLowerCase(),
      sample: text.slice(0, 40),
      fg: cs.color, bg: `rgb(${bg.r},${bg.g},${bg.b})`,
      size, bold, large,
    });
  });
  return out;
}
"""


#: Element-wise overflow probe.
#:
#: **Why not ``scrollWidth``.**  ``documentElement.scrollWidth > innerWidth``
#: was the original metric and it is blind to exactly the defect it exists to
#: find: any ``overflow-x: hidden`` on ``html``/``body`` -- including the one
#: the harness itself used to inject -- makes ``scrollWidth`` equal
#: ``innerWidth``, because the content is *clipped*, not scrolled.  Reproduced
#: in a headless Chromium at 375x812 with a 1200px child: ``scrollWidth 375``,
#: ``innerWidth 375``, ``overflow_detected False``, while the child's right
#: edge sat at 1208px -- 833px of the page unreachable on a phone.
#:
#: So the measurement is per element: anything whose right edge is past the
#: viewport is reported, whether or not an ancestor clips it.  Clipping is the
#: defect (WCAG 1.4.10 Reflow requires reflow, not truncation), so a clipped
#: element must still count.
_OVERFLOW_JS = r"""
() => {
  const vw = window.innerWidth;
  const out = [];
  const all = document.querySelectorAll('body *');
  for (const el of all) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 && r.height <= 0) continue;
    // A right edge past the viewport is the defect.  A 1px tolerance absorbs
    // sub-pixel rounding, which is not a layout bug.
    if (r.right > vw + 1) {
      out.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className && String(el.className).slice(0, 60)) || '',
        right: Math.round(r.right),
        width: Math.round(r.width),
        over: Math.round(r.right - vw),
      });
    }
  }
  // Widest offenders first: the report should name the cause, not a symptom.
  out.sort((a, b) => b.over - a.over);
  return {viewport: vw, count: out.length, worst: out.slice(0, 5)};
}
"""


@dataclass
class ContrastPair:
    tag: str
    sample: str
    fg: str
    bg: str
    size: float
    large: bool
    ratio: float

    @property
    def required(self) -> float:
        return AA_LARGE if self.large else AA_NORMAL

    @property
    def passes(self) -> bool:
        return self.ratio >= self.required

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag, "sample": self.sample, "fg": self.fg, "bg": self.bg,
            "size": self.size, "large": self.large,
            "ratio": round(self.ratio, 2), "required": self.required,
            "passes": self.passes,
        }


@dataclass
class WcagReport:
    pairs: list[ContrastPair] = field(default_factory=list)
    overflow_375: bool = False
    overflow_1440: bool = False
    #: The elements that stick out, widest first.  A boolean alone says "there
    #: is a defect" and leaves the reader to find it; the offenders say which
    #: element and by how much, which is what a repair needs.
    overflow_375_offenders: list[dict[str, Any]] = field(default_factory=list)
    overflow_1440_offenders: list[dict[str, Any]] = field(default_factory=list)
    min_ratio: float = 0.0
    error: str = ""

    @property
    def failures(self) -> list[ContrastPair]:
        return [p for p in self.pairs if not p.passes]

    @property
    def ok(self) -> bool:
        return not self.error and not self.failures and not self.overflow_375

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": len(self.pairs),
            "failures": [p.to_dict() for p in self.failures],
            "min_ratio": round(self.min_ratio, 2),
            "overflow_375": self.overflow_375,
            "overflow_1440": self.overflow_1440,
            "overflow_375_offenders": list(self.overflow_375_offenders),
            "overflow_1440_offenders": list(self.overflow_1440_offenders),
            "error": self.error,
        }


def _channel(value: float) -> float:
    v = value / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = rgb
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.1 contrast ratio between two ``rgb()``/``rgba()`` colours."""
    def parse(value: str) -> tuple[float, float, float]:
        m = re.search(r"rgba?\(([^)]+)\)", value or "")
        if not m:
            return (0.0, 0.0, 0.0)
        parts = [float(x.strip()) for x in m.group(1).split(",")[:3]]
        while len(parts) < 3:
            parts.append(0.0)
        return (parts[0], parts[1], parts[2])

    l1 = _luminance(parse(fg))
    l2 = _luminance(parse(bg))
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def measure(page: Path, *, timeout_ms: int = 30000) -> WcagReport:
    """Render ``page`` at two widths and measure contrast + overflow."""
    report = WcagReport()
    if not Path(page).is_file():
        report.error = f"page not found: {page}"
        return report
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        report.error = f"playwright is not installed: {exc}"
        return report

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                view = browser.new_page(viewport={"width": 1440, "height": 900})
                view.goto(Path(page).resolve().as_uri(), timeout=timeout_ms)
                pairs = view.evaluate(_JS)
                wide = view.evaluate(_OVERFLOW_JS) or {}
                view.set_viewport_size({"width": 375, "height": 812})
                view.wait_for_timeout(200)
                narrow = view.evaluate(_OVERFLOW_JS) or {}
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - a render failure is a report, not a crash
        report.error = f"{type(exc).__name__}: {exc}"[:400]
        return report

    for row in pairs or []:
        ratio = contrast_ratio(row["fg"], row["bg"])
        report.pairs.append(ContrastPair(
            tag=row["tag"], sample=row["sample"], fg=row["fg"], bg=row["bg"],
            size=float(row["size"]), large=bool(row["large"]), ratio=ratio,
        ))
    report.overflow_1440 = bool(wide.get("count"))
    report.overflow_375 = bool(narrow.get("count"))
    report.overflow_1440_offenders = list(wide.get("worst") or [])
    report.overflow_375_offenders = list(narrow.get("worst") or [])
    report.min_ratio = min((p.ratio for p in report.pairs), default=0.0)
    return report


#: In-page repair.  For every element whose text fails AA, walk the foreground
#: toward black or white — whichever direction the background allows — until the
#: ratio clears the threshold, then write the colour onto the element.
#:
#: **Why in the DOM and not in the CSS text.**  The failing colour is usually an
#: inline style or a rule the model wrote for a specific button; a text-level
#: rewrite would have to parse CSS, resolve specificity and guess which rule
#: won.  The browser has already done all of that, so the computed colour is the
#: only thing worth changing — and setting it inline is exactly what a designer
#: would do to override it.
_REPAIR_JS = r"""
() => {
  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
  };
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
    return 0.2126*f(c.r) + 0.7152*f(c.g) + 0.0722*f(c.b);
  };
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    const hi = Math.max(l1, l2), lo = Math.min(l1, l2);
    return (hi + 0.05) / (lo + 0.05);
  };
  const bgOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.05) return c;
      n = n.parentElement;
    }
    return {r: 255, g: 255, b: 255, a: 1};
  };
  const fixed = [];
  document.querySelectorAll('h1,h2,h3,h4,p,li,a,button,span,label,td,th').forEach(el => {
    const text = (el.textContent || '').trim();
    if (!text || text.length < 2) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.1) return;
    const fg = parse(cs.color);
    if (!fg) return;
    // ``cs`` is a *live* CSSStyleDeclaration: reading ``cs.color`` after the
    // ``setProperty`` below returns the new value, so the report said
    // "rgb(51,51,51) -> rgb(51,51,51)" for a change that really happened.
    // Capture the original string now.
    const original = cs.color;
    const bg = bgOf(el);
    const size = parseFloat(cs.fontSize) || 16;
    const bold = (parseInt(cs.fontWeight) || 400) >= 700;
    const need = (size >= 24 || (bold && size >= 18.66)) ? 3.0 : 4.5;
    if (ratio(fg, bg) >= need) return;
    // Try both directions and keep the first that clears the threshold with the
    // least movement: a dark page should stay dark, a light one light.
    const candidates = [];
    for (let t = 0; t <= 1.0001; t += 0.05) {
      candidates.push({r: fg.r + (0 - fg.r) * t, g: fg.g + (0 - fg.g) * t, b: fg.b + (0 - fg.b) * t, t});
      candidates.push({r: fg.r + (255 - fg.r) * t, g: fg.g + (255 - fg.g) * t, b: fg.b + (255 - fg.b) * t, t});
    }
    let best = null;
    for (const c of candidates) {
      if (ratio(c, bg) >= need) { if (!best || c.t < best.t) best = c; }
    }
    if (!best) return;
    const css = `rgb(${Math.round(best.r)}, ${Math.round(best.g)}, ${Math.round(best.b)})`;
    el.style.setProperty('color', css, 'important');
    fixed.push({tag: el.tagName.toLowerCase(), sample: text.slice(0, 40), from: original, to: css});
  });
  return fixed;
}
"""


def repair(page: Path, *, out: Path | None = None, timeout_ms: int = 30000) -> dict[str, Any]:
    """Rewrite failing text colours in ``page`` until AA is met.

    Deterministic: the same page produces the same colours, because the search
    is a fixed walk over the luminance axis and the browser's computed values
    are the input.  Returns the list of changes so the caller can report them
    instead of silently editing the artifact.

    **The output is a new file by default.**  ``repair`` used to write
    ``view.content()`` back over the input: that is not "change a colour", it is
    a full re-serialisation of the DOM.  Formatting is normalised, comments
    outside the DOM are lost, and inline ``!important`` colours start fighting
    future CSS rules.  Acceptable for a throwaway artifact, wrong for a page
    someone will edit afterwards.  So the default target is ``<page>.fixed.html``
    and the caller is told where it went; ``out=page`` still means in place, but
    now it has to be asked for.

    Only ``color`` is touched — layout, structure and every other declaration
    are left alone, so a repair cannot introduce the overflow it was not asked
    to fix.
    """
    target = Path(out) if out else Path(page).with_suffix(".fixed.html")
    result: dict[str, Any] = {"fixed": [], "error": "", "out": str(target)}
    if not Path(page).is_file():
        result["error"] = f"page not found: {page}"
        return result
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        result["error"] = f"playwright is not installed: {exc}"
        return result

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                view = browser.new_page(viewport={"width": 1440, "height": 900})
                view.goto(Path(page).resolve().as_uri(), timeout=timeout_ms)
                result["fixed"] = view.evaluate(_REPAIR_JS) or []
                html = view.content()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - a render failure is a report, not a crash
        result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        return result

    target.write_text(html, encoding="utf-8")
    return result

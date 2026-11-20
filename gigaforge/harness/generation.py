"""Sectional generation: the fan-out that a "build me a page" goal needs.

**Why this module exists.**  ``--subagents`` was structurally unable to help a
generation goal.  Fan-out was gated on the goal naming two or three *existing*
files::

    named = [rel for raw in paths if (root / rel).is_file()]
    if len(named) > 3 or len(named) < 2:
        self._event("pipeline.subagents_skipped", reason="coordinated change or too many files")
        return ""

A goal that says "create a cyberpunk landing page" names no existing file, so
``named`` is empty, the fan-out is skipped, and the run falls back to one model
call that writes the whole page in one shot.  Measured live: ``--subagents`` on
the site goal produced ``pipeline.subagents_skipped {"files": 1}`` and a 2564-byte
page with no feature grid — the mode was on and did nothing, silently.

The unit of decomposition for a *generation* goal is not a file, it is a
**section of the artifact**.  The harness can derive that plan deterministically
from the goal (a hero, a feature grid, a footer are named in the brief), give
each section to its own child, and assemble the result.  That is the same
ownership discipline the code fan-out uses, applied to the shape the work
actually has.

What the harness owns, and what the model owns:

* the harness decides **which sections exist** and **in what order** — that is
  a checklist, not a judgement;
* the harness owns the **document skeleton** (doctype, head, the single
  ``<style>`` block, the body wrapper) so no child can emit a second
  ``<html>``, a second ``<body>`` or a stylesheet in the body — the exact
  defects that shipped;
* the model writes **the copy and the markup of one section**, which is the
  part that genuinely needs a model.

Assembly is deterministic and idempotent: the same fragments always produce the
same page, and the page is structurally valid by construction rather than by
luck.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Section kinds the harness can plan, in the order a landing page uses them.
#: Each entry is ``(kind, trigger words, heading)``.  A kind is planned only
#: when the goal names it, so the harness never invents a section the user did
#: not ask for.
_SECTION_CATALOGUE: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("nav", ("nav", "navigation", "menu", "navbar"), "Navigation"),
    ("hero", ("hero", "headline", "banner", "above the fold"), "Hero"),
    ("features", ("feature", "features", "grid", "benefit", "benefits"), "Features"),
    ("how", ("how it works", "workflow", "pipeline", "steps", "process"), "How it works"),
    ("stats", ("stats", "metrics", "numbers", "benchmark"), "Numbers"),
    ("pricing", ("pricing", "plans", "tiers"), "Pricing"),
    ("testimonials", ("testimonial", "quote", "review"), "Testimonials"),
    ("faq", ("faq", "questions"), "FAQ"),
    ("cta", ("call to action", "cta", "get started", "sign up"), "Call to action"),
    ("footer", ("footer",), "Footer"),
)

#: A page with no named section still needs a shape.  This is the minimum a
#: landing page is, and it is what the brief almost always means.
_DEFAULT_SHAPE = ("hero", "features", "footer")

#: Words that mean "this goal produces a single self-contained page".
#:
#: ``site`` and ``html`` used to be in this tuple, and they are substrings of
#: *composite*, *parasite*, *opposite* and *html parser* — so "Fix the composite
#: index in db.py" was routed to landing-page generation and the model's correct
#: diff was discarded.  Routing is now decided by :mod:`gigaforge.harness.task_kind`
#: from the filesystem; this tuple survives only as the phrase list that module
#: consults when a goal names no path at all.
_PAGE_HINTS = (
    "landing page",
    "single-file",
    "single file",
    "index.html",
    "web page",
    "webpage",
    "website",
    "web site",
    "static page",
    "home page",
)


@dataclass(frozen=True)
class SectionPlan:
    """One section of the artifact, and what the child is asked to write."""

    kind: str
    heading: str
    index: int

    @property
    def anchor(self) -> str:
        return f"<!-- section:{self.kind} -->"


@dataclass
class GenerationPlan:
    """The harness's decision about the artifact's shape."""

    path: str
    sections: list[SectionPlan] = field(default_factory=list)
    palette: tuple[str, ...] = ()
    title: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.sections)

    def kinds(self) -> list[str]:
        return [section.kind for section in self.sections]


def is_page_goal(goal: str, repo_root: "Path | str | None" = None) -> bool:
    """True when the goal asks for a self-contained page rather than a code change.

    Delegates to :func:`gigaforge.harness.task_kind.classify_task`, which decides
    from the filesystem instead of from a substring.  The old implementation was
    ``any(hint in low for hint in _PAGE_HINTS)`` with ``"site"`` and ``"html"``
    in the tuple, so "Fix the composite index in db.py" was a page goal and the
    correct diff was thrown away.

    ``repo_root`` is optional for callers that only have the text; without it
    the decision falls back to explicit page phrases, which is the conservative
    direction (no tree to check → do not invent an artifact).
    """
    from gigaforge.harness.task_kind import is_artifact_goal

    return is_artifact_goal(goal, repo_root)


def plan_generation(goal: str) -> GenerationPlan:
    """Derive the artifact's shape from the goal, deterministically.

    The plan is a checklist of what the brief names.  It is deliberately
    conservative: a section appears only when the goal mentions it, and a goal
    that names none gets the minimum landing-page shape rather than an empty
    plan.
    """
    low = (goal or "").lower()
    chosen: list[str] = []
    for kind, triggers, _heading in _SECTION_CATALOGUE:
        if any(re.search(rf"\b{re.escape(trigger)}\b", low) for trigger in triggers):
            chosen.append(kind)
    if not chosen:
        chosen = list(_DEFAULT_SHAPE)
    # A page always ends with a footer; a brief that names sections but not a
    # footer still needs one, and adding it is not inventing content.
    if "footer" not in chosen:
        chosen.append("footer")
    # Keep catalogue order so the page reads top to bottom.
    order = {kind: index for index, (kind, _t, _h) in enumerate(_SECTION_CATALOGUE)}
    chosen.sort(key=lambda kind: order.get(kind, 99))

    headings = {kind: heading for kind, _t, heading in _SECTION_CATALOGUE}
    sections = [
        SectionPlan(kind=kind, heading=headings.get(kind, kind.title()), index=index)
        for index, kind in enumerate(chosen)
    ]

    palette = tuple(
        word for word in ("cyan", "magenta", "purple", "violet", "orange", "green",
                          "red", "blue", "yellow", "pink", "gold")
        if re.search(rf"\b{word}\b", low)
    )

    path = "index.html"
    match = re.search(r"\b([\w./-]+\.html?)\b", goal or "", re.I)
    if match:
        candidate = match.group(1).replace("\\", "/").lstrip("./")
        if candidate and " " not in candidate:
            path = candidate

    title = _derive_title(goal)
    return GenerationPlan(path=path, sections=sections, palette=palette, title=title)


def _derive_title(goal: str) -> str:
    """A page title taken from the goal, not invented."""
    match = re.search(r"\babout\s+(?:the\s+)?([A-Z][\w-]*(?:\s+[A-Z][\w-]*){0,3})", goal or "")
    if match:
        return match.group(1).strip()
    match = re.search(r"\b([A-Z][\w-]*(?:\s+[A-Z][\w-]*){0,2})\s+(?:landing|page|site)", goal or "")
    if match:
        return match.group(1).strip()
    return "Landing page"


def section_prompt(plan: GenerationPlan, section: SectionPlan, goal: str) -> str:
    """The instruction a child receives: one section, nothing else.

    The child is told the document skeleton is already handled, so it cannot
    emit a second ``<html>``/``<body>``/``<style>`` — the defects that shipped
    came from a model writing a whole document when it was asked for a part.
    """
    palette = ", ".join(plan.palette) if plan.palette else "the palette the brief implies"
    return (
        f"Write ONE section of a single-file landing page: the {section.heading} "
        f"section (kind: {section.kind}).\n\n"
        f"Overall brief: {goal.strip()}\n\n"
        "Rules — the harness assembles the document, so you must NOT write a "
        "document:\n"
        "* return ONLY the markup for this one section, starting with "
        f"`<section class=\"{section.kind}\">` (or `<footer>` for the footer, "
        "`<nav>` for navigation);\n"
        "* do NOT emit <!DOCTYPE>, <html>, <head>, <body> or <style> — the "
        "harness owns them and a second copy breaks the page;\n"
        "* do NOT repeat any other section;\n"
        f"* use the palette: {palette};\n"
        "* write real, specific copy about the subject — no lorem ipsum, no "
        "\"Feature 1\", no placeholder text;\n"
        "* self-contained: no external images, fonts or scripts; draw with CSS.\n\n"
        "Return the markup and nothing else."
    )


def _strip_document_wrapper(fragment: str) -> str:
    """Reduce a child's answer to the section markup it was asked for.

    A child that ignores the instruction and returns a whole document is the
    common failure, and it is exactly what produced the doubled stylesheet.  The
    harness keeps the body content and drops everything else, so the assembly
    cannot inherit a second document.
    """
    text = (fragment or "").strip()
    if not text:
        return ""
    # Drop a fenced wrapper if the model added one.
    fence = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    body = re.search(r"<body\b[^>]*>(.*?)</body\s*>", text, re.S | re.I)
    if body:
        text = body.group(1).strip()
    # Remove any document-level tags the child emitted.
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</?(?:html|head|body)\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<style\b[^>]*>.*?</style\s*>", "", text, flags=re.S | re.I)
    text = re.sub(r"<meta\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<title\b[^>]*>.*?</title\s*>", "", text, flags=re.S | re.I)
    return text.strip()


def assemble_page(plan: GenerationPlan, fragments: dict[str, str], *, css: str = "") -> str:
    """Assemble the final page from per-section fragments.

    The skeleton is the harness's, so the result is structurally valid by
    construction: one doctype, one ``<html>``, one ``<head>``, one ``<style>``,
    one ``<body>``, and each section exactly once in catalogue order.
    """
    palette = plan.palette or ("cyan", "magenta")
    accent = palette[0]
    secondary = palette[1] if len(palette) > 1 else palette[0]
    body_parts: list[str] = []
    for section in plan.sections:
        fragment = _strip_document_wrapper(fragments.get(section.kind, ""))
        if not fragment:
            continue
        body_parts.append(f"    {section.anchor}\n    {fragment}")

    base_css = css.strip() or _default_css(accent, secondary)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '    <meta charset="UTF-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"    <title>{_escape(plan.title)}</title>\n"
        "    <style>\n"
        f"{base_css}\n"
        "    </style>\n"
        "</head>\n"
        "<body>\n"
        + "\n".join(body_parts)
        + "\n</body>\n</html>\n"
    )


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _default_css(accent: str, secondary: str) -> str:
    """A dark, neon base sheet the sections can rely on.

    The harness supplies this so every child writes markup against a known
    palette instead of each inventing its own — and so the page has a coherent
    look even when a child returns bare markup.
    """
    return f"""        :root {{
            --accent: {accent};
            --secondary: {secondary};
            --bg: #05060a;
            --fg: #e8f6ff;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            background: var(--bg);
            color: var(--fg);
            font-family: "Segoe UI", system-ui, sans-serif;
            line-height: 1.6;
        }}
        section, footer, nav {{ padding: 4rem 1.5rem; }}
        h1, h2 {{ line-height: 1.15; margin: 0 0 1rem; }}
        h1 {{ font-size: clamp(2.5rem, 6vw, 4.5rem); color: var(--accent); }}
        h2 {{ font-size: clamp(1.5rem, 3vw, 2.25rem); color: var(--secondary); }}
        a {{ color: var(--accent); }}
        .hero {{ min-height: 70vh; display: grid; place-content: center; text-align: center; }}
        .features {{ display: grid; gap: 1.5rem; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
        .feature-card {{
            border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
            border-radius: 12px;
            padding: 1.5rem;
            background: rgba(255, 255, 255, 0.03);
        }}
        footer {{ border-top: 1px solid rgba(255, 255, 255, 0.1); text-align: center; }}"""

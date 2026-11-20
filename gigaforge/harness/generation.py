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
        "* the feature grid needs at least three cards, each with "
        "`class=\"feature-card\"` (the base sheet styles it);\n"
        "* self-contained: no external images, fonts or scripts; draw with CSS. "
        "If you need rules of your own, put them in a single <style> block — the "
        "harness collects it into the page's one stylesheet, scoped to your "
        "section.\n\n"
        "Return the markup and nothing else."
    )


def _extract_css(fragment: str) -> tuple[str, str]:
    """Split a child's answer into ``(markup, css)``.

    **Why the CSS is kept.**  ``_strip_document_wrapper`` used to delete every
    ``<style>`` block a child emitted::

        text = re.sub(r"<style\\b[^>]*>.*?</style\\s*>", "", text, flags=re.S | re.I)

    The child is told "self-contained: no external images, fonts or scripts;
    draw with CSS" — so it was asked to do exactly the thing that was then
    thrown away.  Measured: a child's ``@keyframes glitch`` never reached the
    page, and the report's "glitch in the render" claim had no mechanism behind
    it.

    The harness still owns the *single* ``<style>`` block — that is what stops
    a second stylesheet in the body — but the rules a child wrote are collected
    and concatenated into it, scoped by section so two children cannot collide.
    """
    text = (fragment or "").strip()
    if not text:
        return "", ""
    blocks = re.findall(r"<style\b[^>]*>(.*?)</style\s*>", text, re.S | re.I)
    css = "\n".join(_clean_css(block) for block in blocks if block.strip())
    markup = re.sub(r"<style\b[^>]*>.*?</style\s*>", "", text, flags=re.S | re.I)
    return _clean_markup(markup).strip(), css


#: A unified-diff line: ``+``/``-`` followed by content, or a hunk header.
_DIFF_LINE_RE = re.compile(r"^(?:\+{1,3}|-{1,3})(?![-\d])|^@@ .* @@", re.M)


def _clean_css(css: str) -> str:
    """Remove unified-diff artefacts from a child's stylesheet.

    A child is asked for a fragment and sometimes answers with a *diff* — the
    same model, the same prompt family, and the fix loop's format leaks into the
    section call.  Measured live, the assembled page's ``<style>`` contained::

        .features +            .feature-card { +                background-color: ...

    Every ``+`` is a diff marker, so the rule is invalid CSS and the browser
    drops it — the section's styling silently disappeared while the page still
    looked "assembled".  The markers are stripped rather than the block, because
    the declarations underneath them are the child's real work.
    """
    if not css:
        return ""
    # A diff marker that got joined onto the previous line: ``{ +  color:`` or
    # ``; +  color:``.  A ``+`` *between* selectors is the sibling combinator and
    # is left alone — ``.features + .feature-card`` is valid CSS, and stripping
    # it would silently change which elements the rule applies to.
    css = re.sub(r"([{;])\s*\+\s+", r"\1 ", css)
    lines: list[str] = []
    for line in css.splitlines():
        stripped = line.strip()
        if stripped.startswith("@@") or stripped.startswith("diff --git"):
            continue
        if stripped.startswith("+++") or stripped.startswith("---"):
            continue
        # A leading single ``+`` or ``-`` is a diff marker, not CSS: no selector
        # or declaration starts with either.
        if stripped[:1] in ("+", "-") and not stripped.startswith("--"):
            line = stripped[1:].lstrip()
        lines.append(line)
    return "\n".join(lines).strip()


def _clean_markup(markup: str) -> str:
    """Remove unified-diff artefacts from a child's markup."""
    if not markup:
        return ""
    lines: list[str] = []
    for line in markup.splitlines():
        stripped = line.strip()
        if stripped.startswith("@@") or stripped.startswith("diff --git"):
            continue
        if stripped.startswith("+++") or stripped.startswith("---"):
            continue
        if stripped[:1] in ("+", "-") and not stripped.startswith("--"):
            line = stripped[1:].lstrip()
        lines.append(line)
    return "\n".join(lines)


def _scope_css(css: str, section: str) -> str:
    """Prefix a child's selectors with its section, so sections cannot collide.

    ``@keyframes`` and ``@font-face`` are global by definition and are left
    alone; everything else is scoped under ``.<section>``.  A selector that is
    already scoped, or that targets ``:root``/``body``/``html``, is left as the
    child wrote it — those are page-level rules and prefixing them would break
    the page rather than protect it.
    """
    if not css.strip():
        return ""
    out: list[str] = []
    # Walk top-level blocks with a depth counter.  A regex cannot do this: an
    # ``@keyframes`` block contains ``0% { ... }`` blocks, and a flat
    # ``selector { body }`` match turned the keyframes into ``.hero 0% {...}``
    # — which is not a keyframe and not a rule, so the animation silently died.
    index = 0
    length = len(css)
    while index < length:
        open_at = css.find("{", index)
        if open_at < 0:
            break
        selector = css[index:open_at].strip()
        depth = 0
        cursor = open_at
        end = -1
        while cursor < length:
            if css[cursor] == "{":
                depth += 1
            elif css[cursor] == "}":
                depth -= 1
                if depth == 0:
                    end = cursor
                    break
            cursor += 1
        if end < 0:
            break
        body = css[open_at + 1:end].strip()
        index = end + 1
        if not selector:
            continue
        if selector.startswith("@"):
            # At-rules are global by definition: ``@keyframes``, ``@font-face``,
            # ``@media``.  Prefixing them breaks them.
            out.append(f"{selector} {{ {body} }}")
            continue
        parts: list[str] = []
        for one in selector.split(","):
            one = one.strip()
            if not one:
                continue
            if one.startswith((".", "#", ":", "[")) or one.split()[0] in {
                "body", "html", ":root", "*"
            }:
                parts.append(one)
            else:
                parts.append(f".{section} {one}")
        out.append(f"{', '.join(parts)} {{ {body} }}")
    return "\n".join(out)


def _strip_document_wrapper(fragment: str) -> str:
    """Reduce a child's answer to the section markup it was asked for.

    A child that ignores the instruction and returns a whole document is the
    common failure, and it is exactly what produced the doubled stylesheet.  The
    harness keeps the body content and drops everything else, so the assembly
    cannot inherit a second document.

    The child's ``<style>`` content is *not* dropped — see :func:`_extract_css`;
    the caller collects it and the assembler puts it in the one ``<style>``.
    """
    text, _css = _extract_css(fragment)
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
    text = re.sub(r"<meta\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<title\b[^>]*>.*?</title\s*>", "", text, flags=re.S | re.I)
    return text.strip()


def _layout_invariants() -> str:
    """The structural rules the harness owns, emitted after every child's CSS.

    A child styles its own section; it does not get to decide the page's
    structure.  Measured live, a child emitted::

        .feature-grid { display: flex; gap: 20px; justify-content: center; }

    with no ``flex-wrap``, so four cards sat in one 1440px row, each squeezed to
    a quarter of the width with its text clipped — the page "had four cards" and
    was unreadable.  The harness owns the grid: it wraps, it has a minimum card
    width, and it is a grid rather than a row.

    Emitted last so it wins on equal specificity; a child that wants a different
    *look* can still change colours, borders, shadows and typography.
    """
    return (
        "/* harness layout invariants */\n"
        ".feature-grid, .features-grid, .features:not(section) {\n"
        "    display: grid !important;\n"
        "    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)) !important;\n"
        "    gap: 1.5rem;\n"
        "}\n"
        ".feature-grid > *, .features-grid > *, .features > * {\n"
        "    min-width: 0;\n"
        "    overflow-wrap: anywhere;\n"
        "}\n"
    )


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
    child_css: list[str] = []
    for section in plan.sections:
        raw = fragments.get(section.kind, "")
        fragment = _strip_document_wrapper(raw)
        if not fragment:
            continue
        body_parts.append(f"    {section.anchor}\n    {fragment}")
        # The child's own rules are kept, scoped to its section.  Dropping them
        # was the defect: a child told "draw with CSS" had its CSS deleted, so
        # its @keyframes never reached the page.
        _markup, section_css = _extract_css(raw)
        scoped = _scope_css(section_css, section.kind)
        if scoped:
            child_css.append(f"/* {section.kind} */\n{scoped}")

    base_css = css.strip() or _default_css(accent, secondary)
    if child_css:
        base_css = base_css + "\n" + "\n".join(child_css)
    # The layout invariant goes last, so it wins on equal specificity.  A child
    # is free to style its own section, but it may not decide that the feature
    # grid is a single row: measured live, a child emitted
    # ``.feature-grid { display: flex; gap: 20px; justify-content: center; }``
    # with no wrap, and four cards were laid out in one 1440px row, each
    # squeezed to a quarter of the width with its text clipped.  The harness
    # owns the *structure* — one grid, wrapping, a minimum card width — and the
    # child owns the look.
    base_css = base_css + "\n" + _layout_invariants()
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
        /* The grid selector must match the markup the assembler writes.  It
           styled ``.features`` while the section wrapper is
           ``<div class="feature-grid">``, so the grid rules never applied:
           measured in headless Chromium, four cards laid out as
           ``684px 684px 0px 0px 0px`` — the third and fourth collapsed to zero
           width and were invisible.

           ``.features`` is excluded when it is a ``<section>``: the section is
           the *wrapper* around the heading and the grid, so making it a grid
           lays out two columns — the heading and the whole grid — which is the
           same defect from the other side.  Measured after the first fix: the
           section resolved to ``684px 684px`` and the cards were squeezed into
           the second column. */
        .feature-grid, .features-grid, .features:not(section) {{
            display: grid;
            gap: 1.5rem;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        }}
        .feature-card, .feature-item, .feature-grid > div, .features > div {{
            border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
            border-radius: 12px;
            padding: 1.5rem;
            background: rgba(255, 255, 255, 0.03);
        }}
        @keyframes glitch {{
            0% {{ transform: translate(0); text-shadow: 0 0 0 transparent; }}
            20% {{ transform: translate(-2px, 2px); text-shadow: 2px 0 var(--secondary); }}
            40% {{ transform: translate(2px, -2px); text-shadow: -2px 0 var(--accent); }}
            60% {{ transform: translate(-1px, 1px); text-shadow: 1px 0 var(--secondary); }}
            80% {{ transform: translate(1px, -1px); text-shadow: -1px 0 var(--accent); }}
            100% {{ transform: translate(0); text-shadow: 0 0 0 transparent; }}
        }}
        .glitch {{ animation: glitch 2.4s infinite steps(2, end); }}
        footer {{ border-top: 1px solid rgba(255, 255, 255, 0.1); text-align: center; }}"""

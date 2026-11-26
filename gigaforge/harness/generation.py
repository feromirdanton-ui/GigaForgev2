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
    (
        "features",
        ("feature", "features", "grid", "benefit", "benefits", "model", "models",
         "card", "cards", "spotlight"),
        "Features",
    ),
    ("events", ("event", "events", "community"), "Events and community"),
    ("how", ("how it works", "workflow", "pipeline", "steps", "process"), "How it works"),
    ("stats", ("stats", "metrics", "numbers", "benchmark"), "Numbers"),
    ("pricing", ("pricing", "plans", "tiers"), "Pricing"),
    ("testimonials", ("testimonial", "quote", "review"), "Testimonials"),
    ("faq", ("faq", "questions"), "FAQ"),
    ("contact", ("contact", "get in touch", "reach us", "membership", "form"), "Contact"),
    ("cta", ("call to action", "cta", "get started", "sign up"), "Call to action"),
    ("footer", ("footer",), "Footer"),
)

#: A page with no named section still needs a shape.  This is the minimum a
#: landing page is, and it is what the brief almost always means.
_DEFAULT_SHAPE = ("hero", "features", "footer")

#: Words that mean "this goal produces a single self-contained page".
#:
#: The phrase list lives in :mod:`gigaforge.harness.task_kind` and is consulted
#: there when a goal names no path at all.  It used to be duplicated here, and
#: the duplicate was the defect: ``"site"`` is a substring of *composite*,
#: *parasite*, *opposite* and *html parser*, so "Fix the composite index in
#: db.py" was routed to landing-page generation and the model's correct diff was
#: discarded.  Two copies of a rule is two chances to fix one of them.


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
    #: The goal this plan was made for.  The contract reads the *number* the
    #: user wrote ("a grid with 4 cards") from it, so a page that ships three
    #: is rejected instead of passing against the harness's default of three.
    goal: str = ""
    #: Section words the goal used that the catalogue does not know.
    #:
    #: **Why this is a field and not a log line.**  When the goal named sections
    #: and none of them matched, the planner silently fell back to
    #: ``_DEFAULT_SHAPE`` and built a hero/features/footer page for a goal that
    #: asked for something else entirely.  The run then passed its contract,
    #: because the contract was derived from the same wrong plan — a closed loop
    #: that cannot notice it is wrong.  Recording the words makes the mismatch a
    #: fact the caller can refuse on.
    unresolved_sections: tuple[str, ...] = ()
    #: True when the plan is the default shape because the goal named nothing
    #: the catalogue recognises.  A caller that cares can refuse; a caller that
    #: does not gets the old behaviour, but now it is a decision.
    used_default_shape: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.sections)

    @property
    def card_count(self) -> int:
        """How many cards the goal asked for, or 0 when it did not say.

        The number is in the goal's own words ("a feature grid with four
        cards"), so the harness can read it instead of asking a model to count
        what it wrote.  Zero means "the goal did not say", and a page whose
        plan says nothing about cards is left exactly as the child wrote it.
        """
        from gigaforge.harness.goal_contract import requested_card_count

        return requested_card_count(self.goal)

    def kinds(self) -> list[str]:
        return [section.kind for section in self.sections]


#: Words that name a *section* without being in the catalogue.  Used only to
#: report a mismatch, never to invent a section: a word here means "the goal
#: asked for something the harness does not know how to build", which is a fact
#: worth refusing on rather than a shape worth guessing.
#:
#: Generic containers ("section", "block", "panel", "table") are deliberately
#: absent.  "a pricing table" is a pricing section, and listing "table" here
#: reported a mismatch on a goal the catalogue fully covers — a false alarm that
#: would teach a caller to ignore the field.
_SECTION_NOUNS = (
    "changelog", "roadmap", "timeline", "gallery", "portfolio", "team",
    "about", "blog", "news", "docs", "documentation", "login", "signup",
    "dashboard", "sidebar", "carousel", "slider", "accordion", "changelog",
)


def _unresolved_section_words(low: str, chosen: list[str]) -> list[str]:
    """Section nouns the goal used that the plan does not cover.

    A goal that says "add a pricing table and a changelog" names two sections;
    the catalogue knows ``pricing`` and not ``changelog``.  The plan covers
    pricing and silently drops the changelog, and the contract — derived from
    the same plan — is satisfied by a page with no changelog.  Reporting the
    word is what lets the caller refuse instead of shipping half the request.
    """
    if not low:
        return []
    covered = set(chosen)
    missing: list[str] = []
    for noun in _SECTION_NOUNS:
        if not re.search(rf"\b{re.escape(noun)}\b", low):
            continue
        # A noun is covered when a chosen section's triggers include it.
        for kind, triggers, _heading in _SECTION_CATALOGUE:
            if kind in covered and noun in triggers:
                break
        else:
            if noun not in missing:
                missing.append(noun)
    return missing


def plan_generation(goal: str, existing_html: str = "") -> GenerationPlan:
    """Derive the artifact's shape from the goal, deterministically.

    The plan is a checklist of what the brief names.  It is deliberately
    conservative: a section appears only when the goal mentions it, and a goal
    that names none gets the minimum landing-page shape rather than an empty
    plan.

    ``existing_html`` is the page being **edited**.  When it is given, the plan
    is the union of the sections already on the page and the sections the goal
    asks for, in catalogue order — so "add a footer and a hero section" to a
    page that already has a hero and a grid plans ``hero, features, footer``
    and the child is only asked for what is missing.  Without this, an edit
    either regenerated the whole page (losing what was there) or fell through
    to the code path and was graded by a weaker contract.
    """
    low = (goal or "").lower()
    chosen: list[str] = []
    for kind, triggers, _heading in _SECTION_CATALOGUE:
        if any(re.search(rf"\b{re.escape(trigger)}\b", low) for trigger in triggers):
            chosen.append(kind)

    present: list[str] = []
    if existing_html:
        present = list(existing_sections(existing_html))
        for kind in present:
            if kind not in chosen:
                chosen.append(kind)

    if not chosen:
        # The goal named no section the catalogue knows.  Two very different
        # situations look identical here: "make me a landing page" (the default
        # shape is right) and "add a pricing table and a changelog" (the default
        # shape is wrong, and building it silently is how a run passes a
        # contract derived from its own mistake).  The words the goal used are
        # recorded so the caller can tell them apart.
        chosen = list(_DEFAULT_SHAPE)
        used_default = True
    else:
        used_default = False
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

    palette = _palette_from_goal(low)
    if not palette and existing_html:
        # An edit that names no palette keeps the page's own colours rather
        # than repainting it with the default.  The palette is read from the
        # page's CSS declarations, which is a string operation.
        palette = _palette_of(existing_html)

    path = "index.html"
    match = re.search(r"\b([\w./-]+\.html?)\b", goal or "", re.I)
    if match:
        candidate = match.group(1).replace("\\", "/").lstrip("./")
        if candidate and " " not in candidate:
            path = candidate

    title = _derive_title(goal)
    if title == "Landing page" and existing_html:
        found = re.search(r"<title\b[^>]*>(.*?)</title\s*>", existing_html, re.S | re.I)
        if found and found.group(1).strip():
            title = found.group(1).strip()
    return GenerationPlan(
        path=path, sections=sections, palette=palette, title=title, goal=goal,
        unresolved_sections=tuple(_unresolved_section_words(low, chosen)),
        used_default_shape=used_default,
    )


#: A top-level section of an existing page: ``<section class="hero">…</section>``,
#: ``<footer>…</footer>``, ``<nav>…</nav>``.  Matched with a depth counter so a
#: nested ``<section>`` inside a section does not end the outer one early.
_SECTION_OPEN_RE = re.compile(
    r"<(section|footer|nav|header)\b([^>]*)>", re.I
)


def existing_sections(html: str) -> dict[str, str]:
    """The top-level sections of an existing page, keyed by kind.

    The kind is the first class token that the catalogue knows (``hero``,
    ``features``, ``footer``…), falling back to the tag name for the landmarks
    that have one (``footer``, ``nav``).  This is how an edit keeps what the page
    already has: the assembler reuses these fragments for any section the child
    was not asked to rewrite.

    **A section the catalogue does not know is not given an invented name.**
    The first version keyed it ``section1``, ``section2``… by position, and that
    name then travelled: it became a *planned* section, so the contract demanded
    a section called ``section1``, and the assembler — which has no fragment and
    no fallback for a name it invented — could not produce it.  Measured live on
    an edit: the run failed with "the artifact did not meet the goal: section
    'section1'; section 'section6'" on a page that was structurally fine.

    An unknown section is still *preserved* — :func:`assemble_page` appends it
    verbatim — but it is not a promise the contract can hold the run to.
    """
    out: dict[str, str] = {}
    if not html:
        return out
    known = {kind for kind, _t, _h in _SECTION_CATALOGUE}
    for match in _SECTION_OPEN_RE.finditer(html):
        tag = match.group(1).lower()
        attrs = match.group(2) or ""
        class_match = re.search(r'class\s*=\s*["\']([^"\']*)["\']', attrs, re.I)
        tokens = (class_match.group(1).split() if class_match else [])
        kind = next((t for t in tokens if t in known), "")
        if not kind:
            kind = {"footer": "footer", "nav": "nav", "header": "nav"}.get(tag, "")
        if not kind:
            # Not a section the catalogue knows: keep it, but do not name it.
            continue
        if kind in out:
            continue
        # Find the matching close tag with a depth counter.
        depth = 1
        cursor = match.end()
        token = re.compile(rf"<(/?){re.escape(tag)}\b[^>]*>", re.I)
        end = len(html)
        while depth and cursor < len(html):
            step = token.search(html, cursor)
            if step is None:
                break
            depth += -1 if step.group(1) else 1
            cursor = step.end()
            if depth == 0:
                end = step.end()
        out[kind] = html[match.start():end].strip()
    return out


#: Colour words a goal may name, mapped to the CSS colour the page should use.
#: A dict rather than a tuple of literals because the previous version was a
#: hard-coded 11-word list: "teal", "lime", "amber", "crimson" and every other
#: CSS colour name were silently ignored, and a goal that asked for a teal page
#: got the default cyan/magenta.  The values are CSS keywords, so a word that is
#: already a valid colour needs no translation.
_COLOUR_WORDS: dict[str, str] = {
    "cyan": "cyan", "magenta": "magenta", "purple": "purple", "violet": "violet",
    "orange": "orange", "green": "green", "red": "red", "blue": "blue",
    "yellow": "yellow", "pink": "pink", "gold": "gold", "teal": "teal",
    "lime": "lime", "amber": "amber", "crimson": "crimson", "indigo": "indigo",
    "turquoise": "turquoise", "aqua": "aqua", "fuchsia": "fuchsia",
    "emerald": "emerald", "scarlet": "scarlet", "azure": "azure",
    "coral": "coral", "salmon": "salmon", "lavender": "lavender",
    "maroon": "maroon", "navy": "navy", "olive": "olive", "silver": "silver",
    "bronze": "bronze", "copper": "copper", "neon": "cyan",
}

#: A hex colour in the goal, e.g. ``#00e5ff``.  A goal that names an exact
#: colour is more specific than one that names a word, so hex wins.
_HEX_COLOUR = re.compile(r"#([0-9a-f]{6}|[0-9a-f]{3})\b", re.I)


def _palette_from_goal(low: str) -> tuple[str, ...]:
    """The colours the goal names, in the order it names them.

    Two sources, in priority order:

    * **hex literals** — ``#00e5ff`` is unambiguous and the user chose it;
    * **colour words** — matched against :data:`_COLOUR_WORDS`, which is a dict
      so adding a colour is a one-line change rather than an edit to a tuple
      buried in a function.

    Order is the order of appearance in the goal, not the order of the dict:
    "magenta and cyan" must produce magenta first, because the first colour is
    the accent and the second is the secondary.
    """
    found: list[tuple[int, str]] = []
    for match in _HEX_COLOUR.finditer(low):
        found.append((match.start(), f"#{match.group(1).lower()}"))
    for word, colour in _COLOUR_WORDS.items():
        for match in re.finditer(rf"\b{re.escape(word)}\b", low):
            found.append((match.start(), colour))
    found.sort(key=lambda item: item[0])
    ordered: list[str] = []
    for _position, colour in found:
        if colour not in ordered:
            ordered.append(colour)
    return tuple(ordered)


def _palette_of(html: str) -> tuple[str, ...]:
    """The colours an existing page actually declares, in CSS.

    Read from the ``<style>`` blocks only.  Scanning the whole document was the
    same defect as counting ``cyan`` in a CSS *comment*: the word appears in
    prose ("a green future"), in a class name, in a link — and the contract then
    demanded a colour the page never used.  Measured live on an edit: the run
    failed with "colour 'green'" for a page whose stylesheet declares cyan and
    magenta.
    """
    if not html:
        return ()
    css = "\n".join(
        block.group(1) for block in re.finditer(r"<style\b[^>]*>(.*?)</style\s*>", html, re.S | re.I)
    )
    if not css:
        return ()
    return tuple(
        word for word in ("cyan", "magenta", "purple", "violet", "orange", "green",
                          "red", "blue", "yellow", "pink", "gold")
        if re.search(rf"\b{word}\b", css, re.I)
    )


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
    # The child cannot know its siblings: it sees one section and the brief.
    # Measured live: the hero wrote `<a href="#features">` while the features
    # section was emitted without an `id`, so the page shipped a dead anchor and
    # the contract rejected it after 17 752 tokens.  The harness knows every
    # section's anchor before any child runs, so it says so.
    #
    # Anchors only, never headings.  Naming a sibling's *heading* puts that
    # section's subject into every other child's prompt, and a child told
    # "Features" while writing the hero writes features — the exact defect that
    # shipped a hero containing a whole features grid.  An anchor is enough to
    # link to and carries no content to copy.
    siblings = [
        f"#{other.kind}"
        for other in plan.sections
        if other.kind != section.kind
    ]
    sibling_line = (
        "* the page's other sections, which you may link to by these exact "
        "anchors: " + ", ".join(siblings) + ";\n"
        if siblings
        else ""
    )
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
        f"{sibling_line}"
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
    """Remove unified-diff artefacts from a child's markup.

    A child is asked for a fragment and sometimes answers with a *diff* — the
    same model, the same prompt family, and the fix loop's format leaks into the
    section call.  Measured live on an edit, the footer child returned::

        index 8d7f96b..c3a8eaa 100644
        --- a/index.html
        +++ b/index.html
        @@ -1,4 +1,4 @@
           </div>
         </section>
        +<section class="footer">…</section>

    The line-by-line pass below strips the ``+``/``-`` markers, but it keeps the
    **context** lines — which are the *old* file — and the ``index …`` header.
    The page then contained a patch: the run failed its own render check with
    "6164 characters of CSS are rendered as text", and the closing tags were
    duplicated.

    So when the answer is a diff, only the added lines are markup.  The
    line-by-line pass stays for the mixed case (a fragment with a stray ``+``).
    """
    if not markup:
        return ""
    if _looks_like_a_diff(markup):
        return _added_lines_of(markup)
    lines: list[str] = []
    for line in markup.splitlines():
        stripped = line.strip()
        if stripped.startswith("@@"):
            continue
        if stripped.startswith("diff --git") or stripped.startswith("index "):
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

    **A child that answers with a diff is the other common failure.**  Measured
    live on an edit: the footer child returned

        index 8d7f96b..c3a8eaa 100644
        --- a/index.html
        +++ b/index.html
        @@ -1,4 +1,4 @@
        -  </div>
        +<section class="footer">…</section>

    and the harness glued the diff header and the context lines into the page —
    the run then failed its own render check with "6164 characters of CSS are
    rendered as text".  A diff is not markup; the added lines are.  Taking them
    is the difference between a page and a page with a patch pasted into it.

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
    if _looks_like_a_diff(text):
        text = _added_lines_of(text)
    body = re.search(r"<body\b[^>]*>(.*?)</body\s*>", text, re.S | re.I)
    if body:
        text = body.group(1).strip()
    # Remove any document-level tags the child emitted.
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</?(?:html|head|body)\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<meta\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<title\b[^>]*>.*?</title\s*>", "", text, flags=re.S | re.I)
    return _balance_fragment(text.strip())


#: Elements that never have a closing tag, so they must not be counted.
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

_TAG_RE_BALANCE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*?(/?)>", re.S)


def _balance_fragment(fragment: str) -> str:
    """Close the tags a child left open, and drop the ones it closed twice.

    Measured live on the site goal: the hero child returned

        <section class="hero"><div class="container">…</div></div>

    — it closed the container twice and never closed the section.  The
    assembler pasted that in verbatim, so the ``<section>`` stayed open for the
    rest of the document: the features section and the footer became its
    children, the hero measured 6232px tall, and the grid laid out at 56px per
    card.  Every structural check passed, because the markup was *valid* — it
    was just nested wrongly.

    A fragment is a self-contained piece of markup, so the harness makes it one:
    an unmatched closing tag is dropped, and a tag left open is closed at the
    end.  This is not a parser and does not try to be; it is the invariant that
    a fragment cannot leak its nesting into the page.
    """
    if not fragment:
        return ""
    # Comments and CDATA are not markup for this purpose.
    cleaned = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    cleaned = re.sub(r"<!\[CDATA\[.*?\]\]>", "", cleaned, flags=re.S)
    stack: list[str] = []
    out: list[str] = []
    cursor = 0
    for match in _TAG_RE_BALANCE.finditer(cleaned):
        out.append(fragment[cursor:match.start()])
        cursor = match.end()
        closing, name, self_closing = match.group(1), match.group(2).lower(), match.group(3)
        if name in _VOID_TAGS or self_closing:
            out.append(match.group(0))
            continue
        if closing:
            if name in stack:
                # Close everything opened after it, then it: a child that
                # forgot an inner ``</div>`` still gets a balanced result.
                while stack:
                    top = stack.pop()
                    out.append(f"</{top}>")
                    if top == name:
                        break
            # An unmatched closing tag is dropped: it would close a tag the
            # harness opened, which is how the hero swallowed the page.
            continue
        stack.append(name)
        out.append(match.group(0))
    out.append(fragment[cursor:])
    while stack:
        out.append(f"</{stack.pop()}>")
    text = "".join(out)
    # A child that mangled its own markup leaves fragments like ``/div>`` —
    # a closing tag with the ``<`` eaten.  Measured live on ``run``: the hero
    # fragment ended with ``</div>/div>`` and the stray text rendered as prose.
    text = re.sub(r"(?<![<\w])/([a-zA-Z][a-zA-Z0-9-]*)>", "", text)
    return text.strip()


#: A unified-diff header, a hunk header, or a ``--- a/…``/``+++ b/…`` pair.
_DIFF_MARKERS = (
    re.compile(r"^diff --git ", re.M),
    re.compile(r"^index [0-9a-f]{6,}\.\.[0-9a-f]{6,}", re.M),
    re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.M),
    re.compile(r"^--- a/", re.M),
    re.compile(r"^\+\+\+ b/", re.M),
)


def _looks_like_a_diff(text: str) -> bool:
    """Is this answer a patch rather than markup?

    Two independent markers, so a page that merely contains a line starting with
    ``---`` (a horizontal rule in a comment, say) is not mistaken for a patch.
    """
    return sum(1 for marker in _DIFF_MARKERS if marker.search(text)) >= 2


def _added_lines_of(patch: str) -> str:
    """The added lines of a unified diff, with the patch furniture removed.

    Only ``+`` lines are kept: the context lines are the *old* file, and pasting
    them in is what duplicated the closing tags.  ``+++ b/…`` is a header, not
    content.
    """
    kept: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("diff --git") or line.startswith("index "):
            continue
        if line.startswith("+"):
            kept.append(line[1:])
    return "\n".join(kept).strip()


def _drop_foreign_sections(fragment: str, kind: str, kinds: set[str]) -> str:
    """Remove a whole sibling section a child pasted into its own fragment.

    Measured live on ``run`` (the agent path): the hero child returned its own
    ``<section class="hero">`` **and** a complete
    ``<section class="features-grid">`` with a broken ``<!-- more cards h</div>>``
    comment.  The assembler pasted both in, so the page carried two feature
    sections — one with a single card, one with four — and the grid laid out
    ``1392px 0px 0px 0px 0px``: the first child took the whole width.

    A child is asked for one section.  A section it was not asked for is not
    its to write, and the harness already has a fragment for it.  Only a
    *top-level* section of another planned kind is dropped, so a nested
    ``<section>`` inside a card is left alone.
    """
    if not fragment or not kinds:
        return fragment
    others = {k for k in kinds if k and k != kind}
    if not others:
        return fragment
    pattern = re.compile(
        r"<section\b[^>]*\bclass\s*=\s*[\"'][^\"']*\b("
        + "|".join(re.escape(k) for k in sorted(others))
        + r")\b[^\"']*[\"'][^>]*>.*?</section\s*>",
        re.S | re.I,
    )
    return pattern.sub("", fragment).strip()


def _looks_like_markup(fragment: str) -> bool:
    """Does this fragment contain any HTML element at all?

    Measured live on ``goal`` at HEAD e00dd90: the footer child answered in
    Slim/HAML instead of HTML —

        section.class=footer
          h2
            = "Join the Future"

    — and the assembler pasted it in as text.  The page rendered the template
    source as prose and had no footer at all, while every structural check
    passed because the file was valid HTML *around* the garbage.

    A fragment with no element is not markup.  The harness cannot translate an
    arbitrary template language, and it must not ship the source: the plan
    promised a footer, so the fallback fills it.
    """
    if not fragment:
        return False
    return bool(re.search(r"<[a-zA-Z][a-zA-Z0-9-]*\b[^>]*>", fragment))


def _drop_dead_anchors(page: str) -> str:
    """Turn a link to a section that does not exist into plain text.

    Measured live on ``subagents`` at HEAD 4d52e0b: the footer child wrote
    ``<a href="#about">About</a>`` and ``<a href="#contact">Contact</a>``, and
    the plan has no such sections.  The page was rejected with "internal links
    target missing ids: #about, #contact" — correctly, because both links went
    nowhere, and the run ended needs_attention after 17 752 tokens.

    The harness knows every id on the page, so it can see the dead link.  It
    cannot invent the section the child meant, and it must not delete the
    label — the reader still needs to see "About".  So the anchor is unwrapped:
    the text stays, the link goes.
    """
    if not page:
        return page
    ids = set(re.findall(r"\bid\s*=\s*[\"']([^\"']+)[\"']", page))
    if not ids:
        return page

    def unwrap(match: re.Match[str]) -> str:
        target = match.group("target")
        if target in ids:
            return match.group(0)
        return match.group("label")

    return re.sub(
        r"<a\b[^>]*\bhref\s*=\s*[\"']#(?P<target>[^\"']+)[\"'][^>]*>"
        r"(?P<label>.*?)</a\s*>",
        unwrap,
        page,
        flags=re.S | re.I,
    )


def _ensure_section_id(fragment: str, kind: str) -> str:
    """Give a section the anchor its own name promises.

    Measured live on ``subagents``: the hero child wrote
    ``<a href="#features">Discover More</a>`` and the features child wrote
    ``<section class="features">`` with no ``id``.  The page was rejected with
    "internal links target missing ids: #features" — correctly, because the
    link went nowhere.

    The harness knows every section's name, so the anchor is its job, not the
    child's: a child cannot know what its siblings will be called.  The first
    element of the fragment gets ``id="<kind>"`` when it has no id of its own.
    """
    if not fragment or not kind:
        return fragment
    first = re.search(r"<([a-zA-Z][a-zA-Z0-9-]*)\b([^>]*)>", fragment)
    if not first:
        return fragment
    attrs = first.group(2) or ""
    if re.search(r"\bid\s*=", attrs):
        return fragment
    # Do not add an id to a void element or to a comment-only fragment.
    if first.group(1).lower() in _VOID_TAGS:
        return fragment
    start, end = first.span()
    return f'{fragment[:start]}<{first.group(1)}{attrs} id="{kind}">{fragment[end:]}'


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
        # A child model that has seen Bootstrap writes its own container names:
        # measured live, the section child emitted ``<div class="grid">`` and
        # its own ``.grid { display: flex }`` rule, which came AFTER the
        # harness's earlier ``.grid`` rule and won.  The page reported four
        # cards and laid them out in one row.  The invariant has to be stated
        # in the block that is emitted last, or it is not an invariant.
        ".grid, .row {\n"
        "    display: grid !important;\n"
        "    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)) !important;\n"
        "    gap: 1.5rem;\n"
        "    width: 100%;\n"
        "}\n"
        ".grid > *, .row > * {\n"
        "    min-width: 0;\n"
        "    overflow-wrap: anywhere;\n"
        "}\n"
        # A card inside a grid is sized by the grid, not by its own width.  A
        # child that has seen a float/flex layout writes
        # ``.feature-card { width: calc(25% - 20px) }``; inside a grid that
        # resolves against the *column*, so four cards measured 56px wide and
        # 2708px tall — the page "had four cards" and was unreadable.  The
        # harness owns the sizing of a grid item; the child owns its look.
        ".feature-grid > *, .features-grid > *, .features > *,\n"
        ".grid > *, .row > * {\n"
        "    width: auto !important;\n"
        "    max-width: none !important;\n"
        "    flex: none !important;\n"
        "}\n"
        '[class*="col-"] {\n'
        "    min-width: 0;\n"
        "    width: auto !important;\n"
        "    max-width: none !important;\n"
        "}\n"
        # The palette is the harness's, and a headline is where it shows.  A
        # child that writes ``<h1 style="color: #fff">`` overrides the sheet
        # with an inline style — measured live, the page's h1 rendered white
        # while ``--accent: cyan`` sat unused in ``:root``.  A stylesheet rule
        # with ``!important`` beats an inline style without it, so the palette
        # is restored without touching the child's markup.
        "h1 { color: var(--accent) !important; }\n"
        "h2 { color: var(--secondary) !important; }\n"
        # A page that is correct at 1440px and scrolls sideways at 375px is not
        # responsive, and no structural check sees it: measured live with
        # ``harness/wcag.py``, the cyberpunk landing page had a 6.46 contrast
        # ratio (AA passes) and ``documentElement.scrollWidth > innerWidth`` at
        # 375px.  The cause is always the same shape — a fixed width, a
        # ``min-width`` larger than the viewport, or a grid whose minimum column
        # is wider than the phone.  The harness owns the layout, so the
        # invariants are stated here rather than hoped for in a child's CSS.
        "@media (max-width: 480px) {\n"
        "    html, body { max-width: 100%; overflow-x: hidden; }\n"
        "    .feature-grid, .features-grid, .features:not(section),\n"
        "    .grid, .row {\n"
        "        grid-template-columns: 1fr !important;\n"
        "    }\n"
        "    img, video, iframe, table, pre { max-width: 100% !important; }\n"
        "    pre, code { white-space: pre-wrap !important; word-break: break-word; }\n"
        "}\n"
        # A single element wider than the viewport is the other half of the same
        # defect: a long unbroken token (a URL, a hash) or a fixed ``width:
        # 1200px`` on a container.  ``overflow-wrap`` on the body is the
        # cheapest global guard, and ``max-width: 100%`` on block containers
        # stops a fixed width from escaping.
        "body { overflow-wrap: break-word; }\n"
        "section, header, footer, main, .container, .wrapper {\n"
        "    max-width: 100%;\n"
        "    box-sizing: border-box;\n"
        "}\n"
    )


def _normalise_cards(fragment: str, section: Any, plan: Any) -> str:
    """Drop empty cards and bring the card count up to what the plan promised.

    Two defects, one cause: the harness knew the answer and let the model
    decide it.  A child returned ``<div class="feature-card">`` with no
    content, and the page shipped a blank card; it also returned three cards
    where the goal asked for four.  Both are countable facts about markup the
    harness already holds.

    Only the features section is touched, and only when the plan states a
    count: a page whose plan says nothing about cards is left exactly as the
    child wrote it.
    """
    kind = getattr(section, "kind", "")
    if kind not in ("features", "feature_grid", "grid"):
        return fragment
    wanted = int(getattr(plan, "card_count", 0) or 0)
    if wanted <= 0:
        return fragment

    # An empty card is a card element whose inner text is blank.
    def _is_empty(match: "re.Match[str]") -> bool:
        inner = re.sub(r"<[^>]+>", "", match.group(2))
        return not inner.strip()

    pattern = re.compile(
        r'(<div\s+class="[^"]*feature-card[^"]*"[^>]*>)(.*?)(</div>)',
        re.DOTALL,
    )
    kept: list[str] = []
    for match in pattern.finditer(fragment):
        if _is_empty(match):
            continue
        kept.append(match.group(0))

    if not kept:
        return fragment

    # Rebuild the grid with the surviving cards, then top it up from the plan.
    grid = re.search(
        r'(<div\s+class="[^"]*(?:feature-grid|features-grid)[^"]*"[^>]*>)(.*?)(</div>\s*</section>)',
        fragment,
        re.DOTALL,
    )
    if grid is None:
        return fragment

    features = list(getattr(plan, "features", None) or [])
    while len(kept) < wanted:
        index = len(kept)
        if index < len(features):
            name, desc = features[index]
        else:
            name, desc = f"Feature {index + 1}", "Details to follow."
        kept.append(
            f'<div class="feature-card"><h3>{_escape(name)}</h3>'
            f"<p>{_escape(desc)}</p></div>"
        )

    rebuilt = grid.group(1) + "".join(kept) + grid.group(3)
    return fragment[: grid.start()] + rebuilt + fragment[grid.end():]


def _fallback_section(section: "SectionSpec", plan: "GenerationPlan") -> str:
    """Minimal valid markup for a planned section whose child returned nothing.

    Deliberately plain: the harness is not trying to out-design the model, it is
    keeping the page structurally complete.  The comment the caller prepends says
    the section was filled by the harness, so a reader of the file can tell.
    """
    kind = getattr(section, "kind", "")
    title = _escape(getattr(plan, "title", "") or "GigaForge")
    if kind == "hero":
        # The goal's own words decide the hero's shape.  Measured live on
        # ``subagents``: the hero child returned nothing, the fallback emitted a
        # bare ``<h1>``, and the page shipped with no glitch effect and no
        # tagline — the goal had asked for both.  The harness can read the
        # request, so the fallback honours it instead of being deliberately
        # plain.
        goal = (getattr(plan, "goal", "") or "").lower()
        tagline = _escape(getattr(plan, "tagline", "") or "")
        heading = f"<h1>{title}</h1>"
        if "glitch" in goal:
            heading = f'<div class="glitch-text">{heading}</div>'
        body = f"<p>{tagline}</p>" if tagline else ""
        return f'<section class="hero">{heading}{body}</section>'
    if kind in ("features", "feature_grid", "grid"):
        cards = "".join(
            f'<div class="feature-card"><h3>{_escape(name)}</h3>'
            f"<p>{_escape(desc)}</p></div>"
            for name, desc in (getattr(plan, "features", None) or [])
        )
        if not cards:
            cards = "".join(
                f'<div class="feature-card"><h3>Feature {i}</h3>'
                f"<p>Details to follow.</p></div>"
                for i in range(1, 4)
            )
        return f'<section class="features"><h2>Features</h2><div class="feature-grid">{cards}</div></section>'
    if kind in ("footer",):
        return f'<footer class="footer"><p>{title} — built by the GigaForge harness.</p></footer>'
    if kind in ("cta",):
        return '<section class="cta"><h2>Get started</h2><a class="cta-button" href="#">Try it</a></section>'
    if kind in ("nav", "header"):
        return f'<nav class="nav"><a href="#">{title}</a></nav>'
    return ""


def assemble_page(
    plan: GenerationPlan,
    fragments: dict[str, str],
    *,
    css: str = "",
    existing: dict[str, str] | None = None,
) -> str:
    """Assemble the final page from per-section fragments.

    The skeleton is the harness's, so the result is structurally valid by
    construction: one doctype, one ``<html>``, one ``<head>``, one ``<style>``,
    one ``<body>``, and each section exactly once in catalogue order.

    ``existing`` holds the sections of the page being edited.  A section the
    child was not asked to rewrite is reused verbatim from there, so an edit
    adds what the goal asked for and keeps everything else — the alternative,
    regenerating the whole page, silently discards work the user already had.
    """
    palette = plan.palette or ("cyan", "magenta")
    accent = palette[0]
    secondary = palette[1] if len(palette) > 1 else palette[0]
    body_parts: list[str] = []
    child_css: list[str] = []
    for section in plan.sections:
        raw = fragments.get(section.kind, "")
        reused = False
        if not raw.strip() and existing:
            raw = existing.get(section.kind, "")
            reused = bool(raw.strip())
        fragment = _strip_document_wrapper(raw)
        # A child is asked for one section; a sibling section it pasted in is
        # not its to write, and the harness has its own fragment for it.
        fragment = _drop_foreign_sections(
            fragment, section.kind, {s.kind for s in plan.sections}
        )
        # A child that answers in a template language (Slim/HAML/Pug) has not
        # produced markup.  Shipping its source renders the template as prose.
        if fragment and not _looks_like_markup(fragment):
            fragment = ""
        if fragment:
            # A child that emits an empty card has produced a grid with a hole
            # in it.  Measured live on ``subagents``: the features child
            # returned ``<div class="feature-card">`` with no content, so the
            # page shipped three cards where the goal asked for four, and the
            # first of them was blank.  The harness knows the plan's card count
            # and can see the markup, so it settles both deterministically
            # instead of asking the model to count.
            fragment = _normalise_cards(fragment, section, plan)
        if not fragment:
            # A planned section with no fragment used to be skipped silently, so
            # the page came out missing a part the plan promised.  Measured live
            # on ``subagents``: the footer child returned nothing usable, the
            # footer vanished, and the run still reported ``accepted: True`` —
            # the contract had no footer requirement to fail on.
            #
            # The plan is the harness's own promise.  If a child cannot fill a
            # section, the harness fills it from the plan rather than shipping a
            # page that is missing a section it said it would have.
            fragment = _fallback_section(section, plan)
            if not fragment:
                continue
            self_note = f"<!-- {section.kind}: filled by the harness, child returned nothing -->"
            fragment = f"{self_note}\n{fragment}"
        body_parts.append(f"    {section.anchor}\n    {_ensure_section_id(fragment, section.kind)}")
        if reused:
            # The section came from the page being edited: its rules are
            # already in that page's stylesheet, and re-scoping them would
            # change how it looks.
            continue
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
    page = (
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
    # Last, because it needs every id the page ended up with.
    return _drop_dead_anchors(page)


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
        /* A glitch hero is a request the harness can honour itself.  Measured
           live on ``subagents``: the hero child returned nothing, the fallback
           emitted a bare heading, and the page shipped with no glitch effect
           at all — the goal had asked for one.  The markup and the animation
           are both the harness's, so a child that fails cannot lose them. */
        .glitch-text {{ position: relative; display: inline-block; }}
        .glitch-text h1 {{ position: relative; animation: gf-glitch 2.5s infinite; }}
        .glitch-text h1::before,
        .glitch-text h1::after {{
            content: attr(data-text);
            position: absolute;
            inset: 0;
            clip-path: inset(0 0 55% 0);
        }}
        .glitch-text h1::before {{ color: var(--secondary); animation: gf-glitch-a 2.5s infinite; }}
        .glitch-text h1::after {{ color: var(--accent); animation: gf-glitch-b 2.5s infinite; }}
        @keyframes gf-glitch {{
            0%, 92%, 100% {{ transform: translate(0); }}
            93% {{ transform: translate(-2px, 1px); }}
            95% {{ transform: translate(2px, -1px); }}
            97% {{ transform: translate(-1px, -1px); }}
        }}
        @keyframes gf-glitch-a {{
            0%, 90%, 100% {{ transform: translate(0); opacity: 0; }}
            92% {{ transform: translate(-3px, 0); opacity: .8; }}
            96% {{ transform: translate(3px, 0); opacity: .8; }}
        }}
        @keyframes gf-glitch-b {{
            0%, 90%, 100% {{ transform: translate(0); opacity: 0; }}
            94% {{ transform: translate(3px, 0); opacity: .8; }}
            98% {{ transform: translate(-3px, 0); opacity: .8; }}
        }}
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
        /* A child model that has seen Bootstrap writes
           ``.feature-grid > .container > .grid > .row > .col-md-6.col-lg-3``.
           The rules above then match nothing: measured in headless Chromium on
           the live page, ``.feature-grid`` resolved to ``display: block`` with
           ONE child (the container), and the four cards stacked vertically at
           1440px each.  The page "had four cards" and was not a grid.

           The harness cannot know which wrapper names a model will invent, so
           the invariant is stated structurally instead of by class name: any
           element that is the sole child of a grid container becomes a grid
           too, and any ``.row``/``.grid``/``.col-*`` is laid out as one.  This
           is emitted last, so it wins over the model's own rules. */
        .feature-grid > :only-child,
        .features-grid > :only-child,
        .grid, .row {{
            display: grid;
            gap: 1.5rem;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            width: 100%;
        }}
        .feature-grid > :only-child > :only-child,
        .grid > .row {{
            display: grid;
            gap: 1.5rem;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            width: 100%;
        }}
        [class*="col-"] {{
            min-width: 0;
            width: auto !important;
            max-width: none !important;
            flex: 1 1 240px;
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

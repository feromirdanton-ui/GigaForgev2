"""Deterministic verification of a produced artifact against the goal.

**Why this module exists.**  A live run asked for a cyberpunk page with a
"neon cyan/magenta palette".  The model wrote a 4 KB page whose entire palette
was ``#fff`` on ``#000`` — no cyan, no magenta — then called ``finish`` with an
empty plan and the run reported success.  Nothing in the harness had ever
looked at the file.  The model was asked to grade its own homework and did.

The harness's job is to take on the work that has a deterministically correct
answer.  "Does the page contain the colours the user named?" is a string
search, not a judgement call.  This module extracts the *explicit* constraints
from the goal — named colours, named sections, "no external assets" — and
checks the produced files against them.  It never guesses at taste; it only
checks what the user literally asked for.

The verdict is fed back to the model as an observation, so a miss becomes a
correction step inside the same episode instead of a silent pass.

**Three ways the check used to pass without the artifact deserving it.**  All
three were measured, and all three are the same mistake — checking something
other than the file the run produced:

* a colour named in a **CSS comment** counted.  A page that was white on black
  but contained ``/* background: cyan; abandoned */`` satisfied "cyan";
* with no file list, the check fell back to **every text file in the worktree**,
  so a run that wrote nothing passed on a file that was already in the repo;
* ``min_size`` **summed** the files, so five 300-byte files satisfied "at least
  1200 bytes" for an artifact that was supposed to be one page.

The rule that fixes all three: check only the files the run created or changed,
check each of them on its own, and read the text the way a browser would.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Colour words a goal may name, mapped to the CSS spellings that satisfy them.
#:
#: Only words that appear *in the goal* become requirements — this is not a
#: style guide, it is a checklist of what the user said.  "neon" is deliberately
#: absent: it is a style modifier ("neon cyan"), not a colour, and treating it as
#: one let a page satisfy the contract by writing the word in prose.
#:
#: The numeric forms matter.  Measured live: a page whose only magenta was
#: ``text-shadow: 2px 2px 4px rgba(255, 0, 255, 0.5)`` was rejected for
#: "colour 'magenta'" — the colour was there, in the form a designer actually
#: writes it, and the checker only knew the keyword.  A false negative is as
#: damaging as a false positive: it rejects work that is correct.
_COLOUR_WORDS: dict[str, tuple[str, ...]] = {
    "cyan": ("cyan", "#0ff", "#00ffff", "aqua", "rgb(0, 255, 255", "rgb(0,255,255",
             "rgba(0, 255, 255", "rgba(0,255,255"),
    "magenta": ("magenta", "#f0f", "#ff00ff", "fuchsia", "rgb(255, 0, 255",
                "rgb(255,0,255", "rgba(255, 0, 255", "rgba(255,0,255"),
    "purple": ("purple", "#800080", "#a020f0", "#9d00ff", "rgb(128, 0, 128",
               "rgba(128, 0, 128"),
    "violet": ("violet", "#8a2be2", "#7f00ff"),
    "orange": ("orange", "#ffa500", "#ff7f00", "rgb(255, 165, 0", "rgba(255, 165, 0"),
    "green": ("green", "#0f0", "#00ff00", "lime", "rgb(0, 255, 0", "rgba(0, 255, 0"),
    "red": ("red", "#f00", "#ff0000", "rgb(255, 0, 0", "rgba(255, 0, 0"),
    "blue": ("blue", "#00f", "#0000ff", "rgb(0, 0, 255", "rgba(0, 0, 255"),
    "yellow": ("yellow", "#ff0", "#ffff00", "rgb(255, 255, 0", "rgba(255, 255, 0"),
    "pink": ("pink", "#ffc0cb", "#ff69b4"),
    "gold": ("gold", "#ffd700"),
}

#: A colour only counts when it sits in a CSS declaration, not in prose.  The
#: live run produced a page whose only colours were #fff and #000, and the
#: contract still passed "cyan" because the word appeared in a comment.
_CSS_COLOUR_CONTEXT = (
    "color",
    "background",
    "shadow",
    "border",
    "fill",
    "stroke",
    "outline",
    "rgba(",
    "rgb(",
    "hsl(",
    "hsla(",
    "gradient",
)

#: Section words a goal may name, mapped to the markup that satisfies them.
_SECTION_WORDS: dict[str, tuple[str, ...]] = {
    "footer": ("<footer", "footer"),
    "header": ("<header", "header"),
    "hero": ("hero",),
    "grid": ("grid", "display:grid", "display: grid"),
    "nav": ("<nav", "nav"),
    "form": ("<form", "form"),
    "table": ("<table", "table"),
    "button": ("<button", "button"),
    "card": ("card",),
    "sidebar": ("sidebar", "aside"),
}

#: Phrases that mean "the artifact must be self-contained".
_SELF_CONTAINED = (
    "self-contained",
    "self contained",
    "no external assets",
    "no external resources",
    "single file",
    "standalone",
)

_NAMED_LIST_RE = re.compile(
    r"\b(?:cards?|models?|libraries|resources?)\b[^()]{0,80}\(([^()]{2,240})\)",
    re.I,
)
_SOURCE_COUNT_RE = re.compile(
    r"\b(?P<count>\d[\d,]*)\s+(?:(?:official|reputable|credible)\s+)?sources?\b",
    re.I,
)
_WORD_UNIT = r"(?:words?|слов(?:о|а|ей)?)"
_CHAR_UNIT = r"(?:characters?|chars?|символ(?:а|ов)?|знак(?:а|ов)?)"
_EXACT_COUNT_RE = re.compile(
    rf"\b(?:exactly|precisely|ровно)\s+"
    rf"(?P<count>\d[\d,]*)\s*(?P<unit>{_WORD_UNIT}|{_CHAR_UNIT})\b",
    re.I,
)
_COUNT_RANGE_RE = re.compile(
    rf"\b(?P<minimum>\d[\d,]*)\s*[-\u2013\u2014]\s*"
    rf"(?P<maximum>\d[\d,]*)\s*(?P<unit>{_WORD_UNIT}|{_CHAR_UNIT})\b",
    re.I,
)
_MIN_COUNT_RE = re.compile(
    rf"\b(?:at\s+least|minimum(?:\s+of)?|no\s+fewer\s+than|not\s+less\s+than)"
    rf"\s+(?P<count>\d[\d,]*)\s*(?P<unit>{_WORD_UNIT}|{_CHAR_UNIT})\b",
    re.I,
)

#: A page smaller than this cannot plausibly satisfy a multi-part design brief.
_MIN_ARTIFACT_BYTES = 1200

#: Placeholder text that means the artifact is a skeleton, not a deliverable.
#:
#: Measured live: a goal asking for a cyberpunk landing page produced a page
#: that satisfied every structural requirement — cyan and magenta in real CSS
#: declarations, a hero, a glitch keyframe, a grid, a footer, self-contained,
#: 1539 bytes — and whose entire body was
#:
#:     <div class="card">Feature Card 1</div>   (×4)
#:     <div class="footer">Footer Content</div>
#:
#: The contract passed it, because every check it had was structural.  A
#: skeleton is not a deliverable, and "is this text a placeholder?" is a string
#: match, not a judgement call — so it belongs here rather than in a prompt.
_PLACEHOLDER_PATTERNS = (
    r"\bfeature\s+card\s+\d+\b",
    r"\bfeature\s+\d+\b",
    r"\bfooter\s+content\b",
    r"\bheader\s+content\b",
    r"\bcard\s+content\b",
    r"\blorem\s+ipsum\b",
    r"\bdolor\s+sit\s+amet\b",
    r"\bplaceholder\b",
    r"\byour\s+text\s+here\b",
    r"\btext\s+here\b",
    r"\btodo\b",
    r"\bcoming\s+soon\b",
    r"\bsample\s+text\b",
    r"\bexample\s+content\b",
    r"\binsert\s+.{0,20}\s+here\b",
    r"\b(?:detailed\s+)?(?:explanation|details|content)\s+continues?\s+in\s+"
    r"(?:the\s+)?(?:following|subsequent)\s+sections?\b",
    r"\b(?:to\s+be\s+continued|more\s+content\s+to\s+follow)\b",
    # Meta-copy: a sentence that describes the page instead of being the page.
    # Measured live: a feature card read "This is where we showcase our
    # cutting-edge AI technology" — a description of what the card would say,
    # accepted as the card's content.
    r"\bthis\s+is\s+where\s+we\b",
    r"\bthis\s+section\s+(?:will\s+)?(?:show|showcase|display|contain)\b",
    r"\bhere\s+you\s+can\s+(?:see|find|learn)\b",
    r"\badd\s+your\s+.{0,20}\s+here\b",
)

#: Deliberately NOT included: a bare run of ``x`` characters.  It looks like a
#: placeholder and it fires on legitimate content — a redacted value, a hex
#: mask, a form field's filler — and a check that rejects real content is worse
#: than one that misses an edge case.  The patterns above catch the skeleton the
#: live run actually produced.

#: A page whose visible text is shorter than this is a skeleton regardless of
#: how many elements it has.  Four cards reading "Feature Card 1" are four
#: elements and zero content.
_MIN_VISIBLE_TEXT_CHARS = 400

_PLACEHOLDER_RE = re.compile("|".join(_PLACEHOLDER_PATTERNS), re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)

#: Comment syntaxes to strip before checking.  A comment is not a declaration:
#: the browser does not apply it, so the contract must not count it.
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


_CUSTOM_PROPERTY_RE = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]+);")


def resolve_css_variables(text: str) -> str:
    """Inline ``var(--x)`` uses with the value declared for ``--x``.

    Measured live: a sectional run produced a page that declared

        :root { --accent: cyan; --secondary: magenta; }
        h1 { color: var(--accent); }

    and the colour check rejected it.  The colours are declared correctly — the
    reference is indirect, and resolving an indirection is a string operation,
    not a judgement.  One level is resolved (a variable defined in terms of
    another is left alone), which covers the palette idiom without pretending to
    be a CSS engine.
    """
    declared = {name: value.strip() for name, value in _CUSTOM_PROPERTY_RE.findall(text)}

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        fallback = match.group(2)
        if name in declared:
            return declared[name]
        return fallback if fallback is not None else match.group(0)

    out = re.sub(
        r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^)]*))?\)",
        _sub,
        text,
    )
    # A custom property whose value is a colour is itself a colour declaration:
    # ``--accent: cyan;`` says the accent *is* cyan.
    if declared:
        out += "\n" + "\n".join(f"color: {value};" for value in declared.values())
    return out


def strip_comments(text: str) -> str:
    """Remove CSS/JS/HTML comments, keeping the code that actually runs.

    Deliberately conservative: only comment forms that cannot appear inside a
    string literal in the languages this contract checks are removed, so a
    ``//`` inside a URL is not mistaken for a comment.
    """
    out = _BLOCK_COMMENT.sub(" ", text)
    out = _HTML_COMMENT.sub(" ", out)
    out = _LINE_COMMENT.sub(" ", out)
    return out


def visible_text(text: str) -> str:
    """The text a reader actually sees: no tags, no script, no style.

    A placeholder check has to look at what is rendered.  Searching the raw
    source would count a class name (``class="placeholder"``) and miss nothing
    useful, while searching the rendered text catches exactly the skeleton the
    live run produced.
    """
    body = _HTML_COMMENT.sub(" ", text)
    body = _SCRIPT_STYLE_RE.sub(" ", body)
    body = _TAG_RE.sub(" ", body)
    return re.sub(r"\s+", " ", body).strip()


def find_placeholders(text: str) -> list[str]:
    """Placeholder phrases in the visible text, deduplicated, in order."""
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(visible_text(text)):
        phrase = match.group(0).strip().lower()
        if phrase and phrase not in seen:
            seen.append(phrase)
    return seen


def _numbered_source_entries(text: str) -> list[str]:
    lines = text.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if re.match(r"\s*\d+[.)]\s+\S", line)
    ]
    return [
        "\n".join(lines[start : starts[pos + 1] if pos + 1 < len(starts) else len(lines)])
        for pos, start in enumerate(starts)
    ]


@dataclass(frozen=True)
class Requirement:
    """One explicit, machine-checkable thing the goal asked for."""

    kind: str  # "colour" | "section" | "self_contained" | "min_size" | ...
    label: str
    needles: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None

    def satisfied_by(self, text: str) -> bool:
        low = strip_comments(resolve_css_variables(text)).lower()
        if self.kind in {"word_count", "char_count"}:
            count = (
                len(re.findall(r"\b[\w'-]+\b", visible_text(text)))
                if self.kind == "word_count"
                else len(text)
            )
            return (
                (self.minimum is None or count >= self.minimum)
                and (self.maximum is None or count <= self.maximum)
            )
        if self.kind == "source_count":
            entries = _numbered_source_entries(text)
            return (
                len(entries) == self.minimum
                and all(re.search(r"https?://\S+", entry) for entry in entries)
            )
        if self.kind == "self_contained":
            # Any absolute URL in a src/href is an external asset.
            return not re.search(r'(?:src|href)\s*=\s*["\']https?://', low)
        if self.kind == "min_size":
            return len(text) >= _MIN_ARTIFACT_BYTES
        if self.kind == "substance":
            # Two independent signals, either of which fails the artifact:
            # a placeholder phrase in the visible text, or too little visible
            # text to be a page rather than a skeleton.
            if find_placeholders(text):
                return False
            return len(visible_text(text)) >= _MIN_VISIBLE_TEXT_CHARS
        if self.kind == "named_item":
            return bool(self.needles) and all(needle in low for needle in self.needles)
        if self.kind == "reduced_motion":
            return bool(
                re.search(
                    r"@media\s*\(\s*prefers-reduced-motion\s*:",
                    low,
                )
            )
        if self.kind == "interactive_form":
            has_form = bool(re.search(r"<form\b", low))
            has_validation = bool(
                re.search(r"\brequired\b|checkvalidity\s*\(|reportvalidity\s*\(", low)
            )
            has_submit_handler = bool(
                re.search(
                    r"addEventListener\s*\(\s*['\"]submit['\"]|onsubmit\s*=",
                    low,
                    re.I,
                )
            )
            has_success_state = any(
                phrase in low
                for phrase in (
                    "success",
                    "thank you",
                    "you're in",
                    "you are in",
                    "успешно",
                    "спасибо",
                    "вы присоединились",
                    "заявка отправлена",
                    "регистрация завершена",
                )
            )
            needs_live_region = "aria-live" in self.needles
            return (
                has_form
                and has_validation
                and has_submit_handler
                and has_success_state
                and (not needs_live_region or "aria-live" in low)
            )
        if self.kind == "responsive_navigation":
            nav_match = re.search(r"<nav\b[^>]*>.*?</nav\s*>", low, re.S)
            if not nav_match or "@media" not in low:
                return False
            controlled_menu = False
            for button in re.findall(r"<button\b[^>]*>", low, re.S):
                if "aria-expanded" not in button:
                    continue
                controls = re.search(r"\baria-controls\s*=\s*['\"]([^'\"]+)['\"]", button)
                if controls and re.search(
                    rf"\bid\s*=\s*['\"]{re.escape(controls.group(1))}['\"]",
                    nav_match.group(),
                ):
                    controlled_menu = True
                    break
            has_click_handler = bool(
                re.search(
                    r"addEventListener\s*\(\s*['\"]click['\"]|onclick\s*=",
                    low,
                    re.I,
                )
            )
            updates_expanded = bool(
                re.search(
                    r"setAttribute\s*\(\s*['\"]aria-expanded['\"]|\.ariaexpanded\s*=",
                    low,
                    re.I,
                )
            )
            changes_menu_state = bool(
                re.search(
                    r"classList\.(?:toggle|add|remove)\s*\(|"
                    r"\.hidden\s*=|\.style\.(?:display|visibility)\s*=",
                    low,
                )
            )
            return (
                controlled_menu
                and has_click_handler
                and updates_expanded
                and changes_menu_state
            )
        if self.kind == "section":
            # A section is identified by the *name* the plan gave it, and a
            # model may carry that name in either attribute.  Measured live on
            # the site goal: the assembler wrote ``<section id="features">``
            # while the contract's needle was ``class="features"``, so a page
            # with a complete, correct feature section failed with
            # "section 'features'" — and the same failure repeated every cycle
            # until the goal gave up.  The name is the fact; the attribute is
            # not.  Both spellings are accepted, and the bare word is accepted
            # as a last resort so a section named in prose still counts.
            for needle in self.needles:
                if needle in low:
                    return True
            for name in self.needles:
                token = name.strip("\"'")
                if not token or token.startswith("<") or ":" in token:
                    continue
                if re.search(
                    rf"\b(?:class|id)\s*=\s*[\"'][^\"']*\b{re.escape(token)}\b",
                    low,
                ):
                    return True
            return False
        if self.kind == "section_heading":
            sections = re.findall(r"<section\b[^>]*>(.*?)</section\s*>", low, re.S)
            for section in sections:
                headings = re.findall(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]\s*>", section, re.S)
                heading_text = " ".join(_TAG_RE.sub(" ", heading) for heading in headings)
                if all(needle in heading_text for needle in self.needles):
                    return True
            return False
        if self.kind == "internal_links":
            targets = set(re.findall(r"\bhref\s*=\s*['\"]#([^'\"]+)['\"]", low))
            ids = set(re.findall(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", low))
            return bool(targets) and targets.issubset(ids)
        if self.kind == "colour":
            # The colour must sit in a CSS *declaration*: the needle has to
            # appear after a ``:`` on a line that also names a colour property.
            # Checking the whole line let a comment satisfy it, and checking
            # only for the property let ``color: red; /* cyan */`` satisfy
            # "cyan" — the value has to be on the right-hand side.
            for line in low.splitlines():
                if not any(n in line for n in self.needles):
                    continue
                if not any(ctx in line for ctx in _CSS_COLOUR_CONTEXT):
                    continue
                head, _, tail = line.partition(":")
                if not tail:
                    continue
                if any(n in tail for n in self.needles):
                    return True
            return False
        return any(n in low for n in self.needles)


@dataclass
class ContractVerdict:
    """What the artifact does and does not satisfy."""

    checked: list[str] = field(default_factory=list)
    unmet: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    #: Requirements the artifact already satisfies.  Carried into the
    #: correction step so a rewrite keeps them: measured live, the model
    #: rewrote index.html to add `hero` and dropped `cyan`/`magenta`, then
    #: rewrote again to add the colours and dropped `hero`, for the whole step
    #: budget.  Telling the model only what is missing invites exactly that.
    met: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unmet

    @property
    def completeness(self) -> float:
        """Fraction of the contract's requirements the artifact satisfies, 0..1.

        **Why this is a property and not a number in a report.**  Every report
        this project produced said "9/10" — a figure a human typed after looking
        at a screenshot.  It was not reproducible, it did not move when the code
        changed, and two runs of the same build could be scored differently by
        the same person.  A number that cannot be recomputed is not a metric.

        This one is computed from the verdict: ``met / (met + unmet)``.  It is
        the same arithmetic for every mode, it is in the run's own output, and
        it changes when the artifact changes.  ``1.0`` means every requirement
        the plan set was satisfied; ``0.0`` means none was.
        """
        total = len(self.met) + len(self.unmet)
        if total <= 0:
            # Nothing was checked.  Reporting 1.0 would claim a perfect artifact
            # on the strength of having checked nothing.
            return 0.0
        return len(self.met) / total

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "completeness": round(self.completeness, 4),
            "met": list(self.met),
            "unmet": list(self.unmet),
            "checked": list(self.checked),
            "files": list(self.files),
        }

    def as_observation(self) -> str:
        """The text handed back to the model as a correction step."""
        if self.ok:
            return ""
        lines = [
            "CONTRACT: the artifact does not yet satisfy the goal. "
            "Fix these before finishing:",
        ]
        lines.extend(f"- {u}" for u in self.unmet)
        if self.met:
            lines.append("")
            lines.append(
                "These are ALREADY satisfied. Keep them — when you rewrite the "
                "file, copy the current content and add what is missing instead "
                "of starting a new page:"
            )
            lines.extend(f"- {m}" for m in self.met)
        lines.append("")
        lines.append(
            "For an existing file, use read_file then edit_file to preserve "
            "the content that already passes. Use write_file only for a new "
            "file or a necessary full rewrite, then finish."
        )
        return "\n".join(lines)


def extract_requirements(goal: str) -> list[Requirement]:
    """Pull the explicit, checkable constraints out of a goal statement.

    Deliberately conservative: a word only becomes a requirement when the goal
    names it.  A goal that says nothing about colour produces no colour
    requirement, so the harness never invents a standard the user did not set.
    """
    low = goal.lower()
    reqs: list[Requirement] = []

    for match in _EXACT_COUNT_RE.finditer(goal):
        count = int(match.group("count").replace(",", ""))
        unit = match.group("unit").lower()
        kind = "word_count" if re.search(_WORD_UNIT, unit, re.I) else "char_count"
        unit_name = "words" if kind == "word_count" else "characters"
        reqs.append(
            Requirement(
                kind,
                f"exactly {count} {unit_name}",
                minimum=count,
                maximum=count,
            )
        )

    for match in _SOURCE_COUNT_RE.finditer(goal):
        count = int(match.group("count").replace(",", ""))
        reqs.append(
            Requirement(
                "source_count",
                f"exactly {count} numbered sources with URLs",
                minimum=count,
                maximum=count,
            )
        )

    for match in _COUNT_RANGE_RE.finditer(goal):
        minimum = int(match.group("minimum").replace(",", ""))
        maximum = int(match.group("maximum").replace(",", ""))
        unit = match.group("unit").lower()
        kind = "word_count" if re.search(_WORD_UNIT, unit, re.I) else "char_count"
        unit_name = "words" if kind == "word_count" else "characters"
        reqs.append(
            Requirement(
                kind,
                f"between {minimum} and {maximum} {unit_name}",
                minimum=minimum,
                maximum=maximum,
            )
        )

    for match in _MIN_COUNT_RE.finditer(goal):
        minimum = int(match.group("count").replace(",", ""))
        unit = match.group("unit").lower()
        kind = "word_count" if re.search(_WORD_UNIT, unit, re.I) else "char_count"
        unit_name = "words" if kind == "word_count" else "characters"
        reqs.append(
            Requirement(
                kind,
                f"at least {minimum} {unit_name}",
                minimum=minimum,
            )
        )

    for word, needles in _COLOUR_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            reqs.append(Requirement("colour", f"colour '{word}'", needles))

    for word, needles in _SECTION_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            reqs.append(Requirement("section", f"section '{word}'", needles))

    for match in _NAMED_LIST_RE.finditer(goal):
        names = [
            item.strip(" \t\r\n.,;:()[]{}'\"")
            for item in re.split(r",|;|\band\b", match.group(1), flags=re.I)
        ]
        if len(names) >= 2:
            reqs.extend(
                Requirement("named_item", f"include '{name}'", (name.lower(),))
                for name in names
                if name
            )

    if re.search(r"\breduced[- ]motion\b", low):
        reqs.append(Requirement("reduced_motion", "reduced-motion support"))

    if "form" in low and any(word in low for word in ("validation", "success state")):
        form_needles = ("aria-live",) if "aria-live" in low else ()
        reqs.append(
            Requirement(
                "interactive_form",
                "a working form with validation and a success state",
                form_needles,
            )
        )

    if "navigation" in low and "mobile" in low:
        reqs.append(
            Requirement(
                "responsive_navigation",
                "working responsive mobile navigation",
            )
        )

    if re.search(r"\b(?:events?\s*(?:/|and)\s*learning|learning\s+resources)\s+section\b", low):
        reqs.append(
            Requirement(
                "section_heading",
                "an events/learning content section",
                ("event", "learning"),
            )
        )

    if "href" in low and ("target" in low or "internal link" in low):
        reqs.append(
            Requirement(
                "internal_links",
                "all internal link targets exist",
            )
        )

    if any(p in low for p in _SELF_CONTAINED):
        reqs.append(Requirement("self_contained", "no external assets"))

    # A design brief with several named parts is not a one-liner.
    if len(reqs) >= 3:
        reqs.append(
            Requirement(
                "heading",
                "a headline (<h1>)",
            )
        )
        reqs.append(
            Requirement(
                "min_size",
                f"artifact of at least {_MIN_ARTIFACT_BYTES} bytes",
            )
        )
        # ...and it is not a skeleton either.  Every structural check above can
        # pass on a page whose body is four "Feature Card 1" divs — measured
        # live.  Substance is the check that separates a deliverable from a
        # wireframe.
        reqs.append(
            Requirement(
                "substance",
                "real content, not placeholders (no 'Feature Card 1', 'Footer "
                "Content', 'lorem ipsum', 'TODO'; at least "
                f"{_MIN_VISIBLE_TEXT_CHARS} characters of visible text)",
            )
        )
    return reqs


def requested_card_count(goal: str) -> int:
    """How many cards the goal asked for, or 0 when it did not say.

    Measured live: the goal said "a features grid with 4 cards", the page
    shipped three, and the contract passed it because the requirement was a
    hard-coded "at least 3".  The number the user wrote is a fact about the
    request, and the harness can read it without a model call.

    Only a number attached to a card-ish noun counts, so "4 sections" or
    "3 colours" does not become a card requirement.  A number above 24 is
    ignored: it is a year, a price or a token count, not a grid.
    """
    text = (goal or "").lower()
    if not text:
        return 0
    pattern = re.compile(
        r"(?<![\d.])(\d{1,2})(?![\d.])\s*(?:[\w-]+\s+){0,2}?"
        r"(?:cards?|tiles?|panels?|items?|columns?|boxes?)\b"
    )
    for match in pattern.finditer(text):
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 24:
            return value
    return 0


def requirements_from_plan(plan: Any) -> list[Requirement]:
    """The contract for a page goal, taken from the plan the harness itself made.

    **Why this exists.**  ``extract_requirements`` reads the goal text with
    regexes, and it returns **zero** requirements for "Create a single-file
    index.html", "Build a landing page" and "Make a website for my bakery" —
    three of the four ordinary ways to ask for a page.  So the gate was absent
    exactly where it was needed most, and a page with no hero, no palette and
    four "Feature Card 1" divs passed.

    The harness already knows what the page should be: ``plan_generation``
    decided the sections, the palette and the path.  A contract built from that
    plan exists for every page goal, and it checks what the harness itself
    ordered — which is also the fix for "the contract rejects correct work":
    it can no longer demand something the plan never asked for.

    ``plan`` is a :class:`gigaforge.harness.generation.GenerationPlan`; it is
    typed loosely to keep this module free of a circular import.
    """
    reqs: list[Requirement] = []
    kinds = list(getattr(plan, "kinds", lambda: [])() or [])
    palette = tuple(getattr(plan, "palette", ()) or ())

    for kind in kinds:
        needles = _SECTION_WORDS.get(kind)
        if needles is None:
            # A section the catalogue knows but the word table does not: the
            # section's own class name is the marker the assembler writes.
            needles = (f'class="{kind}"', f"<{kind}")
        reqs.append(Requirement("section", f"section '{kind}'", tuple(needles)))

    for word in palette:
        needles = _COLOUR_WORDS.get(word)
        if needles:
            reqs.append(Requirement("colour", f"colour '{word}'", needles))

    # Every page the harness assembles is one self-contained file: the skeleton
    # is the harness's, and a child is told not to emit external assets.
    reqs.append(Requirement("self_contained", "no external assets"))
    reqs.append(Requirement("heading", "a headline (<h1>)"))
    reqs.append(
        Requirement("min_size", f"artifact of at least {_MIN_ARTIFACT_BYTES} bytes")
    )
    reqs.append(
        Requirement(
            "substance",
            "real content, not placeholders (no 'Feature Card 1', 'Footer "
            "Content', 'lorem ipsum', 'TODO'; at least "
            f"{_MIN_VISIBLE_TEXT_CHARS} characters of visible text)",
        )
    )
    if "features" in kinds:
        # The grid is the section most likely to be a skeleton: measured live,
        # a page reported "cards: 0" while four cards were on screen, because
        # the children wrote ``.feature-item`` and the base sheet styled
        # ``.feature-card``.  The count is checked on the markup, and both
        # spellings are accepted — the harness must not fail a page over a
        # class name it did not fix.
        #
        # The *number* comes from the goal when the goal states one.  Measured
        # live: the goal asked for "a features grid with 4 cards", the page
        # shipped three, and the contract passed it — because the requirement
        # was a hard-coded "at least 3".  A contract that ignores the number the
        # user wrote is grading the harness's default, not the request.
        wanted = requested_card_count(getattr(plan, "goal", "") or "")
        reqs.append(
            Requirement(
                "card_count",
                (
                    f"at least {wanted} feature cards in the grid"
                    if wanted
                    else "at least 3 feature cards in the grid"
                ),
                ("feature-card", "feature-item", "card"),
                minimum=wanted or 3,
            )
        )
    # A page has one footer and one header.  Measured live: a page assembled
    # from three sections carried **two** ``<div class="footer">`` blocks — the
    # footer child's, and a second the model added while "finishing" the page.
    # The contract passed it, because nothing checked landmarks: the duplicate
    # check compares content, and the two footers had different text.
    for role in ("footer", "header"):
        if role in kinds:
            reqs.append(
                Requirement(
                    "singleton",
                    f"exactly one {role} on the page",
                    (role,),
                )
            )
    return reqs


def _count_grid_children(html: str) -> int:
    """How many direct children the grid container has.

    The cards are whatever the grid holds, not whatever they are called.  A
    child that writes ``<div class="grid-item">`` has still built a grid of
    four cards; failing it over the class name is the harness grading its own
    vocabulary instead of the artifact.

    The container is found by class token (``grid``, ``features``, ``cards``)
    or by a ``display: grid`` rule naming it, and its direct children are
    counted with a depth counter so nested wrappers are not double-counted.
    """
    container = re.search(
        r'<(?P<tag>[a-z][a-z0-9]*)\b[^>]*class\s*=\s*["\'][^"\']*'
        r'\b(?:feature-grid|features-grid|features|cards|grid)\b[^"\']*["\'][^>]*>',
        html, re.I,
    )
    if not container:
        return 0
    tag = container.group("tag").lower()
    depth = 1
    cursor = container.end()
    children = 0
    token = re.compile(rf"<(/?){re.escape(tag)}\b[^>]*>", re.I)
    while depth and cursor < len(html):
        step = token.search(html, cursor)
        if step is None:
            break
        if step.group(1):
            depth -= 1
        else:
            if depth == 1:
                children += 1
            depth += 1
        cursor = step.end()
    return children


def check_artifacts(
    requirements: list[Requirement],
    worktree: Path,
    files: list[str] | None = None,
) -> ContractVerdict:
    """Check the produced files against the requirements.

    ``files`` is the set the run actually created or changed, and it is the
    **only** thing checked when it is given.  The distinction matters:

    * ``files=[]`` — the run produced nothing, so the verdict is "no artifact
      was produced".  This is what the product path passes, and it is the fix
      for the measured defect where a run that wrote nothing passed the
      contract on a file that was already in the repository;
    * ``files=None`` — the caller did not say what it produced (an ad-hoc check
      of a tree, a test).  Only then does the check fall back to the worktree's
      own text files, and that fallback is never used by a run.

    ``min_size`` is checked **per file**, not against the concatenation.  Five
    300-byte files are not a 1200-byte page.
    """
    root = Path(worktree)
    targets: list[Path] = []
    if files is None:
        # No claim about what was produced: inspect the tree.  A run never
        # takes this branch — it always knows its own written files.
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in {".git", "__pycache__", ".venv", "node_modules", ".gigaforge"}
                   for part in p.parts):
                continue
            if p.suffix.lower() in {".html", ".htm", ".css", ".js", ".md", ".txt", ".py"}:
                targets.append(p)
    else:
        for rel in files:
            p = root / str(rel)
            if p.is_file():
                targets.append(p)

    verdict = ContractVerdict(files=[str(p.relative_to(root)) for p in targets])
    if not requirements:
        return verdict
    if not targets:
        verdict.unmet.append("no artifact was produced")
        return verdict

    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in targets}
    size_constrained = any(req.kind in {"word_count", "char_count"} for req in requirements)
    if size_constrained and not any(req.kind == "substance" for req in requirements):
        placeholders = list(dict.fromkeys(
            phrase for text in texts.values() for phrase in find_placeholders(text)
        ))
        if placeholders:
            verdict.unmet.append(
                "complete text — replace placeholder phrases: "
                + ", ".join(f"'{phrase}'" for phrase in placeholders[:5])
            )
    for req in requirements:
        verdict.checked.append(req.label)
        if req.kind in {"word_count", "char_count"}:
            unit = "words" if req.kind == "word_count" else "characters"
            counts = [
                (
                    path,
                    len(re.findall(r"\b[\w'-]+\b", visible_text(text)))
                    if req.kind == "word_count"
                    else len(text),
                )
                for path, text in texts.items()
            ]
            if any(
                (req.minimum is None or count >= req.minimum)
                and (req.maximum is None or count <= req.maximum)
                for _path, count in counts
            ):
                verdict.met.append(req.label)
            else:
                path, count = min(
                    counts,
                    key=lambda item: (
                        max(0, (req.minimum or 0) - item[1])
                        + max(0, item[1] - (req.maximum or item[1])),
                        -len(texts[item[0]]),
                    ),
                )
                if (
                    req.minimum is not None
                    and req.maximum == req.minimum
                    and count != req.minimum
                ):
                    difference = abs(req.minimum - count)
                    direction = "add" if count < req.minimum else "remove"
                    quantity_unit = unit[:-1] if difference == 1 else unit
                    correction = (
                        f"{direction} exactly {difference} {quantity_unit} to reach "
                        f"{req.minimum}; make no other length-changing edits"
                    )
                elif req.minimum is not None and count < req.minimum:
                    shortage = req.minimum - count
                    if req.maximum is not None:
                        target = (req.minimum + req.maximum) // 2
                        correction = (
                            f"rewrite/expand toward about {target} {unit}, "
                            f"within {req.minimum}-{req.maximum}; add at least "
                            f"{shortage} complete {unit}"
                        )
                    else:
                        correction = (
                            f"add at least {shortage} complete {unit} to reach "
                            f"{req.minimum}; do not leave outlines or placeholders"
                        )
                else:
                    if req.minimum is not None and req.maximum is not None:
                        target = (req.minimum + req.maximum) // 2
                        correction = (
                            f"rewrite or condense rather than append; aim for "
                            f"about {target} {unit}, within {req.minimum}-{req.maximum}"
                        )
                    else:
                        correction = f"reduce it to at most {req.maximum}"
                verdict.unmet.append(
                    f"{req.label} (closest file {path.name} has {count} {unit}; "
                    f"{correction})"
                )
            continue
        if req.kind == "min_size":
            # Per file: the requirement is about *an* artifact, and summing
            # unrelated files is how five small files passed a 1200-byte check.
            biggest = max((len(t) for t in texts.values()), default=0)
            if biggest >= _MIN_ARTIFACT_BYTES:
                verdict.met.append(req.label)
            else:
                verdict.unmet.append(
                    f"{req.label} (largest file is {biggest} bytes)"
                )
            continue
        if req.kind == "source_count":
            best_entries = max(
                (_numbered_source_entries(text) for text in texts.values()),
                key=len,
                default=[],
            )
            valid_entries = sum(
                bool(re.search(r"https?://\S+", entry)) for entry in best_entries
            )
            if any(req.satisfied_by(text) for text in texts.values()):
                verdict.met.append(req.label)
            else:
                verdict.unmet.append(
                    f"{req.label} (found {len(best_entries)} numbered entries, "
                    f"{valid_entries} with URLs; put each source on its own numbered line)"
                )
            continue
        if req.kind == "heading":
            # A landing page without an <h1> has no headline, which is the one
            # thing a hero section exists to provide.  Measured live: a page
            # passed every check with no <h1> at all — the hero was a <section>
            # whose first child was an <h2>.
            found = any(
                # ``[^<\s]`` and not ``\S``: the closing ``</h1>`` starts with
                # ``<``, which ``\S`` happily matches, so an empty headline
                # passed the check.
                re.search(r"<h1\b[^>]*>\s*[^<\s]", text, re.I) for text in texts.values()
            )
            if found:
                verdict.met.append(req.label)
            else:
                verdict.unmet.append(f"{req.label} — no non-empty <h1> in the page")
            continue
        if req.kind == "singleton":
            # Count the role, not the content: ``<footer>`` and
            # ``<div class="footer">`` are the same landmark, and the two
            # footers a live run produced had different text.
            #
            # Count ELEMENTS, not matches.  The first version ran two regexes —
            # one for the tag, one for the class — and added them, so the
            # ordinary ``<footer class="footer">`` was counted twice and a page
            # with exactly one footer failed with "found 2 footer landmarks".
            # Measured by the new bench HTML fixture on its first run.  A single
            # alternation with a dedupe by match position counts each element
            # once however it is spelled.
            role = req.needles[0]
            element = re.compile(
                rf"<{role}\b"
                rf"|<[a-z][a-z0-9]*\b[^>]*\bclass\s*=\s*[\"'][^\"']*\b{role}\b",
                re.I,
            )
            worst = 0
            for text in texts.values():
                # ``texts`` includes the stylesheet; a CSS rule mentioning
                # ``footer`` is not a landmark, so only the markup is counted.
                markup = re.sub(r"<style\b[^>]*>.*?</style\s*>", "", text,
                                flags=re.S | re.I)
                positions = {match.start() for match in element.finditer(markup)}
                worst = max(worst, len(positions))
            if worst <= 1:
                verdict.met.append(req.label)
            else:
                verdict.unmet.append(
                    f"{req.label} (found {worst} {role} landmarks)"
                )
            continue
        if req.kind == "card_count":
            # Count the cards in the markup, accepting every spelling a child
            # plausibly writes.  Measured live: the report said "cards: 0"
            # while four cards were on screen, because the children wrote
            # ``.feature-item`` and the base sheet styled ``.feature-card``.
            # The count is a fact about the file; the class name is not.
            #
            # A child may also name its cards something the harness never
            # guessed — measured live, a section child wrote
            # ``<div class="grid-item">`` and the contract failed a page that
            # had four cards in a grid.  So the structural reading is tried
            # too: the direct children of the grid container are the cards,
            # whatever they are called.
            best = 0
            for text in texts.values():
                found = 0
                for needle in req.needles:
                    found = max(found, len(re.findall(
                        rf'class="[^"]*\b{re.escape(needle)}\b', text, re.I)))
                best = max(best, found, _count_grid_children(text))
            # The threshold is the number the goal asked for, not a constant.
            # A page that ships three cards for a "4 cards" goal is short by
            # one, and the contract must say so.
            needed = int(req.minimum or 3)
            if best >= needed:
                verdict.met.append(req.label)
            else:
                verdict.unmet.append(
                    f"{req.label} (found {best} card(s) in the markup)"
                )
            continue
        if req.kind == "substance":
            # Name the offending phrases: "add real content" is not actionable,
            # "replace 'Feature Card 1' with a real feature description" is.
            offenders: list[str] = []
            for text in texts.values():
                offenders.extend(find_placeholders(text))
            if offenders:
                unique = list(dict.fromkeys(offenders))[:5]
                verdict.unmet.append(
                    f"{req.label} — found placeholders: "
                    + ", ".join(f"'{p}'" for p in unique)
                )
            else:
                longest = max((len(visible_text(t)) for t in texts.values()), default=0)
                if longest >= _MIN_VISIBLE_TEXT_CHARS:
                    verdict.met.append(req.label)
                else:
                    verdict.unmet.append(
                        f"{req.label} — only {longest} characters of visible text"
                    )
            continue
        if any(req.satisfied_by(text) for text in texts.values()):
            verdict.met.append(req.label)
        else:
            verdict.unmet.append(req.label)
    return verdict

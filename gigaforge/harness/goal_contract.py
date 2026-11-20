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
    body = _SCRIPT_STYLE_RE.sub(" ", text)
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


@dataclass(frozen=True)
class Requirement:
    """One explicit, machine-checkable thing the goal asked for."""

    kind: str  # "colour" | "section" | "self_contained" | "min_size"
    label: str
    needles: tuple[str, ...] = ()

    def satisfied_by(self, text: str) -> bool:
        low = strip_comments(text).lower()
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
            "Use write_file to rewrite the file with the missing pieces, "
            "then finish."
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

    for word, needles in _COLOUR_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            reqs.append(Requirement("colour", f"colour '{word}'", needles))

    for word, needles in _SECTION_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            reqs.append(Requirement("section", f"section '{word}'", needles))

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
    for req in requirements:
        verdict.checked.append(req.label)
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

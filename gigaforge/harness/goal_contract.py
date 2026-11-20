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
_COLOUR_WORDS: dict[str, tuple[str, ...]] = {
    "cyan": ("cyan", "#0ff", "#00ffff", "aqua"),
    "magenta": ("magenta", "#f0f", "#ff00ff", "fuchsia"),
    "purple": ("purple", "#800080", "#a020f0", "#9d00ff"),
    "violet": ("violet", "#8a2be2", "#7f00ff"),
    "orange": ("orange", "#ffa500", "#ff7f00"),
    "green": ("green", "#0f0", "#00ff00", "lime"),
    "red": ("red", "#f00", "#ff0000"),
    "blue": ("blue", "#00f", "#0000ff"),
    "yellow": ("yellow", "#ff0", "#ffff00"),
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
                "min_size",
                f"artifact of at least {_MIN_ARTIFACT_BYTES} bytes",
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
        if any(req.satisfied_by(text) for text in texts.values()):
            verdict.met.append(req.label)
        else:
            verdict.unmet.append(req.label)
    return verdict

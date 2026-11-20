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
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Colour words a goal may name, mapped to the CSS spellings that satisfy them.
#:
#: Only words that appear *in the goal* become requirements — this is not a
#: style guide, it is a checklist of what the user said.
_COLOUR_WORDS: dict[str, tuple[str, ...]] = {
    "cyan": ("cyan", "#0ff", "#00ffff", "aqua", "#0ff"),
    "magenta": ("magenta", "#f0f", "#ff00ff", "fuchsia"),
    "neon": ("neon", "text-shadow", "box-shadow", "drop-shadow"),
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


@dataclass(frozen=True)
class Requirement:
    """One explicit, machine-checkable thing the goal asked for."""

    kind: str  # "colour" | "section" | "self_contained" | "min_size"
    label: str
    needles: tuple[str, ...] = ()

    def satisfied_by(self, text: str) -> bool:
        low = text.lower()
        if self.kind == "self_contained":
            # Any absolute URL in a src/href is an external asset.
            return not re.search(r'(?:src|href)\s*=\s*["\']https?://', low)
        if self.kind == "min_size":
            return len(text) >= _MIN_ARTIFACT_BYTES
        return any(n in low for n in self.needles)


@dataclass
class ContractVerdict:
    """What the artifact does and does not satisfy."""

    checked: list[str] = field(default_factory=list)
    unmet: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

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

    ``files`` is the set the episode actually wrote.  When it is empty the
    check falls back to the worktree's own text files, because a run that
    produced nothing must not pass by having nothing to inspect.
    """
    root = Path(worktree)
    targets: list[Path] = []
    if files:
        for rel in files:
            p = root / rel
            if p.is_file():
                targets.append(p)
    if not targets:
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in {".git", "__pycache__", ".venv", "node_modules", ".gigaforge"}
                   for part in p.parts):
                continue
            if p.suffix.lower() in {".html", ".htm", ".css", ".js", ".md", ".txt", ".py"}:
                targets.append(p)

    verdict = ContractVerdict(files=[str(p.relative_to(root)) for p in targets])
    if not requirements:
        return verdict
    if not targets:
        verdict.unmet.append("no artifact was produced")
        return verdict

    text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in targets)
    for req in requirements:
        verdict.checked.append(req.label)
        if not req.satisfied_by(text):
            verdict.unmet.append(req.label)
    return verdict

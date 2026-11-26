"""The leakage screen: reject a change that encodes the task instead of solving it.

**Why this module exists.**  GigaForge improves itself — the ladder, the
contracts, the prompts, the skills.  Every one of those improvements is
measured on a set of tasks, and a harness that is measured on a fixed set will
eventually learn the set.  The failure is not a crash; it is a green bench and a
worse product:

    if goal == "build a cyberpunk landing page":
        return _the_cyberpunk_page_we_already_know_works()

That passes every fixture and helps nobody.  RRSI calls the fix a *critic*: a
screen that runs **before** the score, so a leaked candidate is never evaluated
at all.  Evaluating it first and rejecting it after is worse than useless — the
score is what the search optimises, so a leaked candidate that scores well
teaches the search to leak more.

**What counts as leakage.**  Three shapes, all deterministic to detect:

1. **A task name in executable code.**  The fixture's name, its file name, its
   goal string, appearing in a branch, a comparison or a literal that is not a
   comment or a docstring.  A comment may name the task — that is documentation.
   A condition may not — that is a lookup table.
2. **A value lifted from the goal.**  A long literal from the goal text (a
   colour, a heading, a product name) hardcoded where the harness should have
   derived it.  Short common words are excluded: "page" is not leakage.
3. **Suite-specific logic.**  Markers that only make sense against one
   benchmark: ``if task ==``, ``if name in {``, ``expected_output``,
   ``golden``, ``fixture_name``.

**What it must not do.**  It must not reject a legitimate change.  A harness
that refuses to mention "html" because a fixture is about HTML is a harness that
cannot do its job.  So the screen is deliberately narrow: it looks for the
*combination* of a task-specific token and an executable position, and it
reports a finding with the line so a human can judge.  A finding is a reason to
look, and for the evolution loop it is a reason to discard — never a silent
rewrite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "LeakageFinding",
    "LeakageReport",
    "screen",
    "screen_diff",
    "DEFAULT_SUITE_MARKERS",
]


#: Markers that only make sense against one benchmark.  Each is a regex over a
#: single line.  Kept short and specific: a broad pattern here rejects honest
#: code, and a screen that cries wolf is a screen that gets switched off.
DEFAULT_SUITE_MARKERS: tuple[tuple[str, str], ...] = (
    (r"\bif\s+task\s*==", "branches on the task name"),
    (r"\bif\s+goal\s*==", "branches on the goal string"),
    (r"\bif\s+name\s+in\s*\{", "branches on a set of task names"),
    (r"\bexpected_output\b", "compares against a stored expected output"),
    (r"\bgolden\b", "compares against a golden file"),
    (r"\bfixture_name\b", "reads the fixture's own name"),
    (r"\bbench_task\b", "reads the benchmark's task object"),
    (r"\bif\s+.*\bin\s+_?TASKS?\b", "branches on the task registry"),
)

#: Words too common to be evidence of leakage.  A goal says "build a landing
#: page"; a harness that mentions "page" is doing its job.
_STOPWORDS = frozenset(
    """
    a an the and or but if then else for to of in on at by with from into
    build create make write add fix update change remove delete generate
    page site website file files code test tests run use using new old
    this that these those it its is are was were be been being do does did
    please can could should would will shall may might must
    """.split()
)

#: A literal must be at least this long to count as "lifted from the goal".
#: Shorter strings collide with ordinary identifiers.
_MIN_LITERAL = 6


@dataclass(frozen=True)
class LeakageFinding:
    """One reason to suspect a change encodes the task rather than solving it."""

    kind: str
    detail: str
    line: int = 0
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "line": self.line,
            "text": self.text[:200],
        }


@dataclass
class LeakageReport:
    """The screen's verdict on one candidate."""

    findings: list[LeakageFinding] = field(default_factory=list)

    @property
    def leaked(self) -> bool:
        return bool(self.findings)

    @property
    def clean(self) -> bool:
        return not self.findings

    def reasons(self) -> list[str]:
        return [f"{f.kind}: {f.detail}" for f in self.findings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "leaked": self.leaked,
            "findings": [f.to_dict() for f in self.findings],
        }


def _code_lines(text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(line_number, line)`` for lines that are *executable*.

    Comments and docstrings are documentation, and documentation is allowed to
    name the task — that is how a future reader learns why the code exists.  A
    screen that rejected those would force the code to be unexplained.
    """
    in_docstring: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        stripped = line.strip()
        if in_docstring is not None:
            if in_docstring in stripped:
                in_docstring = None
            continue
        for quote in ('"""', "'''"):
            if stripped.startswith(quote):
                # A one-line docstring closes on the same line.
                if stripped.count(quote) >= 2:
                    break
                in_docstring = quote
                break
        else:
            quote = None
        if in_docstring is not None and quote is not None:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        yield number, line


def _goal_tokens(goal: str) -> list[str]:
    """Long, distinctive words from the goal — the ones worth hardcoding.

    Hex colours are included even though they are not words: ``#00ffcc`` is
    exactly the kind of value a harness should *derive* from the goal and a
    leaked candidate hardcodes.  Measured: the first version tokenised only
    ``[A-Za-z][A-Za-z0-9_-]+``, so a hardcoded ``ACCENT = "#00ffcc"`` passed the
    screen — the single most likely leak in a page generator.
    """
    tokens: list[str] = []
    for colour in re.findall(r"#[0-9a-fA-F]{3,8}\b", goal or ""):
        if colour.lower() not in tokens:
            tokens.append(colour.lower())
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]+", goal or ""):
        lowered = word.lower()
        if len(lowered) < _MIN_LITERAL or lowered in _STOPWORDS:
            continue
        if lowered not in tokens:
            tokens.append(lowered)
    return tokens


def screen(
    text: str,
    *,
    goal: str = "",
    task_names: Sequence[str] = (),
    suite_markers: Sequence[tuple[str, str]] = DEFAULT_SUITE_MARKERS,
    extra_tokens: Sequence[str] = (),
) -> LeakageReport:
    """Screen one candidate change for task leakage.

    ``text`` is the candidate — a diff, a prompt, a module.  ``goal`` is the
    task it was produced for; ``task_names`` are the fixtures it will be scored
    on.  The screen is deterministic and model-free: it is a filter, not a
    judge, and it must be cheap enough to run on every candidate.
    """
    report = LeakageReport()
    if not text:
        return report

    names = [n.strip() for n in task_names if n and n.strip()]
    tokens = _goal_tokens(goal)
    for extra in extra_tokens:
        extra = (extra or "").strip().lower()
        if len(extra) >= _MIN_LITERAL and extra not in tokens:
            tokens.append(extra)

    for number, line in _code_lines(text):
        lowered = line.lower()

        # 1. A task name in an executable position.
        for name in names:
            if name.lower() in lowered:
                report.findings.append(LeakageFinding(
                    kind="task_name",
                    detail=f"the task name {name!r} appears in executable code",
                    line=number,
                    text=line,
                ))
                break

        # 2. Suite-specific logic.
        for pattern, why in suite_markers:
            if re.search(pattern, line):
                report.findings.append(LeakageFinding(
                    kind="suite_logic",
                    detail=why,
                    line=number,
                    text=line,
                ))
                break

        # 3. A distinctive value lifted from the goal, as a string literal.
        for literal in re.findall(r"""["']([^"'\n]{6,})["']""", line):
            if literal.lower() in tokens:
                report.findings.append(LeakageFinding(
                    kind="goal_literal",
                    detail=(
                        f"the literal {literal!r} is lifted from the goal; "
                        "the harness should derive it"
                    ),
                    line=number,
                    text=line,
                ))
                break

    return report


def screen_diff(
    diff: str,
    *,
    goal: str = "",
    task_names: Sequence[str] = (),
    suite_markers: Sequence[tuple[str, str]] = DEFAULT_SUITE_MARKERS,
) -> LeakageReport:
    """Screen only the **added** lines of a unified diff.

    A removed line is not a leak — it is the leak being taken out.  Screening
    the whole diff would reject the very change that fixes a leak, which is the
    kind of inversion that makes a guard useless.
    """
    added: list[str] = []
    for line in (diff or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return screen(
        "\n".join(added),
        goal=goal,
        task_names=task_names,
        suite_markers=suite_markers,
    )

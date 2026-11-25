"""The task brief — facts the harness computes so the model does not guess.

**Why this module exists.**  Every live run paid tokens for the model to work
out things the harness already knew or could measure in microseconds:

* whether the file the goal names exists, and how big it is;
* what language the repository is written in, and how many files it has;
* which test command this project uses;
* whether the goal forbids changes ("do not change any files");
* which paths the goal names at all.

The model guessed at these, and a guess about the tree is the most expensive
kind: a wrong "the file exists" produces an edit to a file that is not there, a
wrong "there are tests" produces a `run_tests` call that fails, and a wrong
"this is a page goal" produces a landing page for a database fix.  The
classifier already answers the last one; the rest are a `stat` and a `glob`.

**What a brief is.**  A short, structured block of *measured* facts, rendered
into the system prefix.  It is not advice and it is not a plan: every line is
something the harness observed, and a fact it could not observe is absent
rather than guessed.  That distinction is the whole point — a brief that says
"3 files, Python, pytest" is useful; one that says "probably a Python project"
is a guess wearing a fact's clothes.

**Why it is cheap.**  The brief is built once per run, from the same
filesystem walk the classifier already does, and it is bounded: at most
``max_paths`` named paths and ``max_files`` repository files are listed.  A
brief that grows with the repository would be a second repo map, and the repo
map already exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["TaskBrief", "build_brief", "render_brief"]

#: Extensions that identify a language, for the one-line summary.
_LANGUAGES: dict[str, str] = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".c": "C",
    ".h": "C", ".cpp": "C++", ".swift": "Swift", ".scala": "Scala",
}

#: Test-runner markers, checked in order.  The first match wins, because a
#: repository with both a `pyproject.toml` and a `Makefile` is a Python project
#: whose Makefile happens to exist.
_TEST_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "pytest"),
    ("pytest.ini", "pytest"),
    ("tox.ini", "pytest"),
    ("setup.cfg", "pytest"),
    ("package.json", "npm test"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
    ("pom.xml", "mvn test"),
    ("build.gradle", "gradle test"),
    ("Makefile", "make test"),
)

#: Phrases that forbid a change.  Duplicated from the classifier on purpose:
#: the classifier uses them to *route*, the brief uses them to *state a
#: constraint to the model*, and a shared constant would couple two decisions
#: that must be able to diverge.
_NO_CHANGE = (
    "do not change", "don't change", "do not edit", "don't edit",
    "do not modify", "don't modify", "do not touch", "don't touch",
    "no file changes", "no changes", "without changing", "read-only",
    "read only", "answer in chat", "just answer", "only answer",
)

_PATH_RE = re.compile(r"[A-Za-z0-9_][\w./\\-]*\.[A-Za-z0-9]{1,8}\b")

#: A bare word that could be a directory name.  ``_PATH_RE`` requires an
#: extension, so "look at src" named no path at all and the brief said nothing
#: about a directory the goal was pointing at.  A bare token is only treated as
#: a path when it actually exists as a directory in the tree — otherwise every
#: English word would become a "missing path".
_BARE_RE = re.compile(r"\b[A-Za-z_][\w-]{1,40}\b")


@dataclass(frozen=True)
class NamedPath:
    """One path the goal names, and what the filesystem says about it."""

    path: str
    exists: bool
    is_dir: bool = False
    size_bytes: int = 0
    lines: int = 0
    language: str = ""

    def describe(self) -> str:
        if not self.exists:
            return f"`{self.path}` — does not exist (the run must create it)"
        if self.is_dir:
            return f"`{self.path}` — directory"
        bits = [f"{self.size_bytes} bytes"]
        if self.lines:
            bits.append(f"{self.lines} lines")
        if self.language:
            bits.append(self.language)
        return f"`{self.path}` — exists, " + ", ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "is_dir": self.is_dir,
            "size_bytes": self.size_bytes,
            "lines": self.lines,
            "language": self.language,
        }


@dataclass(frozen=True)
class TaskBrief:
    """Measured facts about the task and the tree it will run against."""

    #: The classifier's verdict and the evidence behind it.
    task_kind: str = ""
    task_evidence: str = ""
    #: Paths the goal names, resolved against the tree.
    named_paths: tuple[NamedPath, ...] = ()
    #: Repository shape: how many files, which languages, how big.
    file_count: int = 0
    languages: tuple[tuple[str, int], ...] = ()
    #: The test command this project uses, or empty when none was found.
    test_command: str = ""
    #: True when the goal explicitly forbids changing files.
    read_only: bool = False
    #: True when the goal asks for a reply rather than a change.
    wants_answer: bool = False
    #: Anything the harness measured that does not fit the fields above.
    notes: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.task_kind
            or self.named_paths
            or self.file_count
            or self.test_command
            or self.read_only
            or self.wants_answer
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_kind": self.task_kind,
            "task_evidence": self.task_evidence,
            "named_paths": [p.to_dict() for p in self.named_paths],
            "file_count": self.file_count,
            "languages": [list(pair) for pair in self.languages],
            "test_command": self.test_command,
            "read_only": self.read_only,
            "wants_answer": self.wants_answer,
            "notes": list(self.notes),
        }


def _language_of(path: Path) -> str:
    return _LANGUAGES.get(path.suffix.lower(), "")


def _count_lines(path: Path, *, limit: int = 2_000_000) -> int:
    """Line count, bounded so a huge file cannot stall the brief."""
    try:
        if path.stat().st_size > limit:
            return 0
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _named_paths(goal: str, *, root: Path | None = None) -> list[str]:
    """Path-like tokens in the goal, de-duplicated, order preserved.

    Two tiers, because a goal names paths in two ways:

    * a token with an extension (``db.py``, ``docs/design.md``) is a path
      whether or not it exists — that is exactly the case the brief must
      report as "does not exist, the run must create it";
    * a bare token (``src``) is a path only when it exists as a directory.
      Without that check every English word would become a missing path, and a
      brief full of "``the`` — does not exist" is noise the model learns to
      ignore.
    """
    seen: dict[str, None] = {}
    for match in _PATH_RE.finditer(goal or ""):
        token = match.group(0).replace("\\", "/").lstrip("./")
        if token and token not in seen:
            seen[token] = None
    if root is not None:
        for match in _BARE_RE.finditer(goal or ""):
            token = match.group(0)
            if token in seen:
                continue
            try:
                if (root / token).is_dir():
                    seen[token] = None
            except OSError:
                continue
    return list(seen)


def _walk(root: Path, *, max_files: int) -> tuple[int, dict[str, int]]:
    """Count files and languages, skipping the directories that are not source."""
    skip = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", "dist", "build", ".gigaforge", ".tox",
        "site-packages", ".idea", ".vscode",
    }
    count = 0
    languages: dict[str, int] = {}
    for path in root.rglob("*"):
        if count >= max_files:
            break
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if any(part in skip for part in path.parts):
            continue
        count += 1
        language = _language_of(path)
        if language:
            languages[language] = languages.get(language, 0) + 1
    return count, languages


def _test_command(root: Path) -> str:
    for marker, command in _TEST_MARKERS:
        try:
            if (root / marker).is_file():
                return command
        except OSError:
            continue
    return ""


def build_brief(
    goal: str,
    *,
    repo_root: Path | str | None = None,
    task_kind: str = "",
    task_evidence: str = "",
    max_paths: int = 8,
    max_files: int = 5_000,
) -> TaskBrief:
    """Measure the task and the tree, and return the facts.

    ``task_kind``/``task_evidence`` are passed in rather than recomputed: the
    classifier already ran, and running it twice would be two answers to one
    question.  Everything else is measured here.
    """
    text = goal or ""
    low = text.lower()
    root = Path(repo_root) if repo_root is not None else None

    named: list[NamedPath] = []
    for rel in _named_paths(text, root=root)[:max_paths]:
        if root is None:
            named.append(NamedPath(path=rel, exists=False))
            continue
        target = root / rel
        try:
            if target.is_dir():
                named.append(NamedPath(path=rel, exists=True, is_dir=True))
            elif target.is_file():
                named.append(
                    NamedPath(
                        path=rel,
                        exists=True,
                        size_bytes=target.stat().st_size,
                        lines=_count_lines(target),
                        language=_language_of(target),
                    )
                )
            else:
                named.append(NamedPath(path=rel, exists=False))
        except OSError:
            named.append(NamedPath(path=rel, exists=False))

    file_count = 0
    languages: tuple[tuple[str, int], ...] = ()
    test_command = ""
    if root is not None and root.is_dir():
        file_count, language_counts = _walk(root, max_files=max_files)
        languages = tuple(
            sorted(language_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:4]
        )
        test_command = _test_command(root)

    read_only = any(phrase in low for phrase in _NO_CHANGE)
    wants_answer = task_kind == "answer"

    notes: list[str] = []
    if read_only:
        notes.append(
            "the goal forbids changing files: produce a reply, not a diff"
        )
    if wants_answer:
        notes.append("the goal asks for a reply: the run's output is text")

    return TaskBrief(
        task_kind=task_kind,
        task_evidence=task_evidence,
        named_paths=tuple(named),
        file_count=file_count,
        languages=languages,
        test_command=test_command,
        read_only=read_only,
        wants_answer=wants_answer,
        notes=tuple(notes),
    )


def render_brief(brief: TaskBrief) -> str:
    """Render the brief as a prompt block, or ``""`` when there is nothing to say.

    Every line is a measurement.  A brief with no facts renders to nothing
    rather than to a heading with an empty body — an empty section in a system
    prompt is tokens spent to say nothing.
    """
    if brief.is_empty:
        return ""

    lines: list[str] = ["## Task brief (measured, not guessed)"]

    if brief.task_kind:
        line = f"- Task class: **{brief.task_kind}**"
        if brief.task_evidence:
            line += f" — {brief.task_evidence}"
        lines.append(line)

    if brief.named_paths:
        lines.append("- Paths the goal names:")
        for named in brief.named_paths:
            lines.append(f"  - {named.describe()}")

    if brief.file_count:
        shape = f"- Repository: {brief.file_count} files"
        if brief.languages:
            shape += " (" + ", ".join(f"{name} {count}" for name, count in brief.languages) + ")"
        lines.append(shape)

    if brief.test_command:
        lines.append(f"- Test command: `{brief.test_command}`")

    for note in brief.notes:
        lines.append(f"- {note}")

    return "\n".join(lines)

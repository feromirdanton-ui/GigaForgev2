"""What kind of work a goal actually asks for — decided by the filesystem.

**Why this module exists.**  The harness used to decide "is this a page goal?"
by looking for a substring in the goal text::

    _PAGE_HINTS = ("landing page", ..., "site", "html")
    return any(hint in low for hint in _PAGE_HINTS)

``"site" in low`` is not a word check, it is a substring check, and it is true
for *composite*, *parasite*, *opposite* and *site-wide*.  Measured on the real
``TaskPipeline``: the goal "Fix the composite index in db.py" was routed to
landing-page generation, the model's correct ``db.py`` diff was thrown away, an
``index.html`` about "Composite" was written, and ``final_diff`` described that
file.  The run did the wrong work and reported the wrong artifact.

The routing question has a deterministic answer, and it is not in the prose: a
page goal **names an ``.html`` path**.  Whether that path is already in the
repository decides *which* artifact workflow runs, not whether one runs at all:

* absent → ``artifact`` — the run is asked to create the page;
* present → ``artifact_edit`` — the run is asked to change a page that exists.

**Why the second case is its own kind.**  The first version of this module
returned ``code`` for an existing ``.html`` path, on the reasoning that "the
file is there, so this is a change to a file".  Measured on the real pipeline:

    code | Update index.html to add a footer and a hero section
    code | Add a pricing section to index.html
    code | Redesign the landing page in index.html

and the consequence chained: ``_sectional_generation`` never ran, so there was
no section plan; ``ctx.extras["generation"]`` stayed empty, so
``requirements_from_plan`` was never called; the contract fell back to
``extract_requirements``, which returns two bare requirements for "add a footer
and a hero section" and none of ``min_size``/``substance``/``heading`` (those
are only added at three or more).  A page being *edited* was therefore graded
more weakly than a page being *created* — and editing is the common case: you
create a page once and change it many times.

So an existing artifact path is ``artifact_edit``: the same sectional workflow,
seeded with the file's current content, and the same contract.

Everything else is a guess, and a guess is what produced the defect.

So the decision is made in this order, and each step is evidence:

1. **Paths named in the goal, resolved against the repository.**
   * an ``.html``/``.htm``/``.css`` path that is *absent* → ``artifact``;
   * an ``.html``/``.htm``/``.css`` path that *exists* → ``artifact_edit``;
   * a code path that exists → code work;
   * a code path that is absent → code work (creating a module is still code).
2. **Explicit page phrases**, only when no path was named at all:
   ``landing page``, ``web page``, ``webpage``, ``website``, ``web site``,
   ``single-file page``, ``static page``, ``home page``.
   Bare ``site`` and bare ``html`` are deliberately **not** in this list: they
   are the two words that caused the defect, and a goal that means a page can
   say "website" or name the file.
3. **Default: code.**  The safe default, because a wrong "code" verdict edits
   a file the user named, while a wrong "artifact" verdict writes a stray
   ``index.html`` and discards the real work.

The verdict carries ``evidence`` — a sentence naming the paths it looked at and
what it found — so the ledger and the report can show *why* a path was chosen
instead of asserting it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = ["TaskKind", "classify_task", "is_artifact_goal", "PAGE_PHRASES"]

#: Suffixes that mean "this is a rendered artifact, not source code".
_ARTIFACT_SUFFIXES = frozenset({".html", ".htm", ".css"})

#: Suffixes worth resolving against the tree.  A path the goal names that is
#: not in this set is ignored rather than guessed at.
_KNOWN_SUFFIXES = frozenset(
    {
        ".html", ".htm", ".css", ".md", ".txt", ".json", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs",
        ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".c", ".h", ".cpp",
        ".sql", ".sh", ".bat", ".ps1", ".env", ".lock",
    }
)

#: A path-like token: at least one dot, no spaces, no quotes.
_PATH_RE = re.compile(r"[A-Za-z0-9_][\w./\\-]*\.[A-Za-z0-9]{1,8}\b")

#: Verbs that ask for a *change to the tree*.  A goal containing one of these is
#: never an ``answer`` goal, whatever else it says: "explain and fix the bug in
#: app.py" is code work with an explanation attached, not a chat reply.
_MUTATING_VERBS: tuple[str, ...] = (
    "add", "create", "write", "build", "make", "generate", "implement",
    "fix", "repair", "patch", "refactor", "rename", "move", "delete",
    "remove", "update", "change", "edit", "modify", "replace", "rewrite",
    "migrate", "install", "configure", "set up", "setup", "scaffold",
    "оптимизир", "исправ", "добав", "созда", "напиши", "написать",
    "перепиши", "удали", "измени", "обнови", "почини", "сгенерир",
    "реализуй", "отрефактор", "переименуй", "внедри", "настрой",
)

#: Phrases that ask for a *reply*, not a change.  These are checked only after
#: the filesystem and the mutating verbs have had their say, so a goal that
#: names a file to change is never demoted to a chat answer.
_ANSWER_PHRASES: tuple[str, ...] = (
    "what is", "what's", "what are", "who is", "who was", "when did",
    "where is", "why does", "why is", "how does", "how do", "how many",
    "how much", "explain", "describe", "summarise", "summarize", "tell me",
    "find out", "look up", "search for", "search the web", "research",
    "compare", "list the", "give me a summary", "answer in chat",
    "do not change", "don't change", "no file changes", "without changing",
    "что такое", "кто такой", "когда", "почему", "как работает",
    "объясни", "опиши", "расскажи", "найди в интернете", "поищи",
    "сравни", "сколько", "перечисли", "дай ответ", "ответь",
)

#: Phrases that explicitly forbid a change.  They contain a mutating verb
#: ("change", "edit") and mean its negation, so they are removed from the text
#: before the verb scan — otherwise the clearest possible request for a reply
#: ("do not change any files") is read as a request to edit.
_NO_CHANGE_PHRASES: tuple[str, ...] = (
    "do not change", "don't change", "do not edit", "don't edit",
    "do not modify", "don't modify", "do not touch", "don't touch",
    "no file changes", "no changes", "without changing", "without editing",
    "without modifying", "read-only", "read only",
    "не меняй", "не изменяй", "не трогай", "без изменений", "только ответь",
)

#: Phrases that mean "produce a page" when the goal names no path at all.
#: Bare ``site`` and bare ``html`` are excluded on purpose — see the module
#: docstring; they are the substrings that mis-routed "composite" and
#: "html parser".
PAGE_PHRASES: tuple[str, ...] = (
    "landing page",
    "web page",
    "webpage",
    "website",
    "web site",
    "single-file page",
    "single file page",
    "static page",
    "home page",
    "homepage",
    "one-page site",
    "one page site",
)


@dataclass(frozen=True)
class TaskKind:
    """The harness's verdict about what a goal asks for, with its evidence."""

    kind: Literal["code", "artifact", "artifact_edit", "mixed"]
    #: Artifact paths the goal names that are **absent** from the repository.
    artifact_paths: tuple[str, ...] = ()
    #: Artifact paths the goal names that **exist** in the repository — the
    #: page is being edited, not created.
    edit_paths: tuple[str, ...] = ()
    #: Paths the goal names that **exist** in the repository and are code.
    code_paths: tuple[str, ...] = ()
    #: Why this verdict — written to the ledger, shown in the report.
    evidence: str = ""
    #: Paths named in the goal that could not be resolved either way.
    unresolved: tuple[str, ...] = field(default=())

    @property
    def is_artifact(self) -> bool:
        return self.kind in ("artifact", "artifact_edit", "mixed")

    @property
    def is_edit(self) -> bool:
        """True when the artifact already exists and is being changed."""
        return self.kind == "artifact_edit"

    @property
    def is_code(self) -> bool:
        return self.kind in ("code", "mixed")

    @property
    def is_answer(self) -> bool:
        """True when the goal asks for a reply, not a change to the tree.

        This is the dimension the harness did not have.  Every run had to end
        in a unified diff, so a goal whose correct output is a sentence was
        either rejected ("diff could not be parsed") or died in the planner
        ("did not produce an executable plan: steps: 0").  Measured live on
        ``"What is 2+2? Answer in chat, do not change any files."``: the run
        failed at ``plan`` with ``steps: 0``.
        """
        return self.kind == "answer"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "artifact_paths": list(self.artifact_paths),
            "edit_paths": list(self.edit_paths),
            "code_paths": list(self.code_paths),
            "unresolved": list(self.unresolved),
            "evidence": self.evidence,
        }


def _named_paths(goal: str) -> list[str]:
    """Path-like tokens in the goal, normalised and de-duplicated, in order."""
    out: list[str] = []
    for match in _PATH_RE.finditer(goal or ""):
        raw = match.group(0).replace("\\", "/").strip()
        # ``e.g.`` and ``i.e.`` are prose, not paths.
        if raw.lower() in {"e.g", "i.e", "etc"}:
            continue
        suffix = Path(raw).suffix.lower()
        if suffix not in _KNOWN_SUFFIXES:
            continue
        # A leading ``./`` or ``/`` is noise for a repo-relative path.
        cleaned = raw.lstrip("./")
        if not cleaned or cleaned in out:
            continue
        out.append(cleaned)
    return out


def _repo_anchor(root: Path) -> tuple[Path, Path] | None:
    """Where to ask, and which directory *root* corresponds to in that tree.

    Returns ``(anchor, base)``:

    * ``anchor`` — the directory whose committed state answers the question;
    * ``base`` — the directory in *anchor*'s tree that *root* stands for.

    Normally both are *root* itself.  But a goal loop does not run against the
    user's project: it runs against a **copy** of it
    (``<project>/.gigaforge/workspaces/<goal>/project``), and the copy excludes
    ``.git`` on purpose — a nested repository would make every git command in
    the run answer about the wrong tree.

    The consequence was a live defect: ``git -C <copy> rev-parse --show-toplevel``
    walked *up* to the user's repository, and the copy's own path
    (``.gigaforge/workspaces/.../project/index.html``) is not in that
    repository's HEAD — so a committed page was reported as "not committed",
    "Update index.html" was classified as a *creation*, the existing page was
    never read, and the run regenerated it from scratch, losing the hero and the
    grid the user already had.

    The copy's manifest records where it came from, so for a copy the anchor is
    the destination and the base is the destination's root.

    **Why the base is separate from the anchor.**  ``git -C <dir> ls-tree --
    <path>`` interprets ``<path>`` relative to ``<dir>``, not to the repository
    root.  Asking from ``pages/`` about ``about.html`` must resolve to
    ``pages/about.html`` in the tree, not ``about.html``.  Returning one value
    for both jobs got that wrong.
    """
    current = root.resolve()
    for candidate in (current, *current.parents):
        workspaces = candidate / ".gigaforge" / "workspaces"
        if workspaces.is_dir():
            for entry in sorted(workspaces.iterdir()):
                record = entry / "manifest.json"
                if not record.is_file():
                    continue
                try:
                    data = json.loads(record.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                destination = data.get("destination")
                workspace = data.get("workspace")
                if not destination or not workspace:
                    continue
                try:
                    inside = current.is_relative_to(Path(workspace).resolve())
                except (OSError, ValueError):
                    inside = False
                if inside:
                    # The copy stands for the destination's root.
                    return Path(destination).resolve(), Path(destination).resolve()
        if (candidate / ".git").exists():
            return candidate, current
    return None


def _tracked_in_head(root: Path, rel: str) -> bool | None:
    """Is *rel* part of the repository's committed state?

    ``None`` when the question cannot be answered (not a git repo, no git
    binary) — the caller then falls back to the working tree.

    **Why HEAD and not the working tree.**  A goal loop runs several cycles
    against the same tree, and cycle 1 writes the page.  On cycle 2 the file
    exists on disk, so a working-tree check says "this path exists → code work"
    and the run stops generating sections — measured live: cycle 1 built a page
    section by section, cycle 2 re-classified the goal as code and the model
    rewrote the whole file in one answer, losing the grid and the palette.  The
    question the routing asks is "is this path part of the repository the user
    gave me", and the answer to that is the committed state, not the run's own
    scratch output.

    **Why the toplevel is resolved first.**  ``git -C <dir> ls-tree -- <path>``
    interprets ``<path>`` relative to ``<dir>``, not to the repository root.  A
    goal loop runs the pipeline against a *copy* of the project
    (``.gigaforge/workspaces/<goal>/project``), which is a subdirectory of the
    repository, so ``ls-tree -- index.html`` from there matched nothing and the
    function answered "not committed" for a file that was committed.  Measured
    live: "Update index.html to add a pricing section" was classified
    ``artifact`` (create) instead of ``artifact_edit``, the existing page was
    never read, and the run regenerated it from scratch — losing the hero and
    the grid the user already had.  Resolving the toplevel and re-relativising
    the path is what makes the answer independent of where the pipeline runs.
    """
    try:
        import subprocess

        anchor = _repo_anchor(root)
        if anchor is None:
            return None
        ask_from, base = anchor
        top = subprocess.run(
            ["git", "-C", str(ask_from), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if top.returncode != 0 or not top.stdout.strip():
            return None
        toplevel = Path(top.stdout.strip())
        try:
            rel_to_top = (base / rel).resolve().relative_to(toplevel.resolve())
        except (ValueError, OSError):
            # The path is outside the repository: it cannot be committed in it.
            return False
        result = subprocess.run(
            ["git", "-C", str(toplevel), "ls-tree", "-r", "--name-only", "HEAD",
             "--", rel_to_top.as_posix()],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def classify_task(goal: str, repo_root: Path | str | None = None) -> TaskKind:
    """Decide what kind of work *goal* asks for, from the filesystem.

    ``repo_root`` is the tree the run will write to.  When it is ``None`` the
    filesystem step is skipped and only the explicit page phrases can make a
    goal an artifact goal — which is the conservative direction: without a tree
    to check, the harness does not invent an artifact.

    "Exists" means *committed*: see :func:`_tracked_in_head`.  A file the run
    itself wrote in an earlier cycle is not evidence that the goal is a code
    change.
    """
    root = Path(repo_root) if repo_root is not None else None
    named = _named_paths(goal or "")

    artifact_paths: list[str] = []
    edit_paths: list[str] = []
    code_paths: list[str] = []
    unresolved: list[str] = []

    for rel in named:
        suffix = Path(rel).suffix.lower()
        tracked = _tracked_in_head(root, rel) if root is not None else None
        if tracked is None:
            exists = bool(root is not None and (root / rel).is_file())
        else:
            exists = tracked
        if exists and suffix in _ARTIFACT_SUFFIXES:
            # An artifact that is already in the repository: the run is asked
            # to change it.  This is ``artifact_edit``, not ``code`` — see the
            # module docstring for the measured consequence of conflating them.
            edit_paths.append(rel)
        elif exists:
            code_paths.append(rel)
        elif suffix in _ARTIFACT_SUFFIXES:
            # An artifact path that is not in the repository: the run is asked
            # to create it.
            artifact_paths.append(rel)
        elif root is not None:
            # A code path that is not there is still code work (a new module).
            code_paths.append(rel)
        else:
            unresolved.append(rel)

    if artifact_paths and (code_paths or edit_paths):
        kind: Literal["code", "artifact", "artifact_edit", "mixed", "answer"] = "mixed"
        evidence = (
            "goal names artifact path(s) absent from the repo "
            f"({', '.join(artifact_paths)}) and existing path(s) "
            f"({', '.join([*edit_paths, *code_paths])})"
        )
    elif edit_paths and code_paths:
        kind = "mixed"
        evidence = (
            "goal names an existing page to edit "
            f"({', '.join(edit_paths)}) and existing code ({', '.join(code_paths)})"
        )
    elif artifact_paths:
        kind = "artifact"
        evidence = (
            "goal names artifact path(s) that do not exist in the repo: "
            + ", ".join(artifact_paths)
        )
    elif edit_paths:
        kind = "artifact_edit"
        evidence = (
            "goal names artifact path(s) that already exist in the repo, so the "
            "page is being edited: " + ", ".join(edit_paths)
        )
    elif code_paths:
        kind = "code"
        evidence = "goal names path(s) that exist in the repo: " + ", ".join(code_paths)
    else:
        low = (goal or "").lower()
        phrase = next((p for p in PAGE_PHRASES if p in low), "")
        if phrase:
            kind = "artifact"
            evidence = f"goal names no path but says {phrase!r}"
        else:
            # No path, no page phrase.  Two possibilities remain, and they are
            # not the same product: a change to the tree, or a reply.
            #
            # The harness had only the first.  A goal whose correct output is a
            # sentence was routed to the coder, the planner demanded an
            # executable step with a target file, and the run died at ``plan``
            # with ``steps: 0`` — measured live on "What is 2+2? Answer in chat,
            # do not change any files."
            #
            # The decision is made on two deterministic signals, in this order:
            #
            # 1. **A mutating verb** ("add", "fix", "refactor", "исправь"…)
            #    means the tree changes.  It wins over any question phrasing,
            #    because "explain and fix the bug" is code work.
            # 2. **A question/answer phrase** ("what is", "explain", "search
            #    the web", "объясни"…) means a reply.
            #
            # Neither present → ``code``, the safe default: a wrong ``code``
            # verdict edits a file the user named, while a wrong ``answer``
            # verdict refuses to do the work at all.
            mutating = next((v for v in _MUTATING_VERBS if v in low), "")
            asking = next((p for p in _ANSWER_PHRASES if p in low), "")
            # "do not change any files" contains the verb "change" and means the
            # opposite of it.  A negation is stripped before the verb scan, or
            # the one phrase that most clearly asks for a reply is read as a
            # request to edit.  Measured: "What is 2+2? Answer in chat, do not
            # change any files." classified as ``code``.
            negated = low
            for phrase in _NO_CHANGE_PHRASES:
                negated = negated.replace(phrase, " ")
            mutating = next((v for v in _MUTATING_VERBS if v in negated), "")
            if asking and not mutating:
                kind = "answer"
                evidence = (
                    f"goal names no path and no mutating verb, but asks {asking!r}: "
                    "the run's output is a reply, not a diff"
                )
            elif mutating:
                kind = "code"
                evidence = (
                    f"goal names no resolvable path but asks to {mutating!r}: "
                    "the tree changes"
                )
            else:
                kind = "code"
                evidence = (
                    "goal names no resolvable path, no page phrase and no answer "
                    "phrase; defaulting to code (a wrong artifact verdict writes a "
                    "stray page)"
                )

    return TaskKind(
        kind=kind,
        artifact_paths=tuple(artifact_paths),
        edit_paths=tuple(edit_paths),
        code_paths=tuple(code_paths),
        evidence=evidence,
        unresolved=tuple(unresolved),
    )


def is_artifact_goal(goal: str, repo_root: Path | str | None = None) -> bool:
    """True when the goal asks for a rendered artifact rather than a code change."""
    return classify_task(goal, repo_root).is_artifact

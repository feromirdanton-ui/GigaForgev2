"""Reusable agent skills — procedural knowledge, selected by task, not by luck.

**Why this module exists.**  The harness had prompts: a persona, a handful of
rules, a tool contract.  What it did not have was *procedural knowledge that
applies to one class of task and not another*.  The consequence was measured
repeatedly across live runs:

* a page section child returned a whole page instead of one section, because
  nothing told it that its output is a fragment;
* a footer child answered in Slim/HAML (``section.class=footer``), because
  nothing told it the output is HTML;
* a document goal died on ``GF-CORE-PIPE-070`` because nothing told the writer
  that a document is a file with a path;
* a search-backed answer was rejected for missing citations, because nothing
  told the writer that a search answer must name its sources.

Each of those is a *procedure*, and each belongs to a task class.  Putting all
of them in one system prompt would cost every run the tokens of every class —
which is exactly the failure OpenAI measured when a single large ``AGENTS.md``
underperformed a short index plus structured detail.

**Progressive disclosure, the way the SOTA harnesses do it.**  Two tiers:

1. **The index** — one line per skill (name + when to use it), always present,
   ~15 tokens each.  It tells the model what knowledge exists.
2. **The body** — the full procedure, included only for the skills selected
   for *this* run.  A code-fix run never pays for the page-section procedure.

**Selection is deterministic.**  A skill declares the task kinds and phases it
applies to; the registry matches them against the classifier's verdict.  No
model call decides which skill to load — that would be a model call to decide
what to tell the model, and the classifier already knows the answer.

**A budget, not a wish.**  ``select`` takes a token budget and stops adding
bodies when it is reached, keeping the highest-priority skills.  A skill set
that silently doubles the prompt is a cost incident, not a feature.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

__all__ = [
    "Skill",
    "SkillRegistry",
    "BUILTIN_SKILLS",
    "default_registry",
    "select_skills",
    "render_skills",
]


@dataclass(frozen=True)
class Skill:
    """One reusable procedure.

    ``description`` is what the index shows — it must say *when* to use the
    skill, not what it contains, because the model reads it to decide whether
    the body is worth loading.  ``body`` is the procedure itself.
    """

    name: str
    description: str
    body: str
    #: Task kinds this skill applies to (``code``, ``artifact``,
    #: ``artifact_edit``, ``answer``, ``document``, ``mixed``).  Empty means
    #: "any kind".
    kinds: tuple[str, ...] = ()
    #: Phases this skill applies to.  Empty means "any phase".
    phases: tuple[str, ...] = ()
    #: Path suffixes the goal must name for this skill to apply.  Empty means
    #: "no constraint".  This is what keeps the document procedure out of a
    #: code-fix run: a ``.md`` goal is classified as ``code`` (a Markdown file
    #: is not a rendered artifact), so kind alone cannot tell the two apart —
    #: but the path in the goal can, deterministically.
    goal_suffixes: tuple[str, ...] = ()
    #: True when the skill is a *phase* procedure and must not be selected
    #: without a phase.  ``repair`` is the case: it is about fixing a rejected
    #: diff, so it is noise in a run that has not produced one yet.
    phase_only: bool = False
    #: Higher wins when the budget cannot fit everything.
    priority: int = 0

    @property
    def token_cost(self) -> int:
        """Rough cost of the body, at the harness's chars/4 convention."""
        return max(1, len(self.body) // 4)

    def index_line(self) -> str:
        return f"- **{self.name}** — {self.description}"

    def applies(self, *, kind: str, phase: str, goal: str = "") -> bool:
        """Whether this skill is relevant to a task of *kind* in *phase*.

        An empty ``kind`` or ``phase`` means "the caller has no constraint" and
        matches everything.  The first version compared them literally, so a
        caller that asked for skills by kind alone — which is what the prefix
        builder does, because the task class is known before any phase runs —
        got an empty list and the whole feature was silently dead.  A filter
        that treats "unspecified" as "matches nothing" is a filter that turns
        every optional argument into a bug.

        ``goal_suffixes`` is checked against the paths the goal names, so a
        skill can be precise about *what* is being written without a model call
        to decide it.
        """
        if kind and self.kinds and kind not in self.kinds:
            return False
        if phase and self.phases and phase not in self.phases:
            return False
        if self.phase_only and not phase:
            return False
        if self.goal_suffixes:
            if not goal:
                return False
            low = goal.lower()
            if not any(suffix in low for suffix in self.goal_suffixes):
                return False
        return True


# ── the built-in skills ─────────────────────────────────────────────────────
# Each body is short on purpose.  A procedure that needs a page is a document,
# not a skill; these are the rules that change behaviour in one paragraph.

_CODE_FIX = Skill(
    name="code-fix",
    description="changing existing code: smallest correct diff, no drive-by refactors",
    kinds=("code", "mixed"),
    phases=("code", "fix", "react"),
    priority=90,
    body="""\
**Changing existing code.**
- Emit ONE unified diff inside one ```diff fence. No prose before or after.
- Touch only the files the goal names. A file you were not asked about is a
  file you must not edit, even if it looks wrong.
- The smallest correct change wins. Do not reformat, rename or "tidy" code the
  task did not mention: every extra hunk is a hunk a reviewer must justify.
- Keep public symbols. If a function is imported elsewhere, its name and
  signature stay; change the body, not the contract.
- If the fix needs a new import, add it in the same hunk as the use.
- Never write a guessed value where a computed one belongs.""",
)

_CODE_ANALYSE = Skill(
    name="code-analyse",
    description="reading code to answer a question: cite paths and symbols, change nothing",
    kinds=("answer", "code"),
    phases=("react", "plan", "summarize"),
    # "What is 2+2" is an answer goal too, and the code-reading procedure is
    # noise for it.  A goal that names a source file is the deterministic
    # signal that the answer has to come from reading code.
    goal_suffixes=(
        ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
        ".kt", ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".sql",
    ),
    priority=80,
    body="""\
**Analysing code without changing it.**
- Read before you conclude. A claim about a function you have not read is a
  guess, and a guess in an analysis is worse than "I did not check".
- Cite the path and the symbol for every claim: `app.py:add` not "the add
  function somewhere".
- Separate what the code does from what it is supposed to do. If they differ,
  that difference IS the finding.
- Do not edit anything. An analysis goal produces a reply, not a diff.""",
)

_PAGE_CREATE = Skill(
    name="page-create",
    description="building a single-file page: one section per child, real CSS, no placeholders",
    kinds=("artifact", "mixed"),
    phases=("code", "react", "plan"),
    priority=95,
    body="""\
**Building a page.**
- One file, self-contained: `<style>` in the head, no external CSS or JS.
- Every section is a real `<section>` with an `id` that matches its name, so
  in-page links resolve. A link to `#pricing` needs an element with
  `id="pricing"`.
- A grid is a grid: `display: grid` with `grid-template-columns`, and its
  children are the cards. Do not nest a second wrapper between the grid and
  its items.
- No placeholder text. "Lorem ipsum", "Feature 1", "TODO" and "Coming soon"
  are failures, not filler.
- Declare the palette once in `:root` and use `var(--…)`. A colour named in
  the goal must appear in the CSS, not only in the prose.
- The page must be readable at 1440px and at 390px.""",
)

_PAGE_SECTION = Skill(
    name="page-section",
    description="writing ONE section of a page: a fragment, not a document",
    kinds=("artifact", "artifact_edit", "mixed"),
    phases=("code", "react"),
    priority=100,
    body="""\
**Writing one section.**
- Return ONLY the markup for the section you were asked for. No `<!DOCTYPE>`,
  no `<html>`, no `<head>`, no `<style>` block, no other section.
- Start with `<section id="<the name you were given>">` (or `<footer>` /
  `<nav>` for those landmarks).
- HTML, not a template language. `section.class=footer` and `= "Title"` are
  Slim/HAML and will be inserted as literal text.
- Do not invent sibling sections. If the page needs a pricing section, that is
  a different child's job.
- Close every tag you open. An unbalanced fragment corrupts the whole page.""",
)

_DOCUMENT = Skill(
    name="document",
    description="writing a document: a real file at a real path, structured, no filler",
    kinds=("document", "artifact", "code"),
    phases=("code", "react", "plan"),
    # A Markdown/plain-text goal is classified as ``code`` (it is not a
    # rendered artifact), so kind alone cannot separate "write a design doc"
    # from "fix the parser".  The path in the goal can, deterministically.
    goal_suffixes=(".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc"),
    priority=85,
    body="""\
**Writing a document.**
- A document is a file. Name the path in the diff header; a document with no
  path cannot be delivered.
- Structure it: a title, then sections with headings. A wall of text is not a
  document.
- Every claim that could be checked carries its source. A number with no
  origin is an opinion in the shape of a fact.
- No filler: no "In today's fast-paced world", no restating the title as the
  first sentence.
- Markdown for `.md`; do not emit HTML inside a Markdown file.""",
)

_WEB_RESEARCH = Skill(
    name="web-research",
    description="answering from the web: search first, cite what the tool returned",
    kinds=("answer", "mixed"),
    phases=("react", "plan", "summarize"),
    priority=88,
    body="""\
**Answering from the web.**
- Search before you answer. An answer about current events from memory is a
  guess with a date on it.
- Cite only URLs the search tool actually returned. A plausible-looking URL
  you constructed is a fabrication, and it is the one error a reader cannot
  detect.
- Prefer primary sources: the project's own docs, the paper, the changelog.
- Say when the sources disagree, and say which one you trusted and why.
- If the search returned nothing useful, say so. "I could not find this" is a
  correct answer; an invented one is not.""",
)

_CHAT_ANSWER = Skill(
    name="chat-answer",
    description="a direct reply: answer the question, change no files",
    kinds=("answer",),
    phases=("react", "summarize", "plan"),
    priority=70,
    body="""\
**Answering in chat.**
- Answer the question that was asked, in the first sentence. Context and
  caveats come after, and only if they change the answer.
- Change no files. This goal's output is a reply.
- If the question is arithmetic or otherwise computable, compute it — do not
  describe how one would compute it.
- Length follows the question: a yes/no question gets a sentence, not a page.""",
)

_REPAIR = Skill(
    name="repair",
    description="fixing a rejected change: read the failure, change the approach",
    # Repair is a *phase*, not a task class: it applies to whatever is being
    # repaired.  Declaring it for every kind made it leak into answer and
    # document runs, where there is no diff to repair.
    kinds=("code", "artifact", "artifact_edit", "mixed"),
    phases=("fix",),
    phase_only=True,
    priority=92,
    body="""\
**Repairing a rejected change.**
- Read the failure before you write. The rejection names what was wrong; a
  repair that ignores it reproduces it.
- Change the approach, not the wording. If the same diff failed twice, the
  third identical attempt will fail identically.
- Fix the cause, not the symptom: if a test fails because a function returns
  the wrong type, change the return, not the test.
- Keep what was already accepted. A repair that breaks a passing part of the
  change is a regression with a new name.""",
)

BUILTIN_SKILLS: tuple[Skill, ...] = (
    _PAGE_SECTION,
    _PAGE_CREATE,
    _REPAIR,
    _CODE_FIX,
    _WEB_RESEARCH,
    _DOCUMENT,
    _CODE_ANALYSE,
    _CHAT_ANSWER,
)


class SkillRegistry:
    """A set of skills, selectable by task kind and phase."""

    def __init__(self, skills: Iterable[Skill] = BUILTIN_SKILLS) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self.add(skill)

    def add(self, skill: Skill) -> None:
        if not skill.name:
            raise ValueError("a skill needs a name")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def __len__(self) -> int:
        return len(self._skills)

    def select(
        self,
        *,
        kind: str = "",
        phase: str = "",
        goal: str = "",
        budget_tokens: int = 1200,
        names: Sequence[str] | None = None,
    ) -> list[Skill]:
        """The skills that apply, highest priority first, within a budget.

        ``names`` pins an explicit set (an operator or a test), and still
        respects the budget.  A skill whose body does not fit is skipped rather
        than truncated: half a procedure is worse than none, because the model
        follows the half it can see.
        """
        if names is not None:
            candidates = [self._skills[n] for n in names if n in self._skills]
        else:
            candidates = [
                s
                for s in self._skills.values()
                if s.applies(kind=kind, phase=phase, goal=goal)
            ]
        candidates.sort(key=lambda s: (-s.priority, s.name))

        chosen: list[Skill] = []
        spent = 0
        for skill in candidates:
            cost = skill.token_cost
            if chosen and spent + cost > budget_tokens:
                continue
            # The first skill is always taken, even when it alone exceeds the
            # budget.  A run with no procedure at all is worse than a run a few
            # hundred tokens over: the budget exists to stop the set growing,
            # not to leave the model with nothing.  This is a decision, not an
            # accident of the loop — the earlier version had the same behaviour
            # by accident and no test said which it was.
            chosen.append(skill)
            spent += cost
        return chosen

    def index(self) -> str:
        """The always-present index: one line per skill, cheapest tier."""
        lines = [s.index_line() for s in sorted(self._skills.values(), key=lambda s: s.name)]
        return "\n".join(lines)


_DEFAULT: SkillRegistry | None = None


def default_registry() -> SkillRegistry:
    """The process-wide registry of built-in skills."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SkillRegistry()
    return _DEFAULT


def select_skills(
    *,
    kind: str = "",
    phase: str = "",
    goal: str = "",
    budget_tokens: int = 1200,
    registry: SkillRegistry | None = None,
    names: Sequence[str] | None = None,
) -> list[Skill]:
    """Convenience wrapper over :meth:`SkillRegistry.select`."""
    return (registry or default_registry()).select(
        kind=kind, phase=phase, goal=goal, budget_tokens=budget_tokens, names=names
    )


def render_skills(
    skills: Sequence[Skill],
    *,
    include_index: bool = True,
    registry: SkillRegistry | None = None,
) -> str:
    """Render the selected skills as a prompt block.

    The index is included by default so the model knows what else exists even
    when the budget did not fit a body — that is the whole point of two tiers.
    """
    parts: list[str] = []
    if include_index:
        index = (registry or default_registry()).index()
        if index:
            parts.append(
                "## Available skills\n"
                "Procedures that apply to this kind of task. The ones relevant to "
                "this run are expanded below.\n" + index
            )
    for skill in skills:
        parts.append(f"## Skill: {skill.name}\n{skill.body.strip()}")
    return "\n\n".join(parts)

"""The GigaChat harness prompt — ported from ``ai-forever/deepagents-gigachat``.

**Why a separate prompt module.**  The upstream project found, by benchmark,
that GigaChat's function-calling behaves differently enough from the models the
generic agent prompts were written for that the *prompt itself* is a
reliability component.  Three failure classes dominated and each has a prompt
countermeasure:

1. **Looping on a broken pattern.**  The model commits to a malformed call on
   turn 1 and repeats it until the step budget is gone.  Countermeasure: an
   explicit anti-looping section with a call budget and a "change the approach
   after two identical failures" rule.
2. **Doing half of a two-step operation.**  "Rename A to B" produces B and
   leaves A.  Countermeasure: a named "two-step operations" section.
3. **Writing a guessed value instead of computing it.**  Countermeasure: a
   "derived data must be computed" rule plus the deterministic-output guard.

**What is different here.**  Upstream's prompt describes a *filesystem* toolset
(``ls``/``read_file``/``write_file``/``glob``/``grep``/``edit_file``/
``execute``).  GigaForge's ReAct loop has a different, smaller vocabulary —
``grep``, ``read_structure``, ``run_tests``, ``finish`` — and its edits are
emitted as a plan the DAG executes, not as direct file writes.  Copying the
upstream text verbatim would describe tools that do not exist, which is exactly
the failure mode the tool-contract guard exists to prevent.  So the *principles*
are ported and the *tool descriptions* are rewritten against the real registry.

The prompt is assembled from named fragments so a caller can compose a variant
(``native_fs`` / ``tool_agnostic`` / ``external_runtime``) the way upstream does,
and so a test can assert a fragment is present without matching a 6 KB string.
"""
from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "CORE_PROMPT",
    "ANTI_LOOP_PROMPT",
    "TWO_STEP_PROMPT",
    "OUTPUT_PROMPT",
    "PLAN_PROMPT",
    "BUDGET_PROMPT",
    "TOOL_DESCRIPTIONS",
    "PROFILE_VARIANTS",
    "build_harness_prompt",
    "tool_description_overrides",
    "describe_tools",
]

CORE_PROMPT = """\
You are an agent working inside a real repository. Solve the task by calling the
tools that are actually available in this run. Be brief.

## How to work
- Read the request literally. Do exactly what is asked — no extras, no
  commentary, no clarifying questions when the task is concrete.
- The repository is the source of truth. Never invent file contents, symbol
  names, or values; look them up.
- If a value is derived from the code (a count, a list, a hash, an ordering),
  compute it from what you actually read. Do not estimate it.
- Process EVERY item the task mentions. "all callers", "every test", "each
  module" means all of them, not the first one you find.
- Prefer direct completion over exploration. For a straightforward task, act
  immediately.
- After your last tool call, return a short answer. Do not narrate intermediate
  steps.
"""

ANTI_LOOP_PROMPT = """\
## Anti-looping — CRITICAL
- A simple task should take 3–6 tool calls. If you have made 10+ calls and the
  task is still simple, STOP and produce your best answer.
- If a tool returns the same result or the same error twice in a row, do NOT
  call it a third time. Change the approach or accept the current result.
- If a search returns nothing, the pattern or the scope is wrong — widen it or
  switch tool. Repeating the same search cannot succeed.
- NEVER re-read something you just produced to "double check" unless the task
  asks for verification. One read + one action = done.
"""

TWO_STEP_PROMPT = """\
## Two-step operations (do both parts!)
The task often has two halves; missing the second one is the most common
failure.
- "rename A to B" / "move X to Y" → create the new location, THEN remove the
  old one.
- "replace X with Y" / "remove old_func" → after the change there must be ZERO
  occurrences of the old text.
- "convert module.py into a package" → create the package, then delete the
  single-file module.

Before finishing, check: "did I do both parts?"
"""

OUTPUT_PROMPT = """\
## Required outputs (strict)
- If the task names output file(s), produce those exact names — same spelling,
  same extension.
- Do not substitute a helper script for the requested artifact.
- Do not leave a requested output empty unless the task asks for an empty file.
- No placeholders ("TODO", "lorem ipsum", mock data) when real content is
  required.
- Paths you report are relative to the repository root: `src/foo.py`, never
  `/src/foo.py` and never an absolute host path.
- Before finishing, verify each required output exists and is non-empty.
- For generated code, run the available tests. A file existing is not
  verification.
"""

PLAN_PROMPT = """\
## The plan you emit
Your final action is `finish`, and its payload is the plan the executor runs.
- One step per concrete change. Each step names the file or symbol it touches.
- An edit step with no target is unactionable and will be dropped.
- A plan that edits and never verifies is a plan that hopes: include a
  verification step (`run_tests`) when you change code.
- Do not restate the goal as a step.
"""

BUDGET_PROMPT = """\
## Budget
- You have a bounded number of steps. Plan around it: simple tasks need 3–6,
  complex ones 15–30.
- If you are past 15 steps on a task that should be simple, something is wrong.
  Stop retrying, emit the best plan you have, and finish.
- An empty plan is a failed episode. On the last step, finish with whatever you
  have learned.
"""

#: Descriptions of the tools GigaForge's ReAct registry actually provides.
#: Rewritten from upstream's filesystem vocabulary to this project's, because a
#: prompt that describes tools the run does not have is worse than no prompt.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "grep": (
        "Search the repository for a pattern. `action_input` is the pattern "
        "(a regular expression). Scope it: a bare pattern searches everything "
        "and is slow. If it returns nothing, the pattern or scope is wrong — "
        "do not repeat the same search."
    ),
    "read_structure": (
        "Return the skeleton of one file: its imports, classes, functions and "
        "signatures, without bodies. `action_input` is a path RELATIVE to the "
        "repository root (`src/foo.py`), never an absolute path. Use this "
        "before editing so you know the real symbol names."
    ),
    "run_tests": (
        "Run the test suite. `action_input` is the pytest argument string, or "
        "empty for the whole suite. Use it to verify a change, not to explore."
    ),
    "think": (
        "Scratchpad for one reasoning step. Call it before a multi-rule change "
        "to enumerate the exact output fields and types, the ordering and "
        "tie-break rules, the missing-value conventions, and every edge-case "
        "branch in the request. It changes nothing in the repository."
    ),
    "finish": (
        "End the episode and emit the plan. `action_input` is a JSON array of "
        "steps: `[{\"tool\": \"edit_function\", \"args\": {\"name\": \"<symbol>\"}}]`. "
        "An empty plan is a failed episode."
    ),
}

#: Profile variants, mirroring upstream's three shapes.
PROFILE_VARIANTS = ("native_fs", "tool_agnostic", "external_runtime")


def describe_tools(names: list[str] | tuple[str, ...] | None = None) -> str:
    """Render the tool contract for the tools that are actually registered.

    Only tools present in *names* are described.  A prompt that advertises a
    tool the run does not have is the exact failure the tool-contract guard
    exists to prevent, so this function is the single source of that text.
    """
    selected = list(names) if names else list(TOOL_DESCRIPTIONS)
    lines = ["## Tools available in this run"]
    for name in selected:
        description = TOOL_DESCRIPTIONS.get(name)
        if description:
            lines.append(f"- `{name}` — {description}")
    return "\n".join(lines)


def build_harness_prompt(
    variant: str = "native_fs",
    *,
    tool_names: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Assemble the harness prompt for *variant*.

    ``native_fs`` is the full prompt (the default).  ``tool_agnostic`` drops the
    tool descriptions, for a runtime that supplies its own contract.
    ``external_runtime`` keeps the principles but points at the caller's
    contract instead of this project's registry.
    """
    normalized = (variant or "native_fs").strip().lower().replace("-", "_")
    if normalized not in PROFILE_VARIANTS:
        raise ValueError(
            f"unknown harness profile variant {variant!r}; "
            f"expected one of {', '.join(PROFILE_VARIANTS)}"
        )

    parts = [CORE_PROMPT, ANTI_LOOP_PROMPT, TWO_STEP_PROMPT, OUTPUT_PROMPT, PLAN_PROMPT]
    if normalized == "native_fs":
        parts.append(describe_tools(tool_names))
    elif normalized == "external_runtime":
        parts.append(
            "## Tools\n"
            "This run exposes its tools through an external runtime. Use the "
            "tool contract supplied by the caller as the source of truth. Do "
            "not invent tool names or shell subcommands."
        )
    parts.append(BUDGET_PROMPT)
    return "\n\n".join(parts)


def tool_description_overrides(
    variant: str = "native_fs",
) -> Mapping[str, str]:
    """Per-tool description overrides, keyed by tool name.

    Upstream rewrites the descriptions of the tools it ships.  Here the same
    text is available for a caller that registers these tools with a provider
    that accepts ``functions=[...]`` — the description the model sees then
    matches the description the prompt gives it.
    """
    if (variant or "").strip().lower().replace("-", "_") == "tool_agnostic":
        return {}
    return dict(TOOL_DESCRIPTIONS)

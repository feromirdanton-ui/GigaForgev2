"""Phase prompt templates (M4–M5).

The point of this module is a clean split:

* the **stable prefix** (persona, rules, tool contract, repo map) is built once
  by :mod:`gigaforge.core.state` and never changes during a run;
* the **volatile instruction** for the current phase is appended *after* it.

Keeping the two apart is what makes prefix caching possible: if the phase
instruction were folded into the system prompt, every phase would invalidate the
cached prefix.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

PLAN_INSTRUCTION = """Decompose the goal into an ordered, minimal plan.

Return a numbered list of concrete steps. Each step must name the file or symbol
it touches. Do not include steps that merely restate the goal."""

CODE_INSTRUCTION = """Produce the change that satisfies the goal.

Return exactly one ```diff fenced unified diff. The diff must apply cleanly to
the current worktree. Change only what the goal requires.

Diff rules — violating them makes the whole change unapplicable:
* ONE file per block. Open each file with its own `diff --git a/<path> b/<path>`,
  then `--- a/<path>` and `+++ b/<path>`.
* To EDIT an existing file, `---` and `+++` must name the SAME path, and every
  context line must be copied verbatim from that file.
* To CREATE a file, use `--- /dev/null` and `+++ b/<path>` — never put a creation
  hunk inside an existing file's block.
* Never rename through the header (`index1.html` -> `index1_new.html`): edit the
  existing file under its own name.
* No prose inside the diff, no placeholders, no `...` elisions.
* Paths are relative to the repository root — never prefix them with the folder
  name of the repository itself.

For NEW files prefer whole-file blocks over diffs:

### FILE: path/to/new_file.html
```
<the complete file content>
```

One block per file, the complete content, no placeholders. Editing an existing file
still uses a unified diff.

For HTML/CSS pages, quality rules (checked, not advisory):
* self-contained: no external http(s) assets, no placeholder-image hosts; draw
  with CSS (gradients, borders, emoji) instead;
* real content: every section carries meaningful copy, not lorem ipsum;
* typography: large hero heading, comfortable line-height, one accent colour;
* navigation links between the pages actually exist and match the file names."""

FIX_INSTRUCTION = """Your previous diff was rejected. Repair it.

Read the failure reason and the repair hint, keep every part of the change that
was accepted, and return one corrected ```diff fenced unified diff. Do not
repeat the mistake that caused the rejection."""

REVIEW_INSTRUCTION = """Review the proposed change for correctness and scope.

Answer with: verdict (accept / reject), the single most important risk, and any
missing test. Be terse."""

SUMMARIZE_INSTRUCTION = """Summarise the outcome for the user.

State what changed, why, and what remains. No preamble, no repetition of the
diff itself."""

RETRIEVE_INSTRUCTION = """Select the smallest set of symbols needed to act.

Return the symbol names you need, most relevant first."""

PHASE_INSTRUCTIONS: Mapping[str, str] = {
    "plan": PLAN_INSTRUCTION,
    "code": CODE_INSTRUCTION,
    "fix": FIX_INSTRUCTION,
    "review": REVIEW_INSTRUCTION,
    "summarize": SUMMARIZE_INSTRUCTION,
    "retrieve": RETRIEVE_INSTRUCTION,
}

DIFF_FENCE = "```diff"


@dataclass
class PhasePrompt:
    """A rendered phase instruction plus the context blocks it carries."""

    phase: str
    instruction: str
    goal: str = ""
    slice_text: str = ""
    failure: str = ""
    hint: str = ""
    scratchpad: str = ""
    extra_blocks: list[tuple[str, str]] = field(default_factory=list)

    def render(self) -> str:
        """Render the volatile tail, most decisive information first.

        The ordering is deliberate: the failure reason and the goal come before
        the (long) context slice, because a model that truncates will drop the
        tail — and the tail is the replaceable part.
        """
        parts: list[str] = [f"# Phase\n{self.phase}", f"# Instruction\n{self.instruction.strip()}"]
        if self.goal:
            parts.append(f"# Goal\n{self.goal.strip()}")
        if self.failure:
            parts.append(f"# Why the previous attempt failed\n{self.failure.strip()}")
        if self.hint:
            parts.append(f"# Repair hint\n{self.hint.strip()}")
        if self.scratchpad:
            parts.append(f"# Scratchpad\n{self.scratchpad.strip()}")
        for title, body in self.extra_blocks:
            if body and body.strip():
                parts.append(f"# {title}\n{body.strip()}")
        if self.slice_text:
            parts.append(f"# Context slice\n{self.slice_text.strip()}")
        return "\n\n".join(parts)

    def as_message(self) -> dict[str, str]:
        return {"role": "user", "content": self.render()}


def instruction_for(phase: str) -> str:
    return PHASE_INSTRUCTIONS.get(phase, f"Perform the '{phase}' phase of the task.")


def build_phase_prompt(
    phase: str,
    *,
    goal: str = "",
    slice_text: str = "",
    failure: str = "",
    hint: str = "",
    scratchpad: str = "",
    extra_blocks: Optional[Sequence[tuple[str, str]]] = None,
    instruction: Optional[str] = None,
) -> PhasePrompt:
    return PhasePrompt(
        phase=phase,
        instruction=instruction or instruction_for(phase),
        goal=goal,
        slice_text=slice_text,
        failure=failure,
        hint=hint,
        scratchpad=scratchpad,
        extra_blocks=list(extra_blocks or []),
    )


def extract_diff(text: str) -> str:
    """Pull the first ```diff fenced block out of a model answer.

    Falls back to the whole text when no fence is present: the firewall's
    ``strip_code_fences`` will then do its own (more tolerant) extraction, and
    duplicating that logic here would just be a second place to get it wrong.
    """
    if not text:
        return ""
    marker = DIFF_FENCE
    start = text.find(marker)
    if start == -1:
        # Tolerate a bare ``` fence whose body looks like a diff.
        for opener in ("```", "~~~diff", "~~~"):
            start = text.find(opener)
            if start != -1:
                break
        if start == -1:
            return text.strip()
        body_start = text.find("\n", start)
        if body_start == -1:
            return text.strip()
        end = text.find("```", body_start)
        if end == -1:
            end = text.find("~~~", body_start)
        body = text[body_start + 1: end if end != -1 else len(text)]
        return body.strip()
    body_start = text.find("\n", start)
    if body_start == -1:
        return text.strip()
    end = text.find("```", body_start + 1)
    body = text[body_start + 1: end if end != -1 else len(text)]
    return body.strip()


__all__ = [
    "PhasePrompt",
    "PHASE_INSTRUCTIONS",
    "PLAN_INSTRUCTION",
    "CODE_INSTRUCTION",
    "FIX_INSTRUCTION",
    "REVIEW_INSTRUCTION",
    "SUMMARIZE_INSTRUCTION",
    "RETRIEVE_INSTRUCTION",
    "DIFF_FENCE",
    "instruction_for",
    "build_phase_prompt",
    "extract_diff",
]

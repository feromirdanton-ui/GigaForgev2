"""TaskState — the session is an OBJECT, NOT a message list.

Spec doctrine: "session-as-object, NOT message history". The TaskState carries
the goal, the current plan, a compact scratchpad, metrics, git context, and a
slice hash. It is NOT chat history — agents consult it to compose each LLM
prompt FRESH from the structured state, never replay messages.

The `serialize_prefix()` is the MINIMAL stable-ordered prefix that gets prepended
to every LLM call. Field order is FIXED for future KV-caching:
    {"goal", "plan": [{"tool","args","done"}], "slice_hash"}

0 LLM calls in this module — pure state container.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = ["PlanStep", "TaskState"]


class PlanStep(BaseModel):
    """One step in the task plan.

    `tool` is the agent/tool to invoke (e.g. "coder", "reviewer", "lint",
    "tests"). `args` is the structured arguments (NOT a free-form string).
    `done` flips to True after the step's result is recorded. `result` is
    intentionally excluded from `serialize_prefix` because it CHANGES —
    including it would invalidate KV cache hits.
    """

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    done: bool = False
    result: dict[str, Any] | None = None

    def to_status_dict(self) -> dict[str, Any]:
        """Minimal stable-ordered dict for serialize_prefix."""
        # Insertion order: tool, args, done — fixed for KV-caching stability.
        return {"tool": self.tool, "args": self.args, "done": self.done}


class TaskState(BaseModel):
    """The full task session object. NOT a message list.

    Lifecycle: Planner populates `goal` + `plan`; agents mutate `scratchpad`
    and flip `done` via `mark_step_done(idx, result)`.
    """

    goal: str
    task_id: str
    git_sha: str = ""
    slice_hash: str = ""
    plan: list[PlanStep] = Field(default_factory=list)
    scratchpad: list[str] = Field(default_factory=list)  # compact facts
    metrics: dict[str, Any] = Field(default_factory=dict)

    # --- mutation -------------------------------------------------------

    def add_fact(self, fact: str) -> None:
        """Append a compact fact to the scratchpad. No dedup — caller's job."""
        if not isinstance(fact, str):
            raise TypeError(f"add_fact expects str, got {type(fact).__name__}")
        self.scratchpad.append(fact)

    def mark_step_done(self, idx: int, result: dict[str, Any]) -> None:
        """Flip step `idx` to done and record its result. Raises IndexError
        if out of range."""
        if idx < 0 or idx >= len(self.plan):
            raise IndexError(f"plan step idx out of range: {idx} (len={len(self.plan)})")
        step = self.plan[idx]
        step.done = True
        step.result = result

    # --- serialization (KV-cache-stable) --------------------------------

    def serialize_prefix(self) -> dict[str, Any]:
        """Minimal stable-ordered prefix prepended before each LLM call.

        Fixed key order: ["goal", "plan", "slice_hash"]
        Plan-step key order: ["tool", "args", "done"]

        Excludes scratchpad/metrics/result — those mutate per step and would
        bust any prefix-based KV cache. The slice itself is identified by
        `slice_hash` (the full slice object is passed separately as the
        structured context payload, NOT inside the prefix).

        Python dicts preserve insertion order (3.7+) — `list(dict.keys())` is
        therefore deterministic for two states with equal construction.
        """
        # Build with EXPLICIT insertion order — do NOT call model_dump()
        # (pydantic's dump may reorder alphabetically or sort_keys=True at
        # JSON encode time elsewhere; here we control order by construction).
        return {
            "goal": self.goal,
            "plan": [s.to_status_dict() for s in self.plan],
            "slice_hash": self.slice_hash,
        }

    def serialize_full(self) -> dict[str, Any]:
        """Full state for persistence/tracing. NOT used as the LLM prefix."""
        # model_dump with mode="json" — but exclude_none=False to keep shape stable.
        return {
            "goal": self.goal,
            "task_id": self.task_id,
            "git_sha": self.git_sha,
            "slice_hash": self.slice_hash,
            "plan": [s.model_dump(mode="json") for s in self.plan],
            "scratchpad": list(self.scratchpad),
            "metrics": dict(self.metrics),
        }

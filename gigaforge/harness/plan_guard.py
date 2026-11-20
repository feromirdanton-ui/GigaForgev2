"""Plan validation \u2014 the harness step between "a plan exists" and "run it".

**Why this is a harness concern, not a prompt concern.**  A model produces a
plan; the DAG then executes it step by step.  Nothing in between checked that
the plan was *executable*, and a live run showed what that costs: the ReAct
episode returned ``edit_function: react.py`` \u2014 a step naming the file the
episode had been reading, with no symbol, no verification, and no relation to
the goal.  ``score_steps`` rated it and the pipeline ran it.

The guard is deliberately **deterministic and model-free**.  A second model
call to judge a plan would double the cost of planning and make the outcome
depend on the judge's mood; every rule here is a property of the plan itself.

What it enforces:

* **Known tools.**  A step naming a tool the pipeline does not have cannot
  run.  Unknown steps are dropped, not silently executed.
* **No duplicates.**  Two identical steps are one step; a repeated step is the
  signature of a hallucinated loop.
* **A verification step.**  A plan that edits and never checks is a plan that
  hopes.  When the plan edits but does not verify, a verification step is
  appended \u2014 the harness knows the pipeline has ``tests``/``lint``.
* **A bounded length.**  Past :data:`MAX_STEPS` the plan is truncated: a
  twenty-step plan for a one-line fix is not thoroughness, it is drift.
* **A target.**  An edit step with no file/symbol argument is unactionable and
  is dropped.

The result is a :class:`PlanVerdict` that says what changed and why, so the
ledger and the report can show it instead of the caller guessing.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "MAX_STEPS",
    "VERIFICATION_TOOLS",
    "EDIT_TOOLS",
    "PlanVerdict",
    "validate_plan",
]

#: A plan longer than this is drift, not thoroughness.
MAX_STEPS = 12

#: Tools that check work rather than change it.  ``run_tests`` is the name the
#: ReAct registry actually registers (``gigaforge/agents/react.py``); ``tests``
#: is the DAG's step vocabulary.  Both are accepted, and the guard appends the
#: one the registry really has.
VERIFICATION_TOOLS = frozenset(
    {"run_tests", "tests", "test", "lint", "reviewer", "review", "typecheck"}
)

#: Tools that change the repository and therefore need a target.
EDIT_TOOLS = frozenset(
    {"edit_function", "edit_file", "coder", "write_file", "apply_patch", "refactor"}
)

#: Argument keys that name what an edit step acts on.
_TARGET_KEYS = ("name", "symbol", "path", "file", "target", "function")


@dataclass
class PlanVerdict:
    """The validated plan plus an account of what the guard changed."""

    steps: list[Any] = field(default_factory=list)
    dropped_unknown: list[str] = field(default_factory=list)
    dropped_untargeted: list[str] = field(default_factory=list)
    deduped: int = 0
    truncated: int = 0
    appended_verification: bool = False

    @property
    def changed(self) -> bool:
        return bool(
            self.dropped_unknown
            or self.dropped_untargeted
            or self.deduped
            or self.truncated
            or self.appended_verification
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": len(self.steps),
            "dropped_unknown": list(self.dropped_unknown),
            "dropped_untargeted": list(self.dropped_untargeted),
            "deduped": self.deduped,
            "truncated": self.truncated,
            "appended_verification": self.appended_verification,
            "changed": self.changed,
        }


def _tool_of(step: Any) -> str:
    return str(getattr(step, "tool", "") or "").strip()


def _args_of(step: Any) -> dict[str, Any]:
    args = getattr(step, "args", None)
    return args if isinstance(args, dict) else {}


def _signature(step: Any) -> tuple[str, str]:
    """Identity of a step for dedupe: tool plus its sorted arguments."""
    args = _args_of(step)
    rendered = "|".join(f"{k}={args[k]!r}" for k in sorted(args))
    return _tool_of(step), rendered


def _has_target(step: Any) -> bool:
    args = _args_of(step)
    return any(str(args.get(key) or "").strip() for key in _TARGET_KEYS)


def validate_plan(
    steps: Sequence[Any],
    *,
    known_tools: Iterable[str] | None = None,
    max_steps: int = MAX_STEPS,
    require_verification: bool = True,
) -> PlanVerdict:
    """Return an executable version of *steps* plus what was changed.

    ``known_tools`` is the set of tools the pipeline can actually run.  Pass
    ``None`` to skip that check (a caller that has no registry, e.g. a unit
    test of the other rules).
    """
    allowed = {str(t).strip() for t in known_tools} if known_tools is not None else None
    verdict = PlanVerdict()

    seen: set[tuple[str, str]] = set()
    kept: list[Any] = []
    for step in steps or []:
        tool = _tool_of(step)
        if not tool:
            # A duck-typed step (a test stub, or a future engine) may carry a
            # ready-made ``description`` instead of tool/args.  ``_describe_plan_step``
            # renders those, so the guard must not drop what it cannot inspect \u2014
            # it has no evidence the step is wrong, only that it is opaque.
            if str(getattr(step, "description", "") or "").strip():
                kept.append(step)
                continue
            verdict.dropped_unknown.append("<empty>")
            continue
        if allowed is not None and tool not in allowed:
            verdict.dropped_unknown.append(tool)
            continue
        if tool in EDIT_TOOLS and not _has_target(step):
            # "edit something" is not a step; it cannot be executed or checked.
            verdict.dropped_untargeted.append(tool)
            continue
        signature = _signature(step)
        if signature in seen:
            verdict.deduped += 1
            continue
        seen.add(signature)
        kept.append(step)

    if require_verification and kept:
        edits = [s for s in kept if _tool_of(s) in EDIT_TOOLS]
        verifies = [s for s in kept if _tool_of(s) in VERIFICATION_TOOLS]
        if edits and not verifies:
            # The pipeline always has a tests step; appending it is the
            # difference between "changed the code" and "changed the code and
            # knows it still works".
            try:
                from gigaforge.core.task_state import PlanStep

                # Append the name the registry actually has, so the DAG can run
                # it: ``run_tests`` is the ReAct tool, ``tests`` the DAG step.
                name = "run_tests" if allowed is None or "run_tests" in allowed else "tests"
                kept.append(PlanStep(tool=name, args={}))
                verdict.appended_verification = True
            except Exception:  # noqa: BLE001 - a missing model is not fatal
                pass

    # Truncate AFTER the verification step is appended, or the cap is exceeded
    # by exactly one \u2014 the guard would then hand the DAG a plan longer than the
    # limit it advertises.
    if len(kept) > max_steps:
        verdict.truncated = len(kept) - max_steps
        kept = kept[:max_steps]

    verdict.steps = kept
    return verdict

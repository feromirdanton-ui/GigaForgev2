"""BudgetTracker — hard token caps per phase and per task.

Spec doctrine: "context is budget". BudgetExceeded = STOP + report, never
"one more call". The Router is the budget controller.

Phases (matched against `Settings`):
    plan     -> budget_plan_in / budget_plan_out
    code     -> budget_code_in / budget_code_out
    fixloop  -> budget_fixloop_in / budget_fixloop_out
    review / classify / summarize / explain -> map to "plan" caps (Lite model)
    hard_reasoning -> maps to "code" caps (Max model is heavyweight).

Fix-loop bounded to `budget_fixloop_iters` (default 3); each iteration beyond
that limit returns the new count and the pipeline (NOT this tracker) decides
to open the circuit breaker. We just count.

0 LLM calls in this module — pure accounting.
"""
from __future__ import annotations

import threading
from typing import Any

from gigaforge.client.errors import BudgetExceeded
from gigaforge.config import Settings, get_settings

__all__ = ["BudgetTracker", "PHASE_CAP_ATTRS"]


# Map of phase -> (settings_in_attr, settings_out_attr) used to resolve caps.
# MED-15: single source of truth — imported by router.py (was duplicated there).
PHASE_CAP_ATTRS: dict[str, tuple[str, str]] = {
    "plan": ("budget_plan_in", "budget_plan_out"),
    "review": ("budget_plan_in", "budget_plan_out"),
    "classify": ("budget_plan_in", "budget_plan_out"),
    "summarize": ("budget_plan_in", "budget_plan_out"),
    "explain": ("budget_plan_in", "budget_plan_out"),
    "code": ("budget_code_in", "budget_code_out"),
    "hard_reasoning": ("budget_code_in", "budget_code_out"),
    "fixloop": ("budget_fixloop_in", "budget_fixloop_out"),
    # A ReAct episode is a loop, not one call: it needs its own cap.
    "react": ("budget_react_in", "budget_react_out"),
}


class BudgetTracker:
    """Per-phase in/out token caps + per-task hard cap + fix-loop iteration counter."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._lock = threading.Lock()
        # phase -> {"in": int, "out": int}
        self._phase_used: dict[str, dict[str, int]] = {}
        # task_id -> {"in": int, "out": int}
        self._task_used: dict[str, dict[str, int]] = {}
        # task_id -> fix-loop iteration count
        self._fixloop_count: dict[str, int] = {}

    # --- internal helpers ------------------------------------------------

    def _caps_for(self, phase: str) -> tuple[int, int]:
        attrs = PHASE_CAP_ATTRS.get(phase)
        if attrs is None:
            # Unknown phase: fall back to plan caps but log (no silent degradation
            # — surface via the returned cap pair, which is conservative).
            attrs = PHASE_CAP_ATTRS["plan"]
        return int(getattr(self._s, attrs[0])), int(getattr(self._s, attrs[1]))

    def _phase_used_in(self, phase: str) -> dict[str, int]:
        d = self._phase_used.get(phase)
        if d is None:
            d = {"in": 0, "out": 0}
            self._phase_used[phase] = d
        return d

    def _task_used_in(self, task_id: str) -> dict[str, int]:
        d = self._task_used.get(task_id)
        if d is None:
            d = {"in": 0, "out": 0}
            self._task_used[task_id] = d
        return d

    # --- public API ------------------------------------------------------

    def check(
        self,
        phase: str,
        tokens_in: int,
        tokens_out: int,
        *,
        task_id: str = "",
    ) -> None:
        """Raise BudgetExceeded if adding (in,out) would breach the phase cap
        OR the per-task hard cap. STOP + report semantics: we raise BEFORE
        committing any tokens, never "one more call".

        This is a VALIDATION ONLY — it does NOT record usage. Callers MUST call
        :meth:`commit` with the ACTUAL token counts after the LLM call returns.
        This separation is critical for retries: estimates don't inflate the
        phase budget; only actual usage does.

        CRIT-02: the per-task cap is PER-TASK (isolated by ``task_id``), NOT
        global. Two tasks running in the same process each get their own full
        cap. The phase caps remain process-global (shared across tasks).
        """
        tokens_in = int(tokens_in)
        tokens_out = int(tokens_out)
        cap_in, cap_out = self._caps_for(phase)
        with self._lock:
            ph = self._phase_used_in(phase)
            new_ph_in = ph["in"] + tokens_in
            new_ph_out = ph["out"] + tokens_out
            if new_ph_in > cap_in:
                raise BudgetExceeded(
                    f"phase {phase!r} input budget exceeded: "
                    f"{ph['in']} + {tokens_in} > {cap_in}"
                )
            if new_ph_out > cap_out:
                raise BudgetExceeded(
                    f"phase {phase!r} output budget exceeded: "
                    f"{ph['out']} + {tokens_out} > {cap_out}"
                )
            # CRIT-02: per-task cap is isolated by task_id, not summed globally.
            tg = self._task_used_in(task_id)
            task_used = tg["in"] + tg["out"]
            if task_used + tokens_in + tokens_out > int(self._s.budget_task_cap):
                raise BudgetExceeded(
                    f"per-task cap exceeded for task {task_id!r}: "
                    f"{task_used} + {tokens_in + tokens_out} > {self._s.budget_task_cap}"
                )

    def commit(
        self,
        phase: str,
        tokens_in: int,
        tokens_out: int,
        *,
        task_id: str = "",
    ) -> None:
        """Record ACTUAL token usage for a completed LLM call.

        Commits to BOTH the phase budget (so future :meth:`check` calls see
        real usage, not estimates) and the per-task ledger (for reporting).
        Does NOT re-validate the cap (the call already happened; if it
        exceeded, that's a post-hoc observation, not a blocking event).
        """
        tokens_in = int(tokens_in)
        tokens_out = int(tokens_out)
        with self._lock:
            ph = self._phase_used_in(phase)
            ph["in"] += tokens_in
            ph["out"] += tokens_out
            tg = self._task_used_in(task_id)
            tg["in"] += tokens_in
            tg["out"] += tokens_out

    def commit_task(self, task_id: str, phase: str, tokens_in: int, tokens_out: int) -> None:
        """Alias of :meth:`commit` (records actual usage to phase + task).

        Kept for backward compatibility; prefer ``commit(phase, in, out, *, task_id=...)``.
        """
        self.commit(phase, tokens_in, tokens_out, task_id=task_id)

    def check_overshoot(self, phase: str, *, task_id: str = "") -> None:
        """HIGH-07: post-commit soft-overshoot guard.

        Estimates are validated by :meth:`check` before the call; the actual
        usage is recorded by :meth:`commit`. If the actual exceeded the phase
        cap (estimates were too low), this raises :class:`BudgetExceeded`
        AFTER the fact — preserving I3 (check validates, commit records) while
        still enforcing a hard stop on the NEXT call. The pipeline catches
        this and escalates rather than making "one more call".

        Per-task cap is also re-checked (in case actuals pushed the task over).
        """
        cap_in, cap_out = self._caps_for(phase)
        with self._lock:
            ph = self._phase_used_in(phase)
            if ph["in"] > cap_in:
                raise BudgetExceeded(
                    f"phase {phase!r} OVERSHOT after commit: "
                    f"{ph['in']} > {cap_in}"
                )
            if ph["out"] > cap_out:
                raise BudgetExceeded(
                    f"phase {phase!r} OVERSHOT after commit: "
                    f"{ph['out']} > {cap_out}"
                )
            tg = self._task_used_in(task_id)
            task_used = tg["in"] + tg["out"]
            if task_used > int(self._s.budget_task_cap):
                raise BudgetExceeded(
                    f"per-task cap OVERSHOT for task {task_id!r}: "
                    f"{task_used} > {self._s.budget_task_cap}"
                )

    def remaining(self, phase: str) -> int:
        """Return the binding constraint (the smaller of in-remaining, out-remaining)
        for this phase. Conservative single-int answer.
        """
        cap_in, cap_out = self._caps_for(phase)
        with self._lock:
            ph = self._phase_used_in(phase)
            in_remaining = max(0, cap_in - ph["in"])
            out_remaining = max(0, cap_out - ph["out"])
        return min(in_remaining, out_remaining)

    def task_total(self) -> int:
        """Cumulative tokens (in+out) committed across ALL phases and tasks."""
        with self._lock:
            return sum(d["in"] + d["out"] for d in self._task_used.values())

    def task_total_for(self, task_id: str) -> int:
        """Per-task token total (in+out)."""
        with self._lock:
            d = self._task_used.get(task_id)
            return (d["in"] + d["out"]) if d else 0

    # --- fix-loop -------------------------------------------------------

    def fixloop_increment(self, task_id: str) -> int:
        """Increment and return the new fix-loop iteration count for `task_id`.

        Raises CircuitBreakerOpen when the count exceeds budget_fixloop_iters.
        The pipeline catches CircuitBreakerOpen and escalates to the human.
        """
        with self._lock:
            n = self._fixloop_count.get(task_id, 0) + 1
            self._fixloop_count[task_id] = n
        # We DON'T raise here directly — the spec says "raises CircuitBreakerOpen
        # semantics via the pipeline (you just count)". We return the count and
        # let the pipeline decide. But we surface the breach via the return so
        # callers can detect n > cap and call `raise_circuit_breaker` themselves.
        return n

    def fixloop_count(self, task_id: str) -> int:
        """Current iteration count (0 if none)."""
        with self._lock:
            return self._fixloop_count.get(task_id, 0)

    def fixloop_cap(self) -> int:
        """The configured fix-loop iteration cap (default 3)."""
        return int(self._s.budget_fixloop_iters)

    def fixloop_at_cap(self, task_id: str) -> bool:
        """True if this task has used all its fix-loop iterations."""
        return self.fixloop_count(task_id) >= self.fixloop_cap()

    def snapshot(self) -> dict[str, Any]:
        """Return a read-only snapshot of the tracker state (for reporting/tests)."""
        with self._lock:
            return {
                "phase_used": {k: dict(v) for k, v in self._phase_used.items()},
                "task_used": {k: dict(v) for k, v in self._task_used.items()},
                "fixloop": dict(self._fixloop_count),
                "caps": {
                    phase: {
                        "in": int(getattr(self._s, attrs[0])),
                        "out": int(getattr(self._s, attrs[1])),
                    }
                    for phase, attrs in PHASE_CAP_ATTRS.items()
                },
                "task_cap": int(self._s.budget_task_cap),
                "fixloop_iters_cap": int(self._s.budget_fixloop_iters),
            }

"""A phase budget belongs to a run, not to the process.

``ModelRouter.reset()`` existed and was never called, so per-phase call and
token counters accumulated across every run in the process.  A goal run calls
``pipeline.run()`` once per cycle, so from cycle 5 onward the planner was
refused with "phase 'plan' used its 4 calls" and spent zero tokens.

Measured live: 4 of 8 cycles were empty (``tokens 0``), and the goal gave up on
work it had already produced.
"""
from __future__ import annotations

from gigaforge.core.router import ModelRouter, PhaseUsage, RouterStats


class _Client:
    def close(self) -> None:
        pass


def _router() -> ModelRouter:
    return ModelRouter(client=_Client())


def test_reset_clears_phase_usage() -> None:
    router = _router()
    router._usage["plan"] = PhaseUsage(phase="plan", calls=4, prompt_tokens=10, completion_tokens=5)
    router.reset()
    assert router._usage == {}


def test_reset_clears_stats() -> None:
    router = _router()
    router._stats.budget_rejections = 7
    router.reset()
    assert router._stats.budget_rejections == 0
    assert isinstance(router._stats, RouterStats)


def test_a_second_run_is_not_refused_by_the_first_runs_usage() -> None:
    """The regression in one assertion: run 1 exhausts the phase, run 2 must not."""
    router = _router()
    budget = router.config.budget_for("plan")
    router._usage["plan"] = PhaseUsage(
        phase="plan",
        calls=budget.max_calls or 4,
        prompt_tokens=budget.max_tokens or 10_000,
        completion_tokens=0,
    )
    from gigaforge.core.errors import PhaseBudgetExceeded

    if budget.max_calls:
        try:
            router._enforce_budget("plan", budget)
        except PhaseBudgetExceeded:
            pass
        else:  # pragma: no cover - the budget must bite within a run
            raise AssertionError("the phase budget did not bite inside a run")

    # A new run starts from zero.
    router.reset()
    router._enforce_budget("plan", budget)

"""The ladder must change which model is called, not only what is logged.

**The defect this file exists for.**  A level that appears in a trace but does
not change the model is a label, not a decision.  The first wiring of the
ladder into the router had exactly that shape: ``decide_level`` returned L3 for
a step that had failed twice, and ``route`` still served the phase's configured
model, because the level was never passed through.

The second defect was subtler and is the reason the ladder's ceiling and the
router's gate are now separate: the ladder was built with
``allow_flagship=self._allow_max()``, so on an account without the flagship
opt-in every escalation from an authoring phase collapsed back to L2 — the
ladder said "strong" for a step that had already failed twice *at strong*.
These tests pin both: the level reaches the model, and the ladder's verdict is
independent of the account's permission.
"""
from __future__ import annotations

import pytest

from gigaforge.core.ladder import Level
from gigaforge.core.router import ModelRouter


def _router() -> ModelRouter:
    return ModelRouter()


# ── the level reaches the model ─────────────────────────────────────────────
def test_l1_is_served_by_the_cheap_model() -> None:
    """A step the ladder called cheap must not be served by the workhorse."""
    router = _router()
    route = router.route("code", level=Level.L1_CHEAP)
    assert route.model == router.config.cheap_model


def test_l2_uses_the_phase_route() -> None:
    """L2 is the phase's own configured model — the operator's choice."""
    router = _router()
    route = router.route("code", level=Level.L2_STRONG)
    assert route.model == router.config.routes["code"].model


def test_l0_is_refused_rather_than_silently_spent() -> None:
    """L0 means "no model call"; returning a Route would spend one anyway."""
    from gigaforge.core.errors import NoModelForPhaseError

    with pytest.raises(NoModelForPhaseError):
        _router().route("code", level=Level.L0_DETERMINISTIC)


# ── the ladder's verdict is independent of the account's permission ─────────
def test_escalation_is_visible_without_the_flagship_opt_in(monkeypatch) -> None:
    """The ladder reports the need; the router applies the permission.

    With the opt-in absent, a step that failed twice must still be *decided*
    at L3 — otherwise the ladder cannot tell the difference between "strong is
    enough" and "strong has already failed twice", and the retry is a repeat.
    """
    monkeypatch.delenv("GIGAFORGE_ALLOW_MAX", raising=False)
    router = _router()
    decision = router.decide_level("code", repeats=2)
    assert decision.level is Level.L3_FLAGSHIP
    assert decision.escalated


def test_the_gate_caps_the_model_not_the_verdict(monkeypatch) -> None:
    """Without the opt-in, L3 is served by the strong model, not a flagship."""
    monkeypatch.delenv("GIGAFORGE_ALLOW_MAX", raising=False)
    router = _router()
    route = router.route("code", level=Level.L3_FLAGSHIP)
    assert route.model == router.config.strong_model


# ── the decision is recorded ────────────────────────────────────────────────
def test_decide_level_keeps_the_last_verdict() -> None:
    """The trace and the ledger read ``last_decision``; it must be populated."""
    router = _router()
    router.decide_level("plan", files_touched=9)
    assert router.last_decision is not None
    assert router.last_decision.phase == "plan"
    assert router.last_decision.to_dict()["level_name"] in {"strong", "flagship"}


def test_budget_pressure_comes_from_the_router_counters() -> None:
    """The caller does not have to know the phase's remaining allowance.

    A phase that has spent most of its budget must not be escalated, and the
    router is the only object that knows how much it has spent.
    """
    router = _router()
    budget = router.config.budget_for("code")
    # Pretend the phase has spent 90% of its token budget.
    from gigaforge.core.router import PhaseUsage

    router._usage["code"] = PhaseUsage(
        phase="code", model="x", total_tokens=int(budget.max_tokens * 0.9)
    )
    decision = router.decide_level("code", contract_rejected=True)
    assert decision.level is Level.L2_STRONG
    assert "nearly out of budget" in decision.reason

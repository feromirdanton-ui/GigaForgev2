"""The decision ladder: a step buys the least intelligence that can do the work.

**The defect this file exists for.**  The harness had a router that answered
"which model for this phase" and no mechanism that answered "should a model be
called at all, and should this step be retried on a *more* capable one".  Two
consequences were measured:

* a step the harness could settle deterministically still paid for a model
  call, because every phase went to its configured model on the first attempt;
* a step that had already failed twice was retried on the same model that had
  just failed twice — the fix loop's ``escalate_after_repeats`` stopped the
  loop, it did not raise the level.

These tests pin the *rule*, not one example of it: the escalation is driven by
signals, so a test that only checked "attempt 2 escalates" would pass while the
contract-rejection and complexity signals stayed dead.  Each signal gets its
own case, and the ceiling and the budget-pressure suppression get theirs.
"""
from __future__ import annotations

import pytest

from gigaforge.core.ladder import (
    Decision,
    Ladder,
    LadderPolicy,
    Level,
    Signals,
)


# ── the starting level ──────────────────────────────────────────────────────
def test_a_mechanical_phase_starts_cheap() -> None:
    """Retrieval, triage and summarisation do not need the workhorse model."""
    ladder = Ladder()
    for phase in ("retrieve", "review", "summarize", "classify"):
        decision = ladder.decide(Signals(phase=phase))
        assert decision.level is Level.L1_CHEAP, phase


def test_an_authoring_phase_starts_strong() -> None:
    """Planning, exploration, authoring and repair start at the workhorse."""
    ladder = Ladder()
    for phase in ("plan", "react", "code", "fix"):
        decision = ladder.decide(Signals(phase=phase))
        assert decision.level is Level.L2_STRONG, phase


def test_an_unknown_phase_is_not_guessed_downwards() -> None:
    """A phase the policy does not know starts capable, not cheap.

    The safe direction: a wrong "cheap" verdict produces a bad artifact that
    has to be redone at the strong level anyway, so the cheap call is not
    saved, it is spent twice.
    """
    decision = Ladder().decide(Signals(phase="something_new"))
    assert decision.level is Level.L2_STRONG


# ── L0: the call that is not made ───────────────────────────────────────────
def test_a_deterministic_step_calls_no_model() -> None:
    """The cheapest model call is the one the harness does not make."""
    decision = Ladder().decide(
        Signals(phase="fix", deterministic_available=True)
    )
    assert decision.level is Level.L0_DETERMINISTIC
    assert decision.is_deterministic
    assert "no model call" in decision.reason


def test_deterministic_wins_over_every_other_signal() -> None:
    """Even a rejected, repeated, wide step is L0 when the harness can do it.

    Otherwise the escalation signals would drag a mechanical step up to a
    model call it does not need — which is the defect, not the fix.
    """
    decision = Ladder().decide(
        Signals(
            phase="fix",
            deterministic_available=True,
            contract_rejected=True,
            repeats=5,
            attempt=4,
            files_touched=20,
        )
    )
    assert decision.level is Level.L0_DETERMINISTIC


# ── escalation on evidence ──────────────────────────────────────────────────
def test_a_contract_rejection_escalates() -> None:
    """A produced-and-judged-wrong artifact is evidence, not a guess."""
    base = Ladder().decide(Signals(phase="code"))
    rejected = Ladder().decide(Signals(phase="code", contract_rejected=True))
    assert rejected.level > base.level
    assert rejected.escalated
    assert "contract rejected" in rejected.reason


def test_repeated_identical_failures_escalate() -> None:
    """The same prompt at the same level produces the same diff."""
    policy = LadderPolicy(repeats_before_escalation=2)
    ladder = Ladder(policy)
    below = ladder.decide(Signals(phase="fix", repeats=1))
    at = ladder.decide(Signals(phase="fix", repeats=2))
    assert below.level is Level.L2_STRONG
    assert at.level > below.level


def test_attempt_count_escalates() -> None:
    """A second attempt at the same level is a second chance, not a third."""
    policy = LadderPolicy(attempts_before_escalation=2)
    ladder = Ladder(policy)
    assert ladder.decide(Signals(phase="code", attempt=1)).level is Level.L2_STRONG
    assert ladder.decide(Signals(phase="code", attempt=2)).level > Level.L2_STRONG


def test_a_wide_step_is_treated_as_complex() -> None:
    """Many files or many sections is a measured complexity signal."""
    policy = LadderPolicy(complex_files=4, complex_sections=5)
    ladder = Ladder(policy)
    assert ladder.decide(Signals(phase="code", files_touched=3)).level is Level.L2_STRONG
    assert ladder.decide(Signals(phase="code", files_touched=4)).level > Level.L2_STRONG
    assert ladder.decide(Signals(phase="code", sections=5)).level > Level.L2_STRONG


def test_escalation_is_capped_at_the_ceiling() -> None:
    """Every signal at once still cannot exceed the top level."""
    policy = LadderPolicy(allow_flagship=True)
    decision = Ladder(policy).decide(
        Signals(
            phase="code",
            contract_rejected=True,
            repeats=9,
            attempt=9,
            files_touched=99,
            sections=99,
        )
    )
    assert decision.level is Level.L3_FLAGSHIP


def test_flagship_is_opt_in() -> None:
    """Without the opt-in the ladder stops at strong, whatever the signals."""
    decision = Ladder(LadderPolicy(allow_flagship=False)).decide(
        Signals(phase="code", contract_rejected=True, repeats=9, attempt=9)
    )
    assert decision.level is Level.L2_STRONG
    assert "flagship is not enabled" in decision.reason


# ── the suppression that keeps escalation honest ────────────────────────────
def test_budget_pressure_suppresses_escalation() -> None:
    """Escalating a nearly-exhausted phase buys a costlier failure.

    The phase has spent 90% of its budget; a flagship call would either be
    refused by the budget gate or spend the remainder on one attempt.  Staying
    at the starting level is the honest choice, and the reason says so.
    """
    decision = Ladder().decide(
        Signals(
            phase="code",
            contract_rejected=True,
            tokens_used=90_000,
            tokens_budget=100_000,
        )
    )
    assert decision.level is Level.L2_STRONG
    assert not decision.escalated
    assert "nearly out of budget" in decision.reason


def test_budget_pressure_does_not_block_a_first_escalation_with_room() -> None:
    """With budget left, the same signals do escalate."""
    decision = Ladder().decide(
        Signals(
            phase="code",
            contract_rejected=True,
            tokens_used=10_000,
            tokens_budget=100_000,
        )
    )
    assert decision.escalated


# ── the operator override ───────────────────────────────────────────────────
def test_an_operator_override_wins() -> None:
    """A pinned level is not second-guessed by the signals."""
    decision = Ladder().decide(
        Signals(phase="code", forced_level=Level.L1_CHEAP, contract_rejected=True)
    )
    assert decision.level is Level.L1_CHEAP
    assert "operator pinned" in decision.reason


# ── escalate() for evidence the ladder could not see ────────────────────────
def test_escalate_raises_one_level_with_a_reason() -> None:
    """A verifier that rejected the artifact can raise the level itself."""
    ladder = Ladder(LadderPolicy(allow_flagship=True))
    base = ladder.decide(Signals(phase="code"))
    raised = ladder.escalate(base, reason="the render check found the page broken")
    assert raised.level > base.level
    assert "render check" in raised.reason
    assert raised.escalated


def test_escalate_at_the_ceiling_is_a_no_op() -> None:
    """There is nothing above the top, and the decision says so by not moving."""
    ladder = Ladder(LadderPolicy(allow_flagship=True))
    top = Decision(level=Level.L3_FLAGSHIP, phase="code", reason="already top")
    assert ladder.escalate(top, reason="again").level is Level.L3_FLAGSHIP


# ── the verdict is auditable ────────────────────────────────────────────────
def test_the_decision_carries_its_evidence() -> None:
    """A report must be able to print *why*, not only *what*."""
    decision = Ladder().decide(
        Signals(phase="code", contract_rejected=True, files_touched=7)
    )
    payload = decision.to_dict()
    assert payload["level_name"] in {"strong", "flagship"}
    assert payload["evidence"]["files_touched"] == 7
    assert payload["evidence"]["contract_rejected"] is True
    assert payload["reason"]


def test_the_policy_is_data_a_test_can_pin() -> None:
    """Thresholds live in the policy, so retuning does not edit the decision."""
    strict = Ladder(LadderPolicy(repeats_before_escalation=1))
    lenient = Ladder(LadderPolicy(repeats_before_escalation=5))
    signals = Signals(phase="fix", repeats=2)
    assert strict.decide(signals).level > lenient.decide(signals).level

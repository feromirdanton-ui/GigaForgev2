"""The decision ladder — how much intelligence a step is allowed to buy.

**Why this module exists.**  The harness had a router, and the router answered
"which model for this phase".  It did not answer the question that actually
decides cost and quality: *should a model be called at all, and if so, how
expensive one?*  Every phase went to its configured model on the first attempt,
so a task the harness could settle deterministically still paid for a flagship
call, and a task that had already failed twice was retried on the same model
that had just failed twice.

The ladder makes the escalation explicit and evidence-based:

    L0  deterministic   the harness computes the answer itself — no model call
    L1  cheap           a small/fast model: classification, extraction, triage
    L2  strong          the workhorse: diff authoring, planning, repair
    L3  flagship        the expensive reasoning model, only when L2 has failed
                        on evidence, or the task is provably hard

**The rule that makes it a ladder and not a menu.**  A step starts at the
lowest level that can plausibly do the work, and moves up only on *evidence*:

* a contract rejection (the artifact was produced and judged wrong),
* a repeated identical failure (the same prompt against the same context
  produces the same diff — repeating it at the same level proves nothing),
* a complexity signal the harness measured (files touched, tokens, sections),
* an explicit operator override.

It never moves up because a step "seems hard", and it never moves down
mid-task: a level that has been paid for is not refunded, and a model that
failed is not asked again at the same level.

**Why L0 matters most.**  The cheapest model call is the one that is not made.
Everything the harness can compute deterministically — a known repair, a
template fill, a structural fix, a routing verdict — belongs at L0.  The
analysis that prompted this module measured the opposite: the harness called a
model to decide things it already knew, and then called it again to fix what it
had got wrong.

**What is recorded.**  Every decision carries ``level``, ``reason`` and
``evidence``, and lands in the ledger, so a report can answer "how much of this
run was deterministic, and where did the money go" instead of only "how many
tokens".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Mapping, Optional

__all__ = [
    "Level",
    "Decision",
    "Signals",
    "LadderPolicy",
    "Ladder",
    "LEVEL_NAMES",
]


class Level(IntEnum):
    """How much intelligence a step is allowed to buy.

    Ordered, so ``max()`` is the escalation and ``>=`` is a comparison a caller
    can read.  ``L0`` is not "a cheaper model" — it is *no model call*, which is
    why it is a level and not a model name.
    """

    L0_DETERMINISTIC = 0
    L1_CHEAP = 1
    L2_STRONG = 2
    L3_FLAGSHIP = 3


LEVEL_NAMES: dict[Level, str] = {
    Level.L0_DETERMINISTIC: "deterministic",
    Level.L1_CHEAP: "cheap",
    Level.L2_STRONG: "strong",
    Level.L3_FLAGSHIP: "flagship",
}


@dataclass(frozen=True)
class Signals:
    """What the harness measured about the step, before choosing a level.

    Every field is a *measurement*, not a guess: ``attempt`` is the loop's own
    counter, ``repeats`` is the number of identical failures the fix loop
    observed, ``contract_rejected`` is a verdict a contract actually returned.
    A signal that cannot be measured is left at its default and does not
    escalate anything.
    """

    #: Which phase is asking (``plan``, ``code``, ``fix``, ``review``…).
    phase: str = ""
    #: 0 for the first attempt, 1 for the first repair, and so on.
    attempt: int = 0
    #: Identical failures observed in a row by the fix loop.
    repeats: int = 0
    #: A contract judged a produced artifact wrong (not "the model errored").
    contract_rejected: bool = False
    #: Files the step is expected to touch.  A wide change is harder to get
    #: right in one shot, so it is a complexity signal.
    files_touched: int = 0
    #: Sections a page plan contains, when the step is a page section.
    sections: int = 0
    #: Tokens the phase has already spent, against its budget.
    tokens_used: int = 0
    tokens_budget: int = 0
    #: The task kind the classifier returned (``code``/``artifact``/``answer``…).
    task_kind: str = ""
    #: True when the step is a mechanical transform the harness can do itself.
    deterministic_available: bool = False
    #: Operator override: pin the level, whatever the signals say.
    forced_level: Optional[Level] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "attempt": self.attempt,
            "repeats": self.repeats,
            "contract_rejected": self.contract_rejected,
            "files_touched": self.files_touched,
            "sections": self.sections,
            "tokens_used": self.tokens_used,
            "tokens_budget": self.tokens_budget,
            "task_kind": self.task_kind,
            "deterministic_available": self.deterministic_available,
            "forced_level": int(self.forced_level) if self.forced_level is not None else None,
        }


@dataclass(frozen=True)
class LadderPolicy:
    """The thresholds that turn signals into a level.

    Kept as data so a test can pin the rule and an operator can retune it
    without editing the decision code.
    """

    #: A phase whose first attempt is mechanical starts at L1, not L2.
    cheap_phases: tuple[str, ...] = ("retrieve", "review", "summarize", "classify")
    #: A phase that authors or repairs starts at L2.
    strong_phases: tuple[str, ...] = ("plan", "react", "code", "fix")
    #: Identical failures before the ladder escalates one level.
    repeats_before_escalation: int = 2
    #: Attempts at the current level before escalating on attempt count alone.
    attempts_before_escalation: int = 2
    #: Files touched at or above which a step is treated as complex.
    complex_files: int = 4
    #: Sections at or above which a page step is treated as complex.
    complex_sections: int = 5
    #: Fraction of the phase budget spent at which the ladder stops escalating
    #: (escalating a phase that is nearly out of budget buys a more expensive
    #: failure, not a success).
    budget_pressure: float = 0.85
    #: Whether the ladder may express a need for L3 at all.
    #:
    #: This is **not** the account's permission to use a flagship model — the
    #: router already owns that gate (``GIGAFORGE_ALLOW_MAX``), and it is the
    #: right place for it: the ladder says what the step *needs*, the router
    #: says what the account *permits*.  Conflating the two made the ladder
    #: useless for the common case: with the ceiling at L2, every escalation
    #: from an authoring phase was a no-op, so a step that had failed twice was
    #: still retried at the level that had just failed twice — the exact defect
    #: this module exists to remove.  An operator who wants to cap the ladder
    #: itself sets this to ``False``.
    allow_flagship: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "cheap_phases": list(self.cheap_phases),
            "strong_phases": list(self.strong_phases),
            "repeats_before_escalation": self.repeats_before_escalation,
            "attempts_before_escalation": self.attempts_before_escalation,
            "complex_files": self.complex_files,
            "complex_sections": self.complex_sections,
            "budget_pressure": self.budget_pressure,
            "allow_flagship": self.allow_flagship,
        }


@dataclass(frozen=True)
class Decision:
    """The ladder's verdict for one step, with the evidence that produced it."""

    level: Level
    phase: str = ""
    #: Why this level — a sentence a report can print.
    reason: str = ""
    #: The measurements that drove it, so the verdict is auditable.
    evidence: Mapping[str, Any] = field(default_factory=dict)
    #: True when the level was raised from the phase's starting level.
    escalated: bool = False

    @property
    def is_deterministic(self) -> bool:
        """True when the step must not call a model at all."""
        return self.level is Level.L0_DETERMINISTIC

    @property
    def name(self) -> str:
        return LEVEL_NAMES.get(self.level, str(int(self.level)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": int(self.level),
            "level_name": self.name,
            "phase": self.phase,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "escalated": self.escalated,
        }


class Ladder:
    """Chooses a :class:`Level` for a step, and escalates it on evidence.

    Stateless by design: the caller owns the attempt counter and the failure
    history, and passes them in as :class:`Signals`.  A ladder that remembered
    state would be a second source of truth about how many times a step had
    run, and the fix loop already owns that number.
    """

    def __init__(self, policy: Optional[LadderPolicy] = None) -> None:
        self.policy = policy or LadderPolicy()

    # ── the decision ────────────────────────────────────────────────────────
    def decide(self, signals: Signals) -> Decision:
        """The level for this step, from the signals alone.

        Order matters and is deliberate:

        1. an operator override wins outright;
        2. a mechanical step the harness can do itself is L0 — no model call;
        3. the phase's starting level (cheap phases start at L1, authoring
           phases at L2);
        4. escalation on evidence, one level per reason, capped at L3 and
           suppressed under budget pressure.
        """
        policy = self.policy
        phase = signals.phase or ""

        if signals.forced_level is not None:
            return Decision(
                level=signals.forced_level,
                phase=phase,
                reason="operator pinned the level",
                evidence={"forced_level": int(signals.forced_level)},
            )

        if signals.deterministic_available:
            return Decision(
                level=Level.L0_DETERMINISTIC,
                phase=phase,
                reason=(
                    "the harness can compute this step itself, so no model call "
                    "is made"
                ),
                evidence={"deterministic_available": True},
            )

        start = self._starting_level(phase, signals.task_kind)
        level = start
        reasons: list[str] = [f"phase {phase!r} starts at {LEVEL_NAMES[start]}"]

        # ── escalation on evidence ──────────────────────────────────────────
        if signals.contract_rejected:
            level = self._raise(level, 1)
            reasons.append("a contract rejected the produced artifact")

        if signals.repeats >= policy.repeats_before_escalation > 0:
            level = self._raise(level, 1)
            reasons.append(
                f"{signals.repeats} identical failures in a row — the same prompt "
                "at the same level produces the same diff"
            )

        if signals.attempt >= policy.attempts_before_escalation > 0:
            level = self._raise(level, 1)
            reasons.append(f"attempt {signals.attempt} at this level")

        if self._is_complex(signals):
            level = self._raise(level, 1)
            reasons.append(
                "the step is wide: "
                f"{signals.files_touched} file(s), {signals.sections} section(s)"
            )

        # ── the ceiling ─────────────────────────────────────────────────────
        if level is Level.L3_FLAGSHIP and not policy.allow_flagship:
            level = Level.L2_STRONG
            reasons.append("flagship is not enabled, so the ladder stops at strong")

        if self._under_budget_pressure(signals) and level > start:
            level = start
            reasons.append(
                "the phase is nearly out of budget, so escalating would buy a "
                "more expensive failure"
            )

        return Decision(
            level=level,
            phase=phase,
            reason="; ".join(reasons),
            evidence=signals.to_dict(),
            escalated=level > start,
        )

    def escalate(self, decision: Decision, *, reason: str) -> Decision:
        """One level up from *decision*, with a stated reason.

        Used by a caller that has evidence the ladder could not see — a
        verifier that rejected the artifact, a render check that found the page
        broken.  The ceiling still applies.
        """
        level = self._raise(decision.level, 1)
        if level is Level.L3_FLAGSHIP and not self.policy.allow_flagship:
            level = Level.L2_STRONG
        if level == decision.level:
            return decision
        return Decision(
            level=level,
            phase=decision.phase,
            reason=f"{decision.reason}; escalated: {reason}",
            evidence=dict(decision.evidence),
            escalated=True,
        )

    # ── internals ───────────────────────────────────────────────────────────
    def _starting_level(self, phase: str, task_kind: str = "") -> Level:
        # For an answer goal the ``summarize`` phase is not a summary — it is
        # the deliverable.  Measured live: "What is 2+2? Answer in chat" went
        # to the cheap model, which returned an empty string, and the run
        # reported ``finish_action`` with ``chars: 0`` twice and then
        # ``needs_attention``.  A phase that writes the product is not a cheap
        # phase, whatever it is called.
        if task_kind == "answer" and phase in ("summarize", "answer"):
            return Level.L2_STRONG
        if phase in self.policy.cheap_phases:
            return Level.L1_CHEAP
        if phase in self.policy.strong_phases:
            return Level.L2_STRONG
        # An unknown phase is not guessed at: it starts where authoring starts,
        # because the safe direction is "capable enough to do the work".
        return Level.L2_STRONG

    @staticmethod
    def _raise(level: Level, steps: int) -> Level:
        return Level(min(int(Level.L3_FLAGSHIP), int(level) + steps))

    def _is_complex(self, signals: Signals) -> bool:
        policy = self.policy
        if signals.files_touched >= policy.complex_files > 0:
            return True
        if signals.sections >= policy.complex_sections > 0:
            return True
        return False

    def _under_budget_pressure(self, signals: Signals) -> bool:
        budget = int(signals.tokens_budget or 0)
        if budget <= 0:
            return False
        return (signals.tokens_used / budget) >= self.policy.budget_pressure

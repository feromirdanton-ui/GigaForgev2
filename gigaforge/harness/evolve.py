"""Propose–Probe–Compose: improve the harness without letting it rewrite itself.

**The problem this solves.**  GigaForge's quality lives in eight places: the
system prompt, the phase prompts, the skills, the ladder thresholds, the
contracts, the retry policy, the context budget, the tool set.  Improving them
by hand is slow and unmeasured; improving them by letting a model rewrite the
repository is how a harness becomes a random walk with a green bench.

Harness²'s answer is a *frozen* search: the weights never move, only the
harness's own text and numbers do, and only through a declared surface.  This
module is that mechanism, ported as an idea rather than as code — the original
runs on Gemini and edits a Python package; this runs on GigaChat and edits a
``SurfaceSpec``.

**The three phases.**

* **Propose** — a model (or a human) returns a *partial* surface: only the
  components it wants to change.  Everything it omits is inherited from the
  current surface.  This is the contract that makes a proposal reviewable: a
  diff of one field, not a rewrite of eight.
* **Probe** — the candidate is measured on the bench.  Not "does it look
  better" — the same tasks, the same fixtures, the same noise floor.  A
  candidate that cannot be measured is not a candidate.
* **Compose** — the candidate is accepted only if it beats the incumbent by
  more than the noise floor **and** does not regress any task that the
  incumbent passed.  A change that trades one task for another is not an
  improvement; it is a different harness.

**What is deliberately absent.**  No mutation of the repository, no automatic
commit, no self-modifying prompt.  The output of a successful compose is a
``SurfaceSpec`` and a report; applying it is a human's decision, because the
bench is a sample and the product is not.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "SurfaceSpec",
    "Proposal",
    "ProbeResult",
    "ComposeVerdict",
    "RoundOutcome",
    "propose",
    "compose",
    "probe",
    "run_round",
    "EDITABLE_COMPONENTS",
]


#: The eight components a proposal may touch.  Anything not in this set is
#: rejected — a surface that can edit anything is not a surface, it is a
#: rewrite.  The names are the harness's own vocabulary, so a proposal reads
#: like a change to the thing it changes.
EDITABLE_COMPONENTS: tuple[str, ...] = (
    "system_prompt",
    "phase_prompts",
    "skills",
    "ladder_thresholds",
    "contracts",
    "retry_policy",
    "context_budget",
    "tool_set",
)


@dataclass
class SurfaceSpec:
    """The editable surface of the harness, as data.

    **Why a dataclass and not a dict.**  A dict accepts any key, so a typo in a
    proposal silently becomes a new component that nothing reads — the change
    looks applied and does nothing.  A declared surface rejects the typo.

    **Omitted is inherited.**  ``compose_surface`` merges a partial proposal
    over the incumbent, so a proposal that names one component changes one
    component.  This is the contract that makes a proposal a diff instead of a
    rewrite, and it is the single most important property here: without it,
    every proposal is a full replacement and every review is a full re-read.
    """

    system_prompt: str = ""
    phase_prompts: dict[str, str] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    ladder_thresholds: dict[str, float] = field(default_factory=dict)
    contracts: dict[str, Any] = field(default_factory=dict)
    retry_policy: dict[str, Any] = field(default_factory=dict)
    context_budget: int = 0
    tool_set: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_prompt": self.system_prompt,
            "phase_prompts": dict(self.phase_prompts),
            "skills": list(self.skills),
            "ladder_thresholds": dict(self.ladder_thresholds),
            "contracts": copy.deepcopy(self.contracts),
            "retry_policy": dict(self.retry_policy),
            "context_budget": self.context_budget,
            "tool_set": list(self.tool_set),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SurfaceSpec":
        unknown = set(data) - set(EDITABLE_COMPONENTS)
        if unknown:
            raise ValueError(
                "a proposal may only touch the declared surface; unknown "
                f"component(s): {', '.join(sorted(unknown))}"
            )
        return cls(**{k: v for k, v in data.items() if k in EDITABLE_COMPONENTS})

    def fingerprint(self) -> str:
        """A stable identity for the surface, for the ledger."""
        import hashlib

        blob = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Proposal:
    """A partial surface plus the reason it was proposed."""

    changes: dict[str, Any]
    rationale: str = ""
    source: str = "model"

    def to_dict(self) -> dict[str, Any]:
        return {
            "changes": copy.deepcopy(self.changes),
            "rationale": self.rationale,
            "source": self.source,
        }


@dataclass
class ProbeResult:
    """What the bench said about one surface."""

    surface_fingerprint: str
    pass_rates: dict[str, float] = field(default_factory=dict)
    tokens_total: int = 0
    cost_micros: int = 0
    error: str = ""

    @property
    def mean_pass_rate(self) -> float:
        if not self.pass_rates:
            return 0.0
        return sum(self.pass_rates.values()) / len(self.pass_rates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface_fingerprint,
            "mean_pass_rate": round(self.mean_pass_rate, 4),
            "tasks": {k: round(v, 4) for k, v in self.pass_rates.items()},
            "tokens_total": self.tokens_total,
            "cost_micros": self.cost_micros,
            "error": self.error,
        }


@dataclass
class ComposeVerdict:
    """Whether a probed candidate replaces the incumbent."""

    accepted: bool
    reason: str
    delta: float = 0.0
    floor: float = 0.0
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "delta": round(self.delta, 4),
            "floor": round(self.floor, 4),
            "regressions": list(self.regressions),
            "improvements": list(self.improvements),
        }


def propose(
    incumbent: SurfaceSpec,
    changes: Mapping[str, Any],
    *,
    rationale: str = "",
    source: str = "model",
) -> Proposal:
    """Validate a partial proposal against the declared surface.

    Rejects an unknown component and a no-op change.  A proposal that changes
    nothing is not a proposal — it is a wasted probe, and probing it would
    report "no improvement" as if the search had tried something.
    """
    unknown = set(changes) - set(EDITABLE_COMPONENTS)
    if unknown:
        raise ValueError(
            "a proposal may only touch the declared surface; unknown "
            f"component(s): {', '.join(sorted(unknown))}"
        )
    # Validate the *value*, not only the name.  The check lived in
    # ``compose_surface`` alone, and ``propose`` is the entry point the CLI
    # uses -- so ``{"ladder_thresholds": {"attempts_before_escalation": -1}}``
    # was reported "accepted" by the command that exists to reject it.  A
    # validation that only one of two entry points performs is not a contract.
    for key, value in changes.items():
        _check_component_value(key, value)
    current = incumbent.to_dict()
    effective = {k: v for k, v in changes.items() if current.get(k) != v}
    if not effective:
        raise ValueError("the proposal changes nothing on the declared surface")
    return Proposal(changes=dict(effective), rationale=rationale, source=source)


def compose_surface(incumbent: SurfaceSpec, proposal: Proposal) -> SurfaceSpec:
    """Merge a partial proposal over the incumbent — omitted is inherited.

    This is the contract, in one function.  A proposal naming one component
    produces a surface that differs in exactly that component; every other
    component is the incumbent's, by identity of value.
    """
    merged = incumbent.to_dict()
    for key, value in proposal.changes.items():
        if key not in EDITABLE_COMPONENTS:
            raise ValueError(f"unknown component {key!r}")
        _check_component_value(key, value)
        merged[key] = copy.deepcopy(value)
    return SurfaceSpec.from_dict(merged)


def _check_component_value(key: str, value: Any) -> None:
    """Reject a value the harness cannot act on.

    **Why the surface check was not enough.**  ``compose_surface`` verified the
    *name* of every component and nothing about its value, so
    ``{"ladder_thresholds": {"attempts_before_escalation": -1}}`` was accepted
    as a valid proposal.  A negative threshold is not a tuning choice: the
    ladder compares ``attempt >= threshold``, so every call would escalate
    immediately and the cheap model would never be used -- a proposal that
    "improves" the bench by spending the flagship on everything.  The surface
    is a contract about shape; a contract that accepts any value is a name
    check wearing a contract's clothes.
    """
    if key == "ladder_thresholds":
        if not isinstance(value, Mapping):
            raise ValueError("ladder_thresholds must be an object")
        for name, threshold in value.items():
            if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
                raise ValueError(f"ladder_thresholds[{name!r}] must be a number")
            if threshold < 0:
                raise ValueError(
                    f"ladder_thresholds[{name!r}] must be >= 0, got {threshold}"
                )
    elif key == "context_budget":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("context_budget must be a non-negative integer")
    elif key in {"skills", "tool_set"}:
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError(f"{key} must be a list of strings")
    elif key in {"phase_prompts", "contracts", "retry_policy"}:
        if not isinstance(value, Mapping):
            raise ValueError(f"{key} must be an object")
    elif key == "system_prompt":
        if not isinstance(value, str):
            raise ValueError("system_prompt must be a string")


def compose(
    incumbent: ProbeResult,
    candidate: ProbeResult,
    *,
    floor: float = 0.0,
    allow_regressions: bool = False,
) -> ComposeVerdict:
    """Decide whether the candidate replaces the incumbent.

    **Two conditions, both required.**

    1. The mean pass rate must improve by more than ``floor`` — the noise floor
       from ``bench/report.py``, so a candidate that "improved" 4/5 → 5/5 is not
       accepted.
    2. No task the incumbent passed may regress.  A change that trades one task
       for another is a different harness, not a better one, and the bench is a
       sample: the task it broke is the one a user will hit.

    ``allow_regressions`` exists for a deliberate trade, and it is off by
    default because the default is the honest one.
    """
    if candidate.error:
        return ComposeVerdict(
            accepted=False,
            reason=f"the candidate could not be measured: {candidate.error}",
        )
    if not candidate.pass_rates:
        return ComposeVerdict(
            accepted=False, reason="the candidate produced no measurements"
        )
    delta = candidate.mean_pass_rate - incumbent.mean_pass_rate
    regressions = [
        name for name, rate in incumbent.pass_rates.items()
        if rate > 0 and candidate.pass_rates.get(name, 0.0) < rate
    ]
    improvements = [
        name for name, rate in candidate.pass_rates.items()
        if rate > incumbent.pass_rates.get(name, 0.0)
    ]
    if regressions and not allow_regressions:
        # Checked before the delta on purpose.  A candidate that trades one task
        # for another can have a delta of exactly zero, and reporting "did not
        # clear the noise floor" would hide the real reason: it broke something.
        return ComposeVerdict(
            accepted=False,
            reason=(
                "the candidate regressed task(s) the incumbent passed: "
                + ", ".join(regressions)
            ),
            delta=delta, floor=floor,
            regressions=regressions, improvements=improvements,
        )
    if delta <= floor:
        return ComposeVerdict(
            accepted=False,
            reason=(
                f"the candidate improved by {delta:+.3f}, which does not clear "
                f"the noise floor of {floor:.3f}"
            ),
            delta=delta, floor=floor,
            regressions=regressions, improvements=improvements,
        )
    return ComposeVerdict(
        accepted=True,
        reason=(
            f"the candidate improved by {delta:+.3f} (floor {floor:.3f}) with "
            f"{len(improvements)} improvement(s) and {len(regressions)} regression(s)"
        ),
        delta=delta, floor=floor,
        regressions=regressions, improvements=improvements,
    )


def probe(
    surface: SurfaceSpec,
    run_bench: Callable[[SurfaceSpec], Mapping[str, Any]],
) -> ProbeResult:
    """Measure a surface with the bench, and never let a crash look like a result.

    A probe that raises is a candidate that could not be measured — which is a
    rejection, not a zero score.  Reporting it as 0.0 would make a broken
    candidate look like a bad one, and the search would learn the wrong lesson.
    """
    try:
        raw = run_bench(surface)
    except Exception as exc:  # noqa: BLE001 - a failed probe is a rejection
        return ProbeResult(
            surface_fingerprint=surface.fingerprint(),
            error=f"{type(exc).__name__}: {exc}",
        )
    rates: dict[str, float] = {}
    for name, value in (raw.get("pass_rates") or {}).items():
        try:
            rates[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return ProbeResult(
        surface_fingerprint=surface.fingerprint(),
        pass_rates=rates,
        tokens_total=int(raw.get("tokens_total") or 0),
        cost_micros=int(raw.get("cost_micros") or 0),
    )


@dataclass
class RoundOutcome:
    """One Propose–Probe–Compose round, with the trajectory it advanced.

    **Why a round is a value and not a side effect.**  The search's whole claim
    is that a change was measured and attributed.  A round that returns nothing
    but a boolean cannot be audited: it cannot say which component moved, what
    the hypothesis was, or whether the budget allowed the bundle.  Returning the
    record makes the history a by-product of running, not a second thing to
    remember to write.
    """

    round_index: int
    budget: int
    proposal: Proposal | None = None
    candidate: SurfaceSpec | None = None
    probe: ProbeResult | None = None
    verdict: ComposeVerdict | None = None
    pruned: list[str] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)
    rejected_reason: str = ""

    @property
    def adopted(self) -> bool:
        return bool(self.verdict and self.verdict.accepted and not self.pruned)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_index,
            "budget": self.budget,
            "components": sorted(self.proposal.changes) if self.proposal else [],
            "hypothesis": self.proposal.rationale if self.proposal else "",
            "adopted": self.adopted,
            "pruned": list(self.pruned),
            "directives": list(self.directives),
            "rejected_reason": self.rejected_reason,
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "probe": self.probe.to_dict() if self.probe else None,
        }


def run_round(
    incumbent_surface: SurfaceSpec,
    incumbent_probe: ProbeResult,
    changes: Mapping[str, Any],
    *,
    round_index: int,
    total_rounds: int,
    run_bench: Callable[[SurfaceSpec], Mapping[str, Any]],
    history: Any = None,
    observation: Mapping[str, Any] | None = None,
    rationale: str = "",
    floor: float = 0.0,
    allow_regressions: bool = False,
) -> RoundOutcome:
    """One full round: budget, screen, propose, probe, compose, record.

    **The order is the mechanism.**  Each step can end the round, and the order
    is chosen so the cheapest rejection comes first:

    1. *Budget* — a bundle larger than the round allows is refused before any
       model call.  This is the annealed constraint: late rounds are
       single-variable experiments.
    2. *Prune* — a candidate that trips the prune set is rejected **before** it
       is probed.  Probing it first and rejecting after would spend the bench on
       a candidate whose score must not be read, and — worse — the score would
       exist, and a score that exists gets averaged in by someone.
    3. *Propose* — validated against the declared surface, values included.
    4. *Probe* — measured; a crash is a rejection, not a zero.
    5. *Compose* — accepted only above the noise floor and with no regression.

    ``history`` is optional so the function is usable in a test without one, but
    when given it is advanced: the round's edit is recorded with its hypothesis
    and its verdict, which is what makes the next round's directives possible.
    """
    from gigaforge.harness.prune import check_prune
    from gigaforge.harness.search import edit_budget, enforce_budget

    budget = edit_budget(round_index, total_rounds)
    outcome = RoundOutcome(round_index=round_index, budget=budget)

    # 1. The annealed budget.
    try:
        enforce_budget(changes, budget)
    except ValueError as exc:
        outcome.rejected_reason = str(exc)
        return outcome

    # 2. The prune set, before the bench.
    if observation is not None:
        prune_verdict = check_prune(observation)
        if prune_verdict.pruned:
            outcome.pruned = list(prune_verdict.tripped)
            outcome.rejected_reason = "; ".join(prune_verdict.details)
            if history is not None:
                for component in changes:
                    history.record(
                        round=round_index, component=str(component),
                        hypothesis=rationale, verdict="pruned", adopted=False,
                    )
            return outcome

    # 3. Propose.
    try:
        proposal = propose(incumbent_surface, changes, rationale=rationale)
    except ValueError as exc:
        outcome.rejected_reason = str(exc)
        return outcome
    outcome.proposal = proposal
    candidate = compose_surface(incumbent_surface, proposal)
    outcome.candidate = candidate

    # 4. Probe.
    probed = probe(candidate, run_bench)
    outcome.probe = probed

    # 5. Compose.
    verdict = compose(
        incumbent_probe, probed, floor=floor, allow_regressions=allow_regressions,
    )
    outcome.verdict = verdict

    if history is not None:
        for component in proposal.changes:
            history.record(
                round=round_index,
                component=str(component),
                hypothesis=rationale,
                score_before=incumbent_probe.mean_pass_rate,
                score_after=probed.mean_pass_rate,
                cost_before=incumbent_probe.cost_micros,
                cost_after=probed.cost_micros,
                verdict="accepted" if verdict.accepted else "rejected",
                adopted=outcome.adopted,
            )
        outcome.directives = history.exploration_directives(EDITABLE_COMPONENTS)
    return outcome

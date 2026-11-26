"""Measured routing: what each rung of the ladder actually bought.

**Why this exists.**  The ladder decides a level per phase call, and the run
report records one row per call.  That is a log, not a measurement: it says what
happened, not whether the decision was right.  The question an operator asks is
"is L3 worth its price?" — and answering it needs the calls grouped by level with
their outcome and their cost.

The table is built from rows the pipeline already writes, so it costs nothing at
run time and cannot disagree with the log.  It is deliberately a *pure function*
of the rows: no model call, no clock, no I/O.

**What it can and cannot say.**  It can say "L3 was used 4 times, 3 of them
accepted, at 2.1x the cost of L2".  It cannot say "L3 was necessary" — a call
that succeeded at L3 might have succeeded at L2.  That counterfactual needs an
A/B run, which is a different experiment; the table is the evidence that makes
such an experiment worth designing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["LevelStats", "routing_table", "routing_advice"]

#: The ladder's rungs, in order.  Kept as plain ints so this module does not
#: import the ladder and cannot create a cycle.
LEVEL_NAMES = {0: "L0 deterministic", 1: "L1 cheap", 2: "L2 strong", 3: "L3 flagship"}


@dataclass
class LevelStats:
    """What one rung did, over every call that used it."""

    level: int
    calls: int = 0
    accepted: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_micros: int = 0
    models: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return LEVEL_NAMES.get(self.level, f"L{self.level}")

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.calls if self.calls else 0.0

    @property
    def cost_per_call(self) -> int:
        return self.cost_micros // self.calls if self.calls else 0

    @property
    def tokens_per_call(self) -> int:
        return (self.tokens_in + self.tokens_out) // self.calls if self.calls else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "name": self.name,
            "calls": self.calls,
            "accepted": self.accepted,
            "acceptance_rate": round(self.acceptance_rate, 4),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_per_call": self.tokens_per_call,
            "cost_micros": self.cost_micros,
            "cost_per_call_micros": self.cost_per_call,
            "models": dict(self.models),
        }


def routing_table(
    rows: Iterable[Mapping[str, Any]],
    *,
    phase_cost: Mapping[str, int] | None = None,
) -> list[LevelStats]:
    """Group routing rows by level, with cost attributed per call.

    ``rows`` are the pipeline's routing entries: ``phase``, ``level``,
    ``tokens_in``, ``tokens_out``, ``model``, and optionally ``accepted``.

    Cost is not on the row — the pipeline prices per phase, not per call — so it
    is attributed evenly across the calls of a phase.  That is exact when a phase
    is called once (the common case) and an honest average when it is called
    several times, which is the only thing the data supports.  Inventing a
    per-call price from token counts would be a second pricing model, and two
    pricing models disagree.
    """
    rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    per_phase_calls: dict[str, int] = {}
    for row in rows:
        phase = str(row.get("phase") or "")
        per_phase_calls[phase] = per_phase_calls.get(phase, 0) + 1

    stats: dict[int, LevelStats] = {}
    for row in rows:
        try:
            level = int(row.get("level"))
        except (TypeError, ValueError):
            # A row with no level is a call the ladder did not decide (a
            # deterministic step, or a run from before the ladder was wired).
            # Counting it as L2 would inflate the rung that is supposed to be
            # the measured one.
            continue
        entry = stats.setdefault(level, LevelStats(level=level))
        entry.calls += 1
        entry.tokens_in += int(row.get("tokens_in") or 0)
        entry.tokens_out += int(row.get("tokens_out") or 0)
        model = str(row.get("model") or "")
        if model:
            entry.models[model] = entry.models.get(model, 0) + 1
        if row.get("accepted") is True:
            entry.accepted += 1
        phase = str(row.get("phase") or "")
        if phase_cost and per_phase_calls.get(phase):
            entry.cost_micros += int(phase_cost.get(phase, 0) or 0) // per_phase_calls[phase]
    return [stats[level] for level in sorted(stats)]


def routing_advice(table: Sequence[LevelStats]) -> list[str]:
    """Plain statements about the table, each one a fact it supports.

    Deliberately not recommendations: "L3 costs 2.1x L2 and was accepted 3 of 4
    times" is a fact; "use L3 less" is a judgement that needs the counterfactual
    this data does not contain.  The operator makes the call; the harness makes
    sure they are looking at the numbers.
    """
    notes: list[str] = []
    by_level = {entry.level: entry for entry in table}
    for entry in table:
        if entry.calls and entry.accepted == 0:
            notes.append(
                f"{entry.name}: {entry.calls} call(s), none accepted — this rung "
                "has not yet produced a result that survived the gate"
            )
    cheap, strong = by_level.get(1), by_level.get(2)
    if cheap and strong and cheap.cost_per_call and strong.cost_per_call:
        ratio = strong.cost_per_call / cheap.cost_per_call
        notes.append(
            f"L2 costs {ratio:.1f}x L1 per call "
            f"({strong.cost_per_call} vs {cheap.cost_per_call} micro-USD)"
        )
    flagship = by_level.get(3)
    if flagship and strong and strong.cost_per_call and flagship.cost_per_call:
        ratio = flagship.cost_per_call / strong.cost_per_call
        notes.append(
            f"L3 costs {ratio:.1f}x L2 per call and was accepted "
            f"{flagship.accepted}/{flagship.calls}"
        )
    if not table:
        notes.append("no levelled calls in this run: the ladder was not consulted")
    return notes

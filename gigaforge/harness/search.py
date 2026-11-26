"""The search trajectory: what the harness has already tried, and what it may try next.

**What was missing.**  ``evolve.py`` has the edit space (eight components, frozen
weights, omitted-is-inherited) and ``critic.py`` has the screen.  What neither
has is the thing RRSI is actually about: the *regularization of the trajectory*.
An open edit space overfits a fixed evolve set, and the fix is not to close the
space — it is to constrain how the search moves through it.

Four constraints, all deterministic, all computable from records the loop
already produces:

* **An annealed edit budget.**  Early rounds may bundle several independent
  edits (explore); later rounds may bundle one (exploit).  A candidate that
  changes eight components at once cannot be attributed to any of them, so its
  score teaches the search nothing — it is a random walk that happens to be
  green.
* **An edit history.**  One record per edit: component, hypothesis, score and
  cost before/after, verdict.  The proposer is conditioned on it, so a
  hypothesis that was already falsified is not redrawn.  Without this the search
  re-proposes the same idea every round and reports "no improvement" as if it
  had tried something new.
* **A stall flag and exploration directives.**  When recent rounds stop
  yielding, the next proposal is *required* to touch a component that has never
  been exercised.  A search that keeps editing the same prompt is not searching;
  it is polishing.
* **Novelty.**  The share of structural component types (tool set, skills,
  context budget) that have never appeared in a *winning* edit.  A harness that
  only ever tunes numbers has not explored its own space, and its "converged"
  state is a local optimum in one dimension.

**What is deliberately not here.**  No model call, no clock, no I/O beyond an
explicit ``save``/``load``.  The trajectory is a pure function of the records,
so a replayed history produces the same directives — which is what makes a
search auditable after the fact.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "EditRecord",
    "History",
    "edit_budget",
    "enforce_budget",
    "novelty",
    "STRUCTURAL_COMPONENTS",
]


#: The components whose *type* is structural rather than numeric.  RRSI's
#: ``K_str`` is ``client_tool, skill, memory, subagent``; three of the four map
#: onto GigaForge's surface directly.  ``subagent`` does not: delegation here is
#: a runtime mode (``--subagents``), not a component a proposal can edit, and
#: pretending otherwise would let a proposal "explore subagents" by editing a
#: prompt.  The mapping is stated rather than silently widened.
STRUCTURAL_COMPONENTS: tuple[str, ...] = ("tool_set", "skills", "context_budget")


@dataclass(frozen=True)
class EditRecord:
    """One edit, with the evidence that decided it.

    ``adopted`` is RRSI's ``a_t``: 1 only for the edits of the candidate that
    became the next incumbent.  It is a separate field from ``verdict`` on
    purpose — a candidate can be *accepted* by the composer and still not be
    adopted (a later candidate in the same round wins), and conflating the two
    makes the yield statistic count edits that never shipped.
    """

    round: int
    component: str
    hypothesis: str = ""
    score_before: float = 0.0
    score_after: float = 0.0
    cost_before: int = 0
    cost_after: int = 0
    verdict: str = "rejected"
    adopted: bool = False

    @property
    def delta(self) -> float:
        return self.score_after - self.score_before

    @property
    def cost_delta(self) -> int:
        return self.cost_after - self.cost_before

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "component": self.component,
            "hypothesis": self.hypothesis,
            "score_before": round(self.score_before, 4),
            "score_after": round(self.score_after, 4),
            "delta": round(self.delta, 4),
            "cost_before": self.cost_before,
            "cost_after": self.cost_after,
            "cost_delta": self.cost_delta,
            "verdict": self.verdict,
            "adopted": self.adopted,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EditRecord":
        return cls(
            round=int(data.get("round") or 0),
            component=str(data.get("component") or ""),
            hypothesis=str(data.get("hypothesis") or ""),
            score_before=float(data.get("score_before") or 0.0),
            score_after=float(data.get("score_after") or 0.0),
            cost_before=int(data.get("cost_before") or 0),
            cost_after=int(data.get("cost_after") or 0),
            verdict=str(data.get("verdict") or "rejected"),
            adopted=bool(data.get("adopted")),
        )


@dataclass
class History:
    """The edit history ``L_t``, the tried set ``T_t`` and the recent yield ``g_t``.

    One JSONL record per edit, in the order the edits were made.  The order is
    the only thing that makes "recent" meaningful, so it is preserved rather
    than sorted.
    """

    records: list[EditRecord] = field(default_factory=list)

    # ── writing ────────────────────────────────────────────────────────────
    def append(self, record: EditRecord) -> EditRecord:
        self.records.append(record)
        return record

    def record(
        self,
        *,
        round: int,
        component: str,
        hypothesis: str = "",
        score_before: float = 0.0,
        score_after: float = 0.0,
        cost_before: int = 0,
        cost_after: int = 0,
        verdict: str = "rejected",
        adopted: bool = False,
    ) -> EditRecord:
        return self.append(EditRecord(
            round=round, component=component, hypothesis=hypothesis,
            score_before=score_before, score_after=score_after,
            cost_before=cost_before, cost_after=cost_after,
            verdict=verdict, adopted=adopted,
        ))

    # ── reading ────────────────────────────────────────────────────────────
    def tried(self) -> set[str]:
        """Every component that has been edited at least once."""
        return {r.component for r in self.records if r.component}

    def untried(self, all_components: Sequence[str]) -> list[str]:
        """Components the search has never touched, in the surface's own order."""
        tried = self.tried()
        return [c for c in all_components if c not in tried]

    def recent_yield(self, window: int = 3) -> float:
        """Share of the last ``window`` edits that were adopted.

        A *yield*, not a score: it answers "is the search still finding things",
        which is a different question from "is the harness good".  A search can
        be at a high score and yielding nothing — that is exactly when it should
        be redirected.
        """
        if not self.records:
            return 0.0
        recent = self.records[-max(1, window):]
        return sum(1 for r in recent if r.adopted) / len(recent)

    def stall_flag(self, *, window: int = 3, threshold: float = 0.0) -> bool:
        """True when the last ``window`` edits produced nothing adopted.

        ``threshold`` is a yield, not a count, so the flag means the same thing
        whether the window is three edits or thirty.
        """
        if len(self.records) < max(1, window):
            # Not enough evidence to call it a stall.  Reporting one after two
            # edits would redirect a search that has barely started.
            return False
        return self.recent_yield(window) <= threshold

    def exploration_directives(
        self,
        all_components: Sequence[str],
        *,
        slots: int = 1,
    ) -> list[str]:
        """Components the next proposal must touch, when the search has stalled.

        Empty when the search is yielding: a directive is a correction, and
        issuing one to a search that is working is noise the proposer has to
        ignore.
        """
        if not self.stall_flag():
            return []
        return self.untried(all_components)[:max(0, slots)]

    def prune_set(self, *, min_attempts: int = 2) -> list[str]:
        """Components that have been tried repeatedly and never helped.

        RRSI hands this to the proposer as "the machinery to remove".  The
        ``min_attempts`` guard matters: one failed edit is a hypothesis that did
        not pan out, not a component that does not work.
        """
        attempts: dict[str, int] = {}
        wins: dict[str, int] = {}
        for r in self.records:
            if not r.component:
                continue
            attempts[r.component] = attempts.get(r.component, 0) + 1
            if r.adopted:
                wins[r.component] = wins.get(r.component, 0) + 1
        return sorted(
            c for c, n in attempts.items()
            if n >= min_attempts and not wins.get(c)
        )

    def falsified(self, *, limit: int = 12) -> list[str]:
        """Hypotheses already tried and rejected, newest first.

        The proposer reads this so it does not redraw a falsified idea.  Only
        rejected ones: an adopted hypothesis is in the incumbent, and repeating
        it is a no-op the surface check already rejects.
        """
        out: list[str] = []
        for r in reversed(self.records):
            if r.adopted or not r.hypothesis:
                continue
            out.append(f"[{r.component}] {r.hypothesis}")
            if len(out) >= limit:
                break
        return out

    # ── persistence ────────────────────────────────────────────────────────
    def to_jsonl(self) -> str:
        return "\n".join(
            json.dumps(r.to_dict(), ensure_ascii=False) for r in self.records
        )

    @classmethod
    def from_jsonl(cls, text: str) -> "History":
        records: list[EditRecord] = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # A truncated final line is a crash mid-write, not a corrupt
                # history: dropping it keeps every complete record.
                continue
            if isinstance(payload, Mapping):
                records.append(EditRecord.from_dict(payload))
        return cls(records=records)

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_jsonl() + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "History":
        target = Path(path)
        if not target.exists():
            return cls()
        return cls.from_jsonl(target.read_text(encoding="utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "edits": len(self.records),
            "adopted": sum(1 for r in self.records if r.adopted),
            "tried": sorted(self.tried()),
            "recent_yield": round(self.recent_yield(), 4),
            "stalled": self.stall_flag(),
            "prune_set": self.prune_set(),
        }


def edit_budget(
    round_index: int,
    total_rounds: int,
    *,
    start: int = 3,
    floor: int = 1,
) -> int:
    """How many independent edits one candidate may bundle at round ``t``.

    **Annealed, not constant.**  A constant budget of one makes the search slow
    to leave a bad starting point; a constant budget of eight makes every
    candidate unattributable.  RRSI anneals: explore wide early, then narrow, so
    the last rounds are single-variable experiments whose result means something.

    The decay is linear in the remaining rounds rather than exponential, because
    the budget is a small integer and an exponential curve spends most of its
    life at the floor — which is the constant-budget-of-one failure again, just
    reached faster.
    """
    total = max(1, int(total_rounds))
    t = max(0, min(int(round_index), total - 1))
    span = max(1, start - floor)
    # ``t + 1`` so the *last* round lands exactly on the floor.  With ``t`` the
    # final round of a ten-round search still allowed two edits, which is the
    # one round whose result is supposed to be a single-variable experiment.
    value = start - math.floor(span * (t + 1) / total)
    return max(floor, min(start, value))


def enforce_budget(changes: Mapping[str, Any], budget: int) -> dict[str, Any]:
    """Reject a proposal that bundles more edits than the round allows.

    **Rejected, not trimmed.**  Trimming would silently drop components the
    proposer asked for, and the resulting candidate would be measured as if it
    were the proposal — so the score would be attributed to a change nobody
    made.  A refusal names the budget and the components, which is information
    the proposer can act on.
    """
    if budget < 1:
        raise ValueError(f"the edit budget must be at least 1, got {budget}")
    if len(changes) > budget:
        raise ValueError(
            f"this round allows {budget} independent edit(s); the proposal "
            f"bundles {len(changes)}: {', '.join(sorted(changes))}. Split it "
            "into separate candidates so each score is attributable."
        )
    return dict(changes)


def novelty(
    history: History,
    *,
    structural: Sequence[str] = STRUCTURAL_COMPONENTS,
) -> float:
    """Share of structural component types never present in a winning edit.

    **Why a share and not a count.**  The number has to be comparable between a
    search that has run three rounds and one that has run thirty.  ``1.0`` means
    "no structural component has ever won", which is the state a harness reaches
    when it only tunes thresholds — and it is the state that looks converged
    while being one-dimensional.
    """
    if not structural:
        return 0.0
    won = {r.component for r in history.records if r.adopted}
    untouched = [c for c in structural if c not in won]
    return len(untouched) / len(structural)

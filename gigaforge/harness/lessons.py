"""The improvement harness: lessons that survive one task and guide the next.

**The second level of recursion.**  ``evolve.py`` refines the harness *for the
current task* — propose, probe, compose.  That is task-level recursion, and on
its own it has a specific failure: every task starts from the same place, so a
lesson learned on task 3 is forgotten by task 4.  Harness² calls the missing
half *harnessing-level recursion*: after each task, reflect on what happened and
update a persistent record that conditions the next task's refinement.

**Why a lesson needs evidence.**  A reflection step that writes "be more careful
with imports" is worse than nothing: it is unfalsifiable, it accumulates, and
after twenty tasks the improvement harness is a pile of advice that no
measurement can confirm or remove.  So a lesson here is a *claim with a
citation*: the observation that produced it, the task it came from, and the
number of times it has since been confirmed or contradicted.  A lesson that is
contradicted more than confirmed is retired, not kept "just in case".

**What is deliberately absent.**  No model call.  The reflection is a function
of the run's own records — which failures occurred, in which phase, with which
error — because a model asked to reflect will produce a plausible lesson whether
or not one exists, and the whole point is that the lesson is supported by
evidence rather than by fluency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "Lesson",
    "ImprovementHarness",
    "reflect",
    "LESSON_KINDS",
]


#: The kinds of lesson the reflector can draw.  A closed set, because an open
#: one becomes a free-text field and a free-text field becomes a diary.
LESSON_KINDS: tuple[str, ...] = (
    "phase_failure",      # a phase failed repeatedly: its prompt or budget is wrong
    "tool_misuse",        # a tool was called with arguments its contract rejects
    "budget_exhaustion",  # the run ran out of calls before finishing
    "gate_rejection",     # the acceptance gate rejected a plausible-looking result
    "regression",         # a change broke a task the incumbent passed
)


@dataclass
class Lesson:
    """One evidence-supported claim about the harness, with its citation.

    ``confirmations`` and ``contradictions`` are counts, not booleans: a lesson
    is a hypothesis about the harness, and a hypothesis that holds on one task
    and fails on the next is not established.  The ratio is what decides whether
    it is still offered to the proposer.
    """

    kind: str
    claim: str
    evidence: str
    task: str = ""
    confirmations: int = 1
    contradictions: int = 0

    @property
    def support(self) -> float:
        """Confirmations as a share of all observations; 0.0 when never observed."""
        total = self.confirmations + self.contradictions
        return self.confirmations / total if total else 0.0

    @property
    def retired(self) -> bool:
        """A lesson contradicted more often than confirmed is retired.

        The threshold is deliberately strict (strictly more contradictions than
        confirmations): a lesson that is right half the time is not a lesson, it
        is a coin flip, and offering it to the proposer spends a probe on it.
        """
        return self.contradictions > self.confirmations

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "claim": self.claim,
            "evidence": self.evidence,
            "task": self.task,
            "confirmations": self.confirmations,
            "contradictions": self.contradictions,
            "support": round(self.support, 4),
            "retired": self.retired,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Lesson":
        return cls(
            kind=str(data.get("kind") or ""),
            claim=str(data.get("claim") or ""),
            evidence=str(data.get("evidence") or ""),
            task=str(data.get("task") or ""),
            confirmations=int(data.get("confirmations") or 0),
            contradictions=int(data.get("contradictions") or 0),
        )

    def key(self) -> tuple[str, str]:
        """Identity for merging: the same claim from two tasks is one lesson."""
        return (self.kind, self.claim)


@dataclass
class ImprovementHarness:
    """The persistent record that conditions the next task's refinement.

    Two things live here, and they are different in kind:

    * **Lessons** — claims about *why* the harness underperformed, each with its
      evidence.  These condition the proposer: a lesson about phase failures
      points it at ``phase_prompts``, a lesson about budget exhaustion at
      ``context_budget``.
    * **Adopted surfaces** — the fingerprints of surfaces that actually won, so
      a later task can start from the best known harness instead of the default.
      A fingerprint, not the surface: the surface is in the ledger, and copying
      it here would create two sources of truth that drift.
    """

    lessons: list[Lesson] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)

    # ── lessons ────────────────────────────────────────────────────────────
    def add(self, lesson: Lesson) -> Lesson:
        """Merge a lesson, or record a confirmation of an existing one.

        Merging by ``(kind, claim)`` is what makes the counts meaningful: the
        same failure seen on three tasks is one lesson confirmed three times,
        not three lessons that each look equally strong.
        """
        for existing in self.lessons:
            if existing.key() == lesson.key():
                existing.confirmations += lesson.confirmations
                existing.contradictions += lesson.contradictions
                if lesson.evidence and lesson.evidence not in existing.evidence:
                    existing.evidence = f"{existing.evidence}; {lesson.evidence}"
                return existing
        self.lessons.append(lesson)
        return lesson

    def contradict(self, kind: str, claim: str, *, evidence: str = "") -> None:
        """Record that a lesson did not hold on a later task."""
        for existing in self.lessons:
            if existing.key() == (kind, claim):
                existing.contradictions += 1
                if evidence:
                    existing.evidence = f"{existing.evidence}; contradicted: {evidence}"
                return

    def active(self) -> list[Lesson]:
        """Lessons still worth offering to the proposer, strongest first."""
        live = [l for l in self.lessons if not l.retired]
        return sorted(live, key=lambda l: (-l.support, -l.confirmations))

    def retired(self) -> list[Lesson]:
        return [l for l in self.lessons if l.retired]

    def components_to_try(self) -> list[str]:
        """Which surface components the active lessons point at.

        This is the bridge from reflection to proposal: a lesson is only useful
        if it names something the proposer can change.  A lesson with no
        component is a comment, and comments do not get probed.
        """
        mapping = {
            "phase_failure": "phase_prompts",
            "tool_misuse": "tool_set",
            "budget_exhaustion": "context_budget",
            "gate_rejection": "contracts",
            "regression": "ladder_thresholds",
        }
        out: list[str] = []
        for lesson in self.active():
            component = mapping.get(lesson.kind)
            if component and component not in out:
                out.append(component)
        return out

    # ── adopted surfaces ───────────────────────────────────────────────────
    def adopt(self, fingerprint: str) -> None:
        if fingerprint and fingerprint not in self.adopted:
            self.adopted.append(fingerprint)

    def best_known(self) -> str:
        """The most recently adopted surface, or empty when none has won."""
        return self.adopted[-1] if self.adopted else ""

    # ── persistence ────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "lessons": [l.to_dict() for l in self.lessons],
            "adopted": list(self.adopted),
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ImprovementHarness":
        target = Path(path)
        if not target.exists():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A corrupt improvement harness must not stop a run: the lessons are
            # an optimisation, and losing them costs a probe, not a result.
            return cls()
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            lessons=[
                Lesson.from_dict(item)
                for item in (payload.get("lessons") or [])
                if isinstance(item, Mapping)
            ],
            adopted=[str(x) for x in (payload.get("adopted") or [])],
        )


def reflect(
    observation: Mapping[str, Any],
    *,
    task: str = "",
) -> list[Lesson]:
    """Turn one run's records into lessons, deterministically.

    **Why deterministic.**  A model asked "what did you learn" produces a
    plausible lesson whether or not one exists — and the plausible ones are the
    ones that sound wise and cannot be tested.  Every lesson here is derived
    from a count in the observation, so it can be checked against the run that
    produced it.

    The observation is the shape the ledger and the bench already produce:
    ``phase_failures`` (phase -> count), ``tool_errors`` (tool -> count),
    ``budget_exhausted`` (bool), ``gate_rejections`` (list of reasons),
    ``regressions`` (list of task names).
    """
    lessons: list[Lesson] = []

    for phase, count in (observation.get("phase_failures") or {}).items():
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        if n < 2:
            # One failure is an incident; two is a pattern.  A lesson drawn from
            # a single failure sends the proposer after a phase that may have
            # failed for a reason outside the harness.
            continue
        lessons.append(Lesson(
            kind="phase_failure",
            claim=f"phase {phase!r} fails repeatedly",
            evidence=f"{n} failure(s) in phase {phase!r}",
            task=task,
        ))

    for tool, count in (observation.get("tool_errors") or {}).items():
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        if n < 2:
            continue
        lessons.append(Lesson(
            kind="tool_misuse",
            claim=f"tool {tool!r} is called in a way its contract rejects",
            evidence=f"{n} rejected call(s) to {tool!r}",
            task=task,
        ))

    if observation.get("budget_exhausted"):
        lessons.append(Lesson(
            kind="budget_exhaustion",
            claim="the run exhausts its call budget before finishing",
            evidence="the run ended on a budget refusal, not on a result",
            task=task,
        ))

    rejections = observation.get("gate_rejections") or []
    if len(rejections) >= 2:
        lessons.append(Lesson(
            kind="gate_rejection",
            claim="the acceptance gate rejects results that look complete",
            evidence=f"{len(rejections)} rejection(s): {rejections[0]}",
            task=task,
        ))

    for name in (observation.get("regressions") or []):
        lessons.append(Lesson(
            kind="regression",
            claim=f"a change regressed task {name!r}",
            evidence=f"task {name!r} passed before the change and failed after",
            task=task,
        ))

    return lessons

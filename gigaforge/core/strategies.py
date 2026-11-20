"""Pluggable reasoning stages for the task DAG.

The V2 DAG has a fixed skeleton — intake → index → slice → cache → plan → act →
validate → fix → settle → summarize → persist — but two of those stages are
*policy*, not mechanics:

``plan``   how the goal is decomposed;
``review`` who is allowed to veto a diff that the deterministic gate accepted.

This module makes both pluggable without touching the DAG.  It is the synthesis
point between the V2 engine and the reasoning machinery that ships in
:mod:`gigaforge.agents`:

* :class:`TreeOfThoughtPlan` drives the K-branch search of
  :mod:`gigaforge.agents.tot` (varied-temperature candidates, zero-token
  heuristic scoring, optional judge call) through the DAG's own router, so the
  tree search obeys the same budget and the same trace as a linear plan.
* :class:`ConstitutionalReview` runs the principle set of
  :mod:`gigaforge.agents.constitutional` — five deterministic blockers plus one
  soft principle judged by a cheap model call — as the DAG's ``review`` stage.

Both are selected by name (constructor argument or environment) and both
degrade to the legacy behaviour when disabled, so the default path is unchanged:

    GIGAFORGE_PLAN_STRATEGY=linear|tot          (default: linear)
    GIGAFORGE_REVIEW_STRATEGY=none|constitutional|llm   (default: none)

Every strategy talks to the model exclusively through ``ask``, a callable with
the signature ``ask(phase, prompt, temperature=None) -> str`` supplied by the
pipeline.  A strategy therefore cannot bypass the router, the budget tracker,
the circuit breaker or the trace — the properties the DAG exists to guarantee.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("gigaforge.core.strategies")

__all__ = [
    "AskFn",
    "PlanOutcome",
    "PlanStrategy",
    "LinearPlan",
    "TreeOfThoughtPlan",
    "ReActPlan",
    "ReviewOutcome",
    "ReviewStrategy",
    "NoReview",
    "ConstitutionalReview",
    "LLMReview",
    "PLAN_STRATEGIES",
    "REVIEW_STRATEGIES",
    "build_plan_strategy",
    "build_review_strategy",
    "score_steps",
]

#: ``ask(phase, prompt, temperature=None) -> answer text``.
AskFn = Callable[..., str]

#: The temperature ladder the tree search walks.  Diversity across branches is
#: the entire point of the technique: identical temperatures would produce K
#: near-identical plans and waste K-1 calls.
_CANDIDATE_TEMPS: tuple[float, ...] = (0.0, 0.25, 0.5, 0.8)

#: Step-count sweet spot for the heuristic scorer.
_IDEAL_STEPS = 3
_MAX_STEPS = 7


# ══════════════════════════════════════════════════════════════════════════
# plan strategies
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class PlanOutcome:
    """What a plan strategy produces, plus the evidence it used to decide."""

    steps: list[str] = field(default_factory=list)
    strategy: str = "linear"
    score: float = 0.0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    judge_used: bool = False
    #: ``{"target_function": ..., "target_file": ...}`` when the strategy could
    #: localise the change.  Carried out of planning because the coder needs it:
    #: without it the model re-derives the target from the goal and guesses.
    localization: dict[str, str] = field(default_factory=dict)
    #: The structured steps, when the strategy produced them.  ``steps`` is the
    #: rendered form the DAG executes; this is the same plan before rendering,
    #: so a consumer can read ``tool``/``args`` instead of parsing a string.
    structured: list[Any] = field(default_factory=list)
    #: Files the exploration episode wrote itself.  Carried out of planning so
    #: the pipeline can compute their diff instead of asking the coder to
    #: reproduce work that already exists on disk.
    written_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "steps": list(self.steps),
            "score": round(float(self.score), 3),
            "judge_used": self.judge_used,
            "localization": dict(self.localization),
            "candidates": [
                {
                    "branch": c.get("branch"),
                    "temperature": c.get("temperature"),
                    "score": round(float(c.get("score") or 0.0), 3),
                    "steps": len(c.get("steps") or []),
                    "error": c.get("error"),
                }
                for c in self.candidates
            ],
        }


class PlanStrategy(Protocol):
    """Decompose a goal into ordered steps."""

    name: str

    def plan(
        self,
        goal: str,
        slice_text: str,
        *,
        ask: AskFn,
        parse: Callable[[str], list[str]],
        budget_remaining: int = 0,
    ) -> PlanOutcome: ...


class LinearPlan:
    """One model call, one plan — the V2 default.

    ``budget_remaining`` and ``parse`` keep the signature identical to the tree
    search so the pipeline never branches on which strategy it holds.
    """

    name = "linear"

    def plan(
        self,
        goal: str,
        slice_text: str,
        *,
        ask: AskFn,
        parse: Callable[[str], list[str]],
        budget_remaining: int = 0,
    ) -> PlanOutcome:
        from .prompt import build_phase_prompt

        prompt = build_phase_prompt("plan", goal=goal, slice_text=slice_text).render()
        text = ask("plan", prompt)
        steps = parse(text)
        return PlanOutcome(steps=steps, strategy=self.name, score=score_steps(steps))


class TreeOfThoughtPlan:
    """K-branch plan search — the ``agents.tot`` algorithm inside the DAG.

    Each branch is one cheap-model call at a different temperature.  Every
    parseable candidate is scored by :func:`score_steps` (zero tokens).  If a
    judge is enabled and the top two candidates are within ``judge_margin``
    points, one extra call arbitrates; otherwise the heuristic winner stands.
    A branch that fails to parse is recorded and ignored, never fatal — the
    search only fails when *no* branch produced a plan, and then the linear
    strategy's behaviour (empty plan) is the honest answer.
    """

    name = "tot"

    def __init__(self, *, branches: int = 3, judge: bool = False,
                 judge_margin: float = 12.0) -> None:
        self.branches = max(1, int(branches))
        self.judge = bool(judge)
        self.judge_margin = float(judge_margin)

    def plan(
        self,
        goal: str,
        slice_text: str,
        *,
        ask: AskFn,
        parse: Callable[[str], list[str]],
        budget_remaining: int = 0,
    ) -> PlanOutcome:
        from .prompt import build_phase_prompt

        base = build_phase_prompt("plan", goal=goal, slice_text=slice_text)
        candidates: list[dict[str, Any]] = []

        for branch in range(self.branches):
            temperature = _CANDIDATE_TEMPS[branch % len(_CANDIDATE_TEMPS)]
            prompt = base.render()
            if self.branches > 1:
                prompt += (
                    f"\n\n(Branch {branch + 1} of {self.branches}: propose an "
                    "independent decomposition. Prefer a different ordering or "
                    "granularity than the obvious one.)"
                )
            try:
                text = ask("plan", prompt, temperature=temperature)
                steps = parse(text)
            except Exception as exc:  # noqa: BLE001 - one branch must not sink the search
                candidates.append({
                    "branch": branch,
                    "temperature": temperature,
                    "steps": [],
                    "score": 0.0,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                })
                continue
            candidates.append({
                "branch": branch,
                "temperature": temperature,
                "steps": steps,
                "score": score_steps(steps, budget_remaining=budget_remaining),
            })

        parsed = [c for c in candidates if c["steps"]]
        if not parsed:
            return PlanOutcome(steps=[], strategy=self.name, candidates=candidates)

        parsed.sort(key=lambda c: (-float(c["score"]), int(c["branch"])))
        best = parsed[0]
        judge_used = False

        if self.judge and len(parsed) > 1:
            runner_up = parsed[1]
            close = (float(best["score"]) - float(runner_up["score"])) <= self.judge_margin
            if close:
                winner = self._arbitrate(goal, parsed, ask=ask)
                if winner is not None:
                    best = winner
                    judge_used = True

        return PlanOutcome(
            steps=list(best["steps"]),
            strategy=self.name,
            score=float(best["score"]),
            candidates=candidates,
            judge_used=judge_used,
        )

    def _arbitrate(
        self, goal: str, parsed: Sequence[dict[str, Any]], *, ask: AskFn
    ) -> dict[str, Any] | None:
        """One cheap call: "which of these plans is best?" — fail-safe to None."""
        listing = []
        for i, cand in enumerate(parsed, start=1):
            body = "\n".join(f"   {n}. {s}" for n, s in enumerate(cand["steps"], start=1))
            listing.append(f"Plan {i}:\n{body}")
        prompt = (
            "Ты арбитр планов. Дана задача и несколько планов её решения.\n\n"
            f"Задача: {goal}\n\n" + "\n\n".join(listing) +
            "\n\nВерни ТОЛЬКО JSON: {\"best\": <номер плана>, \"reason\": \"<кратко>\"}"
        )
        try:
            text = ask("plan", prompt, temperature=0.0)
        except Exception:  # noqa: BLE001 - the heuristic winner already stands
            return None
        index = _parse_choice(text, upper=len(parsed))
        return parsed[index] if index is not None else None


def _parse_choice(text: str, *, upper: int) -> int | None:
    """Extract a 1-based choice from a judge answer (JSON first, then prose)."""
    raw = (text or "").strip()
    if not raw:
        return None
    for blob in _json_blobs(raw):
        value = blob.get("best", blob.get("plan", blob.get("choice")))
        index = _as_index(value, upper=upper)
        if index is not None:
            return index
    match = re.search(r"(?:plan|план)\s*#?\s*(\d{1,2})", raw, re.IGNORECASE)
    if match:
        return _as_index(match.group(1), upper=upper)
    match = re.search(r"\b(\d{1,2})\b", raw)
    if match:
        return _as_index(match.group(1), upper=upper)
    return None


def _as_index(value: Any, *, upper: int) -> int | None:
    try:
        index = int(str(value).strip().strip('"').rstrip("."))
    except (TypeError, ValueError):
        return None
    if 1 <= index <= upper:
        return index - 1
    return None


def _json_blobs(text: str) -> Iterable[dict[str, Any]]:
    """Yield every decodable JSON object embedded in *text*."""
    for match in re.finditer(r"\{[^{}]*\}", text):
        try:
            blob = json.loads(match.group(0))
        except (ValueError, TypeError):
            continue
        if isinstance(blob, dict):
            yield blob


def score_steps(steps: Sequence[str], *, budget_remaining: int = 0) -> float:
    """Zero-token structural score in ``[0, 100]`` for one candidate plan.

    The signals are deliberately lexical — this must cost nothing and must be
    identical across runs, otherwise the tree search would be comparing plans by
    the mood of a model rather than by their shape:

    * **specificity** (35) — does a step name a file, a symbol or a test target?
      A plan of vague verbs ("improve the code") cannot be executed;
    * **verification** (25) — at least one step that runs/inspects something
      after the edit.  A plan with no verification step is a plan that hopes;
    * **length fit** (25) — peaks at :data:`_IDEAL_STEPS`, zero past
      :data:`_MAX_STEPS`;
    * **novelty** (15) — repeated steps signal a hallicunated loop.
    """
    cleaned = [s.strip() for s in steps if str(s).strip()]
    if not cleaned:
        return 0.0

    joined = " ".join(cleaned).lower()
    every_step = [s.lower() for s in cleaned]

    specific = [s for s in every_step if _SPECIFIC_RE.search(s)]
    specificity = min(35.0, 12.0 * len(specific) + (8.0 if specific else 0.0))

    verify = [s for s in every_step if _VERIFY_RE.search(s)]
    verification = 25.0 if verify else 0.0

    n = len(cleaned)
    if n == _IDEAL_STEPS:
        length = 25.0
    elif n < _IDEAL_STEPS:
        length = max(0.0, 25.0 - 9.0 * (_IDEAL_STEPS - n))
    else:
        length = max(0.0, 25.0 * (1.0 - (n - _IDEAL_STEPS) / max(1, _MAX_STEPS - _IDEAL_STEPS)))

    seen: set[str] = set()
    dupes = 0
    for step in every_step:
        key = re.sub(r"\W+", " ", step).strip()
        if key in seen:
            dupes += 1
        seen.add(key)
    novelty = max(0.0, 15.0 - 7.5 * dupes)

    score = specificity + verification + length + novelty

    if budget_remaining > 0:
        estimate = 60 + 90 * n  # cheap-model prompt + completion, tokens
        if estimate > budget_remaining:
            score *= max(0.4, budget_remaining / estimate)

    if not joined:  # pragma: no cover - guarded by ``cleaned``
        return 0.0
    return round(min(100.0, score), 3)


_SPECIFIC_RE = re.compile(
    r"(\.py\b|\.ts\b|\.js\b|\.go\b|\.rs\b|/[\w.-]+|`[^`]+`|\b[a-z]+_[a-z_]+\b|\b[A-Z][a-z]+[A-Z]\w+\b)"
)
_VERIFY_RE = re.compile(
    r"(test|pytest|run |lint|ruff|check|verify|assert|grep|diff|compile|build|"
    r"тест|провер|запуст|линт|собра)"
)


# ══════════════════════════════════════════════════════════════════════════
# review strategies
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class ReviewOutcome:
    """A review verdict plus why it was reached."""

    verdict: str = "ok"  # "ok" | "reject"
    issues: list[str] = field(default_factory=list)
    strategy: str = "none"
    llm_used: bool = False
    principles: list[dict[str, Any]] = field(default_factory=list)
    #: True when the verdict came from a model rather than a deterministic
    #: check.  An advisory rejection is recorded and fed back to the coder, but
    #: it does not by itself fail the run — see ``_node_review``.
    advisory: bool = False

    @property
    def rejected(self) -> bool:
        return self.verdict == "reject"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "strategy": self.strategy,
            "issues": list(self.issues),
            "llm_used": self.llm_used,
            "principles": list(self.principles),
            "advisory": self.advisory,
        }


class ReviewStrategy(Protocol):
    """Adjudicate a diff beyond the deterministic gate."""

    name: str

    def review(
        self,
        diff: str,
        *,
        goal: str,
        ask: AskFn,
        slice_obj: Any = None,
    ) -> ReviewOutcome: ...


class NoReview:
    """Accept everything.  The V2 default: the validator is the only gate."""

    name = "none"

    def review(
        self, diff: str, *, goal: str, ask: AskFn, slice_obj: Any = None
    ) -> ReviewOutcome:
        return ReviewOutcome(verdict="ok", strategy=self.name)


class ConstitutionalReview:
    """The ``agents.constitutional`` principle set as the DAG's review stage.

    Two layers, in cost order:

    1. the constitution's **hard** principles run as pure functions over the
       diff text — zero tokens, deterministic, no model needed;
    2. the **soft** principles (those without a builtin check) are judged by one
       cheap-model call.  A parse failure is a rejection, never an approval.

    ``llm=False`` keeps layer 1 only, which is the right setting for a
    deployment that wants a hard, reproducible gate and no extra spend.
    """

    name = "constitutional"

    def __init__(self, *, llm: bool = True, constitution: Any = None) -> None:
        self.llm = bool(llm)
        self._constitution = constitution

    # -- the constitution object is loaded lazily so importing the DAG stays cheap
    @property
    def constitution(self) -> Any:
        if self._constitution is None:
            from gigaforge.agents.constitutional import load_constitution

            self._constitution = load_constitution()
        return self._constitution

    def review(
        self, diff: str, *, goal: str, ask: AskFn, slice_obj: Any = None
    ) -> ReviewOutcome:
        principles: list[dict[str, Any]] = []
        issues: list[str] = []

        try:
            constitution = self.constitution
        except Exception as exc:  # noqa: BLE001 - a broken constitution must not approve
            return ReviewOutcome(
                verdict="reject",
                issues=[f"constitution unavailable: {type(exc).__name__}: {exc}"],
                strategy=self.name,
            )

        proxy_slice = _slice_proxy(slice_obj)

        try:
            checks = constitution.deterministic_check(diff, proxy_slice)
        except Exception as exc:  # noqa: BLE001 - fail safe
            return ReviewOutcome(
                verdict="reject",
                issues=[f"deterministic checks failed: {type(exc).__name__}: {exc}"],
                strategy=self.name,
            )

        for principle, result in checks:
            violated = bool(getattr(result, "violated", False))
            principles.append({
                "id": getattr(principle, "id", "?"),
                "severity": getattr(principle, "severity", "warn"),
                "violated": violated,
                "detail": str(getattr(result, "detail", ""))[:400],
            })
            if violated:
                issues.append(f"[{getattr(principle, 'id', '?')}] "
                              f"{getattr(result, 'detail', 'violation')}")

        if issues:
            return ReviewOutcome(
                verdict="reject", issues=issues, strategy=self.name, principles=principles
            )

        soft = []
        try:
            soft = list(constitution.soft_principles())
        except Exception:  # noqa: BLE001
            soft = []

        if not soft or not self.llm:
            return ReviewOutcome(
                verdict="ok", strategy=self.name, principles=principles,
                llm_used=False,
            )

        return self._soft_pass(goal, diff, soft, principles, ask=ask)

    def _soft_pass(
        self,
        goal: str,
        diff: str,
        soft: Sequence[Any],
        principles: list[dict[str, Any]],
        *,
        ask: AskFn,
    ) -> ReviewOutcome:
        lines = "\n".join(
            f"- [{getattr(p, 'severity', 'warn')}] {getattr(p, 'id', '?')}: "
            f"{getattr(p, 'description', '')}"
            for p in soft
        )
        prompt = (
            "Ты конституционный ревьюер. Проверь diff против принципов ниже.\n\n"
            f"Цель задачи: {goal}\n\nПринципы:\n{lines}\n\n"
            f"Diff:\n{diff[:12000]}\n\n"
            'Верни ТОЛЬКО JSON: {"verdict": "ok"|"reject", "issues": ["..."]}'
        )
        try:
            text = ask("review", prompt, temperature=0.0)
        except Exception as exc:  # noqa: BLE001 - fail safe
            return ReviewOutcome(
                verdict="reject",
                issues=[f"constitutional judge failed: {type(exc).__name__}: {exc}"],
                strategy=self.name,
                llm_used=True,
                principles=principles,
            )

        verdict, issues = _parse_review_json(text)
        if verdict is None:
            return ReviewOutcome(
                verdict="reject",
                issues=["constitutional reviewer returned an unparseable verdict"],
                strategy=self.name,
                llm_used=True,
                principles=principles,
            )
        return ReviewOutcome(
            verdict=verdict, issues=issues, strategy=self.name, llm_used=True,
            principles=principles, advisory=True,
        )


class LLMReview:
    """A plain cheap-model review — the ``agents.reviewer`` algorithm, one call.

    Kept alongside :class:`ConstitutionalReview` because they answer different
    questions: the constitution asks "does this break a stated rule?", this asks
    "does this diff actually solve the goal?".
    """

    name = "llm"

    def review(
        self, diff: str, *, goal: str, ask: AskFn, slice_obj: Any = None
    ) -> ReviewOutcome:
        prompt = (
            "Ты ревьюер изменений. Реши, решает ли diff поставленную задачу и не "
            "ломает ли он существующее поведение.\n\n"
            f"Задача: {goal}\n\nDiff:\n{diff[:12000]}\n\n"
            'Верни ТОЛЬКО JSON: {"verdict": "ok"|"reject", "issues": ["..."]}'
        )
        try:
            text = ask("review", prompt, temperature=0.0)
        except Exception as exc:  # noqa: BLE001 - fail safe
            return ReviewOutcome(
                verdict="reject",
                issues=[f"reviewer failed: {type(exc).__name__}: {exc}"],
                strategy=self.name,
                llm_used=True,
            )
        verdict, issues = _parse_review_json(text)
        if verdict is None:
            return ReviewOutcome(
                verdict="reject",
                issues=["reviewer returned an unparseable verdict"],
                strategy=self.name,
                llm_used=True,
            )
        return ReviewOutcome(
            verdict=verdict, issues=issues, strategy=self.name, llm_used=True,
            advisory=True,
        )


def _parse_review_json(text: str) -> tuple[str | None, list[str]]:
    """Extract ``(verdict, issues)`` from a reviewer answer. ``None`` verdict = garbage."""
    raw = (text or "").strip()
    if not raw:
        return None, []
    for blob in _json_blobs(raw):
        value = blob.get("verdict", blob.get("decision", blob.get("result")))
        issues = blob.get("issues") or blob.get("problems") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        normalised = _normalise_verdict(value)
        if normalised:
            return normalised, [str(i)[:400] for i in issues]
    bare = _normalise_verdict(raw[:64])
    if bare:
        return bare, []
    return None, []


def _normalise_verdict(value: Any) -> str | None:
    token = str(value or "").strip().strip('"').lower()
    if token in {"ok", "approve", "approved", "accept", "accepted", "pass", "lgtm", "yes"}:
        return "ok"
    if token in {"reject", "rejected", "fail", "failed", "deny", "no", "block"}:
        return "reject"
    return None


def _slice_proxy(slice_obj: Any) -> Any:
    """Give the constitution a slice-shaped object even when there is no index."""
    if slice_obj is not None and hasattr(slice_obj, "to_prompt_dict"):
        return slice_obj

    class _NullSlice:
        files: list[str] = []

        def to_prompt_dict(self) -> dict[str, Any]:
            return {"files": [], "symbols": [], "note": "no context slice (no index wired in)"}

    return _NullSlice()


# ══════════════════════════════════════════════════════════════════════════
# selection
# ══════════════════════════════════════════════════════════════════════════
def _step_signature(step: Any) -> str:
    """The ``tool:key=value`` signature the deterministic-output guard reports.

    Kept next to the filter that uses it so the two cannot drift: the guard
    emits ``f"{tool}:{key}={value}"`` and this must produce the identical
    string, or the filter silently stops matching.
    """
    tool = str(getattr(step, "tool", "") or "").strip()
    args = getattr(step, "args", None) or {}
    if not isinstance(args, dict):
        return tool
    for key, value in args.items():
        if isinstance(value, str):
            return f"{tool}:{key}={value.strip()}"
    return tool


def _describe_plan_step(step: Any) -> str:
    """Render one ReAct ``PlanStep`` as the DAG's plan line.

    ``PlanStep`` carries ``tool`` + ``args`` (a pydantic model), not a
    ``description`` string — the previous implementation read
    ``step.description`` and raised ``AttributeError`` on the first real plan.
    The DAG's plan is a list of human-readable lines, so the tool name plus its
    salient argument is the right rendering.
    """
    tool = str(getattr(step, "tool", "") or "").strip()
    if not tool:
        # A duck-typed step (a test stub, or a future engine) may carry a
        # ready-made description instead of tool/args.
        return str(getattr(step, "description", "") or "").strip()
    args = getattr(step, "args", None) or {}
    if isinstance(args, dict) and args:
        # Prefer the argument that names the target; fall back to the first.
        for key in ("name", "path", "file", "query", "value"):
            if key in args and str(args[key]).strip():
                return f"{tool}: {args[key]}"
        first_key = next(iter(args))
        return f"{tool}: {args[first_key]}"
    return tool


class ReActPlan:
    """Plan by *exploring* the repository, not by guessing from a slice.
    This is the strategy that makes the LangGraph ReAct layer reachable from the
    product.  Before it existed, ``run_react_graph`` and ``ReActRunner`` were
    only ever constructed by tests: the DAG's ``plan`` node always called a
    one-shot prompt, so the whole observe/act/observe engine — and the four
    guards wired into it — could not run in a real pipeline.

    The strategy is a thin adapter: it builds a :class:`ReActRunner` from the
    pipeline's own collaborators (client, router, budgets, tracer, sandbox) and
    runs it through :func:`gigaforge.agents.engine.run_react`, which dispatches
    to the classic loop or the LangGraph graph according to
    ``GIGAFORGE_REACT_ENGINE``.  Both engines return the same
    :class:`~gigaforge.agents.react.ReActResult`, so the DAG sees one shape.

    Opt-in, because every ReAct step is a model call:

        GIGAFORGE_PLAN_STRATEGY=react

    When the runner cannot be built (no sandbox, no index, no client) the
    strategy degrades to :class:`LinearPlan` rather than failing the run — a
    missing exploration capability must not make the harness unrunnable.
    """

    name = "react"

    def __init__(
        self,
        *,
        runner_factory: Callable[..., Any] | None = None,
        max_steps: int = 6,
    ) -> None:
        self._runner_factory = runner_factory
        self._max_steps = max_steps
        #: Set by the pipeline for the duration of one run, so the runner's
        #: ``TaskState`` carries the DAG's own ``run_id`` instead of a
        #: synthetic one.  A separate id would split one run's trace in two.
        self._run_id = ""
        #: The runner built for the current ``plan`` call, kept so the plan
        #: guard can read its real tool registry.
        self._last_runner: Any = None

    def bind_run(self, run_id: str) -> None:
        """Attach the current run id (called by the pipeline before ``plan``)."""
        self._run_id = str(run_id or "")

    def _build_runner(self, goal: str) -> Any:
        """Call the factory, passing the run id and step cap when it takes them.

        The factory is a plain callable so a test can inject a stub; a stub that
        only accepts ``goal`` must keep working, hence the ``TypeError`` retry.
        """
        if self._runner_factory is None:
            return None
        try:
            return self._runner_factory(
                goal, run_id=self._run_id, max_steps=self._max_steps
            )
        except TypeError:
            return self._runner_factory(goal)

    def _linear(
        self,
        goal: str,
        slice_text: str,
        *,
        ask: AskFn,
        parse: Callable[[str], list[str]],
        strategy: str = "react->linear",
    ) -> PlanOutcome:
        from .prompt import build_phase_prompt

        prompt = build_phase_prompt("plan", goal=goal, slice_text=slice_text).render()
        steps = parse(ask("plan", prompt))
        return PlanOutcome(steps=steps, strategy=strategy, score=score_steps(steps))

    def plan(
        self,
        goal: str,
        slice_text: str,
        *,
        ask: AskFn,
        parse: Callable[[str], list[str]],
        budget_remaining: int = 0,
    ) -> PlanOutcome:
        runner = self._build_runner(goal)
        if runner is None:
            # No exploration capability wired in: fall back to the one-shot plan.
            return self._linear(goal, slice_text, ask=ask, parse=parse)
        # Kept so the plan guard can read the runner's real tool registry.
        self._last_runner = runner

        from gigaforge.agents.engine import run_react

        try:
            result = run_react(
                runner,
                goal,
                runner.state,
                ctx=runner.ctx,
                thread_id=self._run_id or "react-plan",
            )
        except Exception as exc:  # noqa: BLE001 - exploration is best-effort
            log.warning("ReAct planning failed (%s); falling back to linear", exc)
            return self._linear(goal, slice_text, ask=ask, parse=parse)

        steps, verdict = self._guarded_steps(result)
        if not steps:
            # The loop explored but never produced a plan: the slice-based plan
            # is still better than an empty one.
            return self._linear(goal, slice_text, ask=ask, parse=parse)
        return PlanOutcome(
            steps=steps,
            strategy=self.name,
            score=score_steps(steps),
            localization=dict(getattr(result, "localization", None) or {}),
            structured=list(getattr(result, "plan", None) or []),
            candidates=[{
                "branch": "react",
                "steps": steps,
                "score": score_steps(steps),
                "finish_reason": getattr(result, "finish_reason", ""),
                "llm_calls": getattr(result, "llm_calls", 0),
                "plan_guard": verdict.to_dict(),
            }],
        )

    def _known_tools(self) -> set[str]:
        """Tools the pipeline can actually run, for the plan guard.

        Two vocabularies meet here and they are **not** the same set:

        * ``react_tools`` — what the ReAct registry can execute (``grep``,
          ``read_structure``, ``run_tests``, ``think``, ``finish``).  A plan
          step naming anything else cannot be run by the episode.
        * ``dag_tools`` — the DAG's own step vocabulary (``coder``, ``tests``,
          ``lint``, ``reviewer``, ``edit_function``).  A plan is *rendered into*
          these, so a step naming one is legitimate even though the ReAct
          registry has never heard of it.

        The previous version unioned the two without saying so, which made the
        guard's "known tools" check meaningless: ``edit_function`` was allowed
        because it is a DAG step, even on a path where the registry could not
        have produced it.  Keeping the sets separate and naming them is what
        makes the check mean something again.

        The union is still returned, but only because the plan is *rendered
        into* DAG steps before it runs — the guard's job is to reject a step
        that cannot be executed at all, and a DAG step can.  What the guard
        must not do is accept an untargeted ``edit_function``, and that is
        enforced by ``_has_target``, not by this set.
        """
        dag_tools = {"tests", "lint", "reviewer", "coder", "edit_function"}
        runner = getattr(self, "_last_runner", None)
        registry = getattr(runner, "registry", None) if runner is not None else None
        names = getattr(registry, "names", None)
        if callable(names):
            try:
                found = {str(n) for n in names()}
            except Exception:  # noqa: BLE001 - a broken registry is not fatal
                found = set()
            if found:
                return found | dag_tools
        return dag_tools | {"read_structure", "grep", "run_tests", "think"}

    def _guarded_steps(self, result: Any) -> tuple[list[str], Any]:
        """Render the episode's plan into DAG steps, after the plan guard.

        This is the single place the guard runs, and both planning entry points
        (``plan`` and ``plan_under_harness``) go through it.  That matters: the
        guard was originally called only from ``plan``, while the pipeline
        prefers ``plan_under_harness`` — so the one path the product actually
        used was the one path the guard did not cover, and a live run executed
        ``edit_function: react.py`` with no symbol and no verification.

        The guard is deterministic and model-free, so it costs nothing and
        cannot drift.  Returns ``(steps, verdict)``; ``steps`` is empty when the
        guard rejected every step, which the caller turns into the linear
        fallback.
        """
        from gigaforge.harness.plan_guard import validate_plan

        raw = getattr(result, "plan", None) or []
        verdict = validate_plan(raw, known_tools=self._known_tools())
        if verdict.changed:
            log.info("plan guard adjusted the ReAct plan: %s", verdict.to_dict())
        # DeterministicOutput (ported from deepagents-gigachat): a plan may not
        # assert a derived value it never computed.  This is a separate rule
        # from the structural guard above — that one asks "is this step
        # actionable", this one asks "is this value earned".
        runner = getattr(self, "_last_runner", None)
        guards = getattr(runner, "guards", None) if runner is not None else None
        if guards is not None and hasattr(guards, "check_plan"):
            det = guards.check_plan(verdict.steps or [])
            if not det.ok:
                log.warning("deterministic-output guard: %s", det.to_dict())
                verdict.dropped_untargeted.extend(det.blocked)
                # Drop only the steps the guard actually named.  The previous
                # filter compared ``f"{tool}:"`` against the blocked strings, so
                # one blocked ``edit_function:name=5`` removed *every*
                # ``edit_function`` in the plan — a single invented value
                # destroyed the legitimate edits around it.  Match the full
                # signature instead.
                blocked = {str(b) for b in det.blocked}
                verdict.steps = [
                    step
                    for step in (verdict.steps or [])
                    if _step_signature(step) not in blocked
                ]
        steps = [_describe_plan_step(step) for step in (verdict.steps or [])]
        if not steps:
            # The guard rejected every step.  That is the right verdict — a
            # plan of ``edit_function`` with no target cannot be executed — but
            # returning nothing throws away everything the episode observed.
            # Measured live: the model grepped, found the files, then emitted
            # ``[{"tool": "edit_function", "args": {}}]`` for a read-only task;
            # the guard dropped it and the run reported zero steps despite
            # having the answer in its trajectory.
            #
            # Salvage from the observations instead.  This is deterministic and
            # model-free, and it is the same recovery the classic loop already
            # applies when a finish payload does not parse.
            trajectory = getattr(result, "trajectory", None)
            if trajectory:
                salvaged = runner.synthesize_plan(trajectory) if runner is not None else []
                steps = [_describe_plan_step(step) for step in salvaged]
                steps = [step for step in steps if step]
                if steps:
                    log.info(
                        "plan guard emptied the plan; salvaged %d step(s) from observations",
                        len(steps),
                    )
        return steps, verdict

    def plan_under_harness(
        self,
        goal: str,
        slice_text: str,
        *,
        ask: AskFn,
        parse: Callable[[str], list[str]],
        policy: Any = None,
    ) -> tuple[PlanOutcome, Any]:
        """Plan through the :class:`~gigaforge.harness.Harness`.

        The harness is what makes a ReAct plan *survivable*: it retries a 429
        with the server's own ``Retry-After``, compacts and retries on a context
        overflow instead of ending the episode, and records a ledger of what the
        exploration cost.

        Without this method the harness would be another inert layer — built,
        tested, and never called by the product.  ``plan()`` keeps its old
        behaviour for callers that do not want a policy; this is the entry point
        the pipeline uses.

        Returns ``(outcome, ledger)``; the ledger is ``None`` when the runner
        could not be built (the linear fallback has no episode to account for).
        """
        from gigaforge.harness import Harness, HarnessPolicy

        runner = self._build_runner(goal)
        if runner is None:
            return self._linear(goal, slice_text, ask=ask, parse=parse), None
        # Kept so the plan guard can read the runner's real tool registry and
        # its guards (the deterministic-output rule lives on the runner).
        self._last_runner = runner

        harness = Harness(
            policy=policy or HarnessPolicy(),
            budgets=getattr(runner, "budgets", None),
        )
        outcome = harness.run(
            runner,
            goal,
            runner.state,
            ctx=runner.ctx,
            run_id=self._run_id or "react-plan",
            # ``engine=""`` — NOT ``"react"``.  The engine argument selects the
            # *implementation*; ``"react"`` is not one of them, so the dispatcher
            # in ``agents/engine.py`` fell through to the classic loop while the
            # ledger recorded ``engine: react``.  Measured: with
            # ``GIGAFORGE_REACT_ENGINE=langgraph`` (the default, and what
            # ``gigaforge stack`` printed) ``run_react_graph`` was called zero
            # times.  An empty string means "resolve it", which is the only
            # value that honours the environment variable.
            engine="",
            # ``react=True`` is what makes the harness dispatch through
            # ``run_react`` — the only function that honours
            # ``GIGAFORGE_REACT_ENGINE``.  Without it the harness called
            # ``runner.run`` directly, so the LangGraph ReAct graph was
            # unreachable on the product path.
            react=True,
        )
        if not outcome.ok:
            log.warning(
                "ReAct planning stopped (%s: %s); falling back to linear",
                outcome.finish_reason,
                outcome.error,
            )
            return self._linear(goal, slice_text, ask=ask, parse=parse), outcome.ledger

        result = outcome.result
        steps, verdict = self._guarded_steps(result)
        if not steps:
            return self._linear(goal, slice_text, ask=ask, parse=parse), outcome.ledger
        if outcome.ledger is not None:
            # Make the plan's fate visible in the run record.  Without this a run
            # that paid for exploration and discarded it is indistinguishable
            # from a successful one in every other column of the ledger.
            outcome.ledger.note_plan(
                used=True,
                steps=len(steps),
                salvaged=bool(getattr(result, "trajectory", None))
                and not bool(getattr(result, "plan", None)),
            )
        return (
            PlanOutcome(
                steps=steps,
                strategy=self.name,
                score=score_steps(steps),
                localization=dict(getattr(result, "localization", None) or {}),
                structured=list(getattr(result, "plan", None) or []),
                written_files=list(getattr(result, "written_files", None) or []),
                candidates=[{
                    "branch": "react",
                    "steps": steps,
                    "score": score_steps(steps),
                    "finish_reason": getattr(result, "finish_reason", ""),
                    "llm_calls": getattr(result, "llm_calls", 0),
                    "compactions": outcome.ledger.compactions if outcome.ledger else 0,
                    "retries": outcome.ledger.retries if outcome.ledger else 0,
                    "plan_guard": verdict.to_dict(),
                }],
            ),
            outcome.ledger,
        )


PLAN_STRATEGIES: dict[str, Callable[[], PlanStrategy]] = {
    "linear": LinearPlan,
    "tot": TreeOfThoughtPlan,
    "react": ReActPlan,
}

REVIEW_STRATEGIES: dict[str, Callable[[], ReviewStrategy]] = {
    "none": NoReview,
    "off": NoReview,
    "constitutional": ConstitutionalReview,
    "llm": LLMReview,
}


def build_plan_strategy(
    name: str | None = None,
    *,
    react_runner_factory: Callable[[], Any] | None = None,
) -> PlanStrategy:
    """Build a plan strategy by name; env ``GIGAFORGE_PLAN_STRATEGY`` wins.

    Unknown names fall back to ``linear`` with a warning — a typo in an env var
    must not make the harness unrunnable.

    ``react_runner_factory`` is only used by :class:`ReActPlan`: the pipeline
    passes a callable that builds a :class:`ReActRunner` from its own
    collaborators, so the strategy never reaches for globals.
    """
    # ``react`` is the default: the merged stack's whole point is that the
    # planner explores before it commits.  A deployment that wants the old
    # one-shot behaviour sets GIGAFORGE_PLAN_STRATEGY=linear explicitly.
    resolved = (name or os.environ.get("GIGAFORGE_PLAN_STRATEGY", "") or "react").strip().lower()
    factory = PLAN_STRATEGIES.get(resolved)
    if factory is None:
        log.warning("unknown plan strategy %r; falling back to react", resolved)
        factory = ReActPlan
    if factory is TreeOfThoughtPlan:
        return TreeOfThoughtPlan(
            branches=_env_int("GIGAFORGE_TOT_MAX_BRANCHES", 3),
            judge=_env_bool("GIGAFORGE_TOT_JUDGE", False),
        )
    if factory is ReActPlan:
        return ReActPlan(
            runner_factory=react_runner_factory,
            max_steps=_env_int("GIGAFORGE_REACT_MAX_STEPS", 6),
        )
    return factory()


def build_review_strategy(name: str | None = None) -> ReviewStrategy:
    """Build a review strategy by name; env ``GIGAFORGE_REVIEW_STRATEGY`` wins."""
    # ``constitutional`` is the default: it is deterministic and costs no extra
    # model call, so a review phase exists without a budget surprise.
    resolved = (name or os.environ.get("GIGAFORGE_REVIEW_STRATEGY", "") or "constitutional").strip().lower()
    factory = REVIEW_STRATEGIES.get(resolved)
    if factory is None:
        log.warning("unknown review strategy %r; falling back to constitutional", resolved)
        factory = ConstitutionalReview
    if factory is ConstitutionalReview:
        return ConstitutionalReview(llm=_env_bool("GIGAFORGE_CONSTITUTION_LLM", True))
    return factory()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

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
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "steps": list(self.steps),
            "score": round(float(self.score), 3),
            "judge_used": self.judge_used,
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
    ) -> Optional[dict[str, Any]]:
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


def _parse_choice(text: str, *, upper: int) -> Optional[int]:
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


def _as_index(value: Any, *, upper: int) -> Optional[int]:
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
            principles=principles,
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
        return ReviewOutcome(verdict=verdict, issues=issues, strategy=self.name, llm_used=True)


def _parse_review_json(text: str) -> tuple[Optional[str], list[str]]:
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


def _normalise_verdict(value: Any) -> Optional[str]:
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
        runner_factory: Optional[Callable[..., Any]] = None,
        max_steps: int = 6,
    ) -> None:
        self._runner_factory = runner_factory
        self._max_steps = max_steps
        #: Set by the pipeline for the duration of one run, so the runner's
        #: ``TaskState`` carries the DAG's own ``run_id`` instead of a
        #: synthetic one.  A separate id would split one run's trace in two.
        self._run_id = ""

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

        steps = [_describe_plan_step(step) for step in (getattr(result, "plan", None) or [])]
        steps = [step for step in steps if step]
        if not steps:
            # The loop explored but never produced a plan: the slice-based plan
            # is still better than an empty one.
            return self._linear(goal, slice_text, ask=ask, parse=parse)
        return PlanOutcome(
            steps=steps,
            strategy=self.name,
            score=score_steps(steps),
            candidates=[{
                "branch": "react",
                "steps": steps,
                "score": score_steps(steps),
                "finish_reason": getattr(result, "finish_reason", ""),
                "llm_calls": getattr(result, "llm_calls", 0),
            }],
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
    react_runner_factory: Optional[Callable[[], Any]] = None,
) -> PlanStrategy:
    """Build a plan strategy by name; env ``GIGAFORGE_PLAN_STRATEGY`` wins.

    Unknown names fall back to ``linear`` with a warning — a typo in an env var
    must not make the harness unrunnable.

    ``react_runner_factory`` is only used by :class:`ReActPlan`: the pipeline
    passes a callable that builds a :class:`ReActRunner` from its own
    collaborators, so the strategy never reaches for globals.
    """
    resolved = (name or os.environ.get("GIGAFORGE_PLAN_STRATEGY", "") or "linear").strip().lower()
    factory = PLAN_STRATEGIES.get(resolved)
    if factory is None:
        log.warning("unknown plan strategy %r; falling back to linear", resolved)
        factory = LinearPlan
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
    resolved = (name or os.environ.get("GIGAFORGE_REVIEW_STRATEGY", "") or "none").strip().lower()
    factory = REVIEW_STRATEGIES.get(resolved)
    if factory is None:
        log.warning("unknown review strategy %r; falling back to none", resolved)
        factory = NoReview
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

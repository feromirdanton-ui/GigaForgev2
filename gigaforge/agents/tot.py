"""Tree-of-Thought planner — K candidate plans, score, pick best.

WHY IT EARNS ITS TOKENS (justification, doctrine-aligned):
    Most L1 planner calls are cheap; the *failure mode* the doctrine cares about
    is silent escalation (PlanEscalation) when ONE Lite call emits a malformed
    plan. Tree-of-Thought spends K× the planner budget ONLY when --tot is set,
    and in exchange: (a) parallel generation yields diverse candidate plans
    (different temperature seeds), so a single bad model output no longer kills
    the task; (b) the cheap L0-scoring (tool diversity, length, repetition,
    budget-fit) deterministically picks the structurally-best candidate; (c)
    an optional L1 "judge" Lite call breaks ties when scores are close. The
    upshot: more Lite tokens spent, but a measurably lower PlanEscalation rate
    on ambiguous tasks — the doctrine's "context is budget" is preserved (every
    call is budget-checked; the K cap is hard; the judge is opt-in via env
    GIGAFORGE_TOT_JUDGE=1).

Doctrine:
- Context is budget. Every LLM call (K candidates + 1 optional judge) is
  pre-budget-checked via :meth:`BudgetTracker.check`; commits record actuals.
- Diff is contract. The ToT planner returns the SAME shape as
  :class:`PlannerAgent` — a ``list[PlanStep]``. The downstream pipeline is
  unchanged; only the SEARCH around the planner is richer.
- No infinite loops. ``max_branches`` (default 3, env
  ``GIGAFORGE_TOT_MAX_BRANCHES``) is a hard cap. Each branch is ONE LLM call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from gigaforge.agents.planner import (
    _PLANNER_PROMPT,  # type: ignore[attr-defined]
    ALLOWED_TOOLS,  # noqa: F401 — re-exported for symmetry with PlannerAgent
    PlanEscalation,
    PlannerAgent,
    json_repair,
    parse_plan_json,
)
from gigaforge.client.errors import BudgetExceeded, GigaForgeError
from gigaforge.client.giga_client import ChatResult, GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.router import ModelRouter
from gigaforge.core.task_state import PlanStep, TaskState
from gigaforge.core.trace import Tracer

log = logging.getLogger(__name__)

__all__ = [
    "ToTPlanner",
    "CandidatePlan",
    "ToTResult",
    "score_plan_heuristic",
    "DEFAULT_TOT_MAX_BRANCHES",
]

# Hard cap on the number of candidate plans generated in parallel. Each is ONE
# Lite LLM call. Env-overridable; default 3 = the sweet spot for diversity vs
# token cost (research on self-consistency shows diminishing returns past K=5).
DEFAULT_TOT_MAX_BRANCHES = 3

# Temps used to encourage diversity across the K candidates. Indexing wraps
# around if K > len(temps). Higher temps -> more exploration; capped at 0.7 to
# stay within Lite's reliability envelope for structured JSON.
_CANDIDATE_TEMPS = (0.1, 0.4, 0.7, 0.5, 0.3)

# Max steps in a "well-formed" plan; longer plans are penalised by the
# heuristic (they tend to over-decompose the task and run over budget).
_IDEAL_PLAN_LEN = 3
_MAX_PLAN_LEN_PENALTY = 8

# When the top-2 candidate heuristic scores are within this margin, the
# optional L1 judge Lite call is invoked to break the tie (if enabled).
_TIE_MARGIN = 5.0


class CandidatePlan:
    """A candidate plan from one branch of the ToT search.

    Carries the parsed ``PlanStep`` list, the raw model text, the LLM token
    usage, and the heuristic score (populated by :func:`score_plan_heuristic`).
    """

    __slots__ = (
        "steps",
        "raw",
        "tokens_in",
        "tokens_out",
        "score",
        "branch",
        "temperature",
        "parse_error",
    )

    def __init__(
        self,
        *,
        branch: int,
        temperature: float,
        steps: list[PlanStep] | None = None,
        raw: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        score: float = 0.0,
        parse_error: str | None = None,
    ) -> None:
        self.branch = branch
        self.temperature = temperature
        self.steps = steps
        self.raw = raw
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.score = score
        self.parse_error = parse_error

    @property
    def ok(self) -> bool:
        return self.steps is not None and self.parse_error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "temperature": self.temperature,
            "ok": self.ok,
            "n_steps": len(self.steps) if self.steps is not None else 0,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "score": round(self.score, 2),
            "parse_error": self.parse_error,
        }


class ToTResult:
    """The outcome of a :meth:`ToTPlanner.plan` call.

    ``plan`` is the chosen list of :class:`PlanStep`; ``candidates`` is the
    full list of candidates (parsed or not, for tracing); ``judge`` is the
    judge's reasoning dict (or None if the judge wasn't invoked); ``llm_calls``
    is the total number of LLM calls made (K + optional 1 judge).
    """

    __slots__ = ("plan", "candidates", "judge", "llm_calls", "chosen_branch")

    def __init__(
        self,
        *,
        plan: list[PlanStep],
        candidates: list[CandidatePlan],
        judge: dict[str, Any] | None,
        llm_calls: int,
        chosen_branch: int,
    ) -> None:
        self.plan = plan
        self.candidates = candidates
        self.judge = judge
        self.llm_calls = llm_calls
        self.chosen_branch = chosen_branch

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_steps": len(self.plan),
            "candidates": [c.to_dict() for c in self.candidates],
            "judge": self.judge,
            "llm_calls": self.llm_calls,
            "chosen_branch": self.chosen_branch,
        }


# --- heuristic scoring ------------------------------------------------------


def score_plan_heuristic(steps: list[PlanStep], *, budget_remaining: int = 0) -> float:
    """Cheap L0 scorer — 0 LLM calls.

    Returns a float in [0, 100]. Higher = structurally better plan.

    The score is a weighted combination of:
      - **Tool diversity** (40 pts max): a plan that uses more *distinct* tools
        (edit_function + run_tests) is preferred over one that uses only one.
        A plan with only ``edit_function`` and no test step is suspicious
        (the pipeline will run tests, but an explicit ``run_tests`` step is a
        stronger signal the planner thought about verification).
      - **Plan length** (30 pts max): peak at ``_IDEAL_PLAN_LEN`` (3), taper off
        linearly; plans longer than ``_MAX_PLAN_LEN_PENALTY`` get 0. Too short
        (<2) also penalised — single-step plans rarely edit + verify.
      - **Repetition penalty** (10 pts): subtract for duplicate tool+args pairs
        (the planner hallucinated a repeated step).
      - **Budget fit** (20 pts): if ``budget_remaining`` is passed, a plan whose
        estimated cost fits within the remaining budget scores higher.
    """
    if not steps:
        return 0.0

    # 1. Tool diversity.
    distinct_tools = {s.tool for s in steps}
    # 4-point scale: 1 distinct tool = 10, 2 = 25, 3 = 35, 4+ = 40.
    diversity = {1: 10.0, 2: 25.0, 3: 35.0}.get(len(distinct_tools), 40.0)

    # 2. Plan length — peak at _IDEAL_PLAN_LEN.
    n = len(steps)
    if n == _IDEAL_PLAN_LEN:
        length_score = 30.0
    elif n < _IDEAL_PLAN_LEN:
        length_score = max(0.0, 30.0 - 15.0 * (_IDEAL_PLAN_LEN - n))
    else:
        # Taper off linearly past the ideal; 0 at >= _MAX_PLAN_LEN_PENALTY.
        over = n - _IDEAL_PLAN_LEN
        span = max(1, _MAX_PLAN_LEN_PENALTY - _IDEAL_PLAN_LEN)
        length_score = max(0.0, 30.0 * (1.0 - over / span))

    # 3. Repetition penalty.
    seen: set[tuple[str, str]] = set()
    dupes = 0
    for s in steps:
        key = (s.tool, json.dumps(s.args, sort_keys=True, ensure_ascii=False))
        if key in seen:
            dupes += 1
        seen.add(key)
    repetition_score = max(0.0, 10.0 - 5.0 * dupes)

    # 4. Budget fit.
    budget_score = 0.0
    if budget_remaining > 0:
        # Crude est: each step ~ 200 tokens out (Lite), 50 in (overhead).
        est_cost = n * 250
        if est_cost <= budget_remaining:
            budget_score = 20.0
        else:
            # Linear taper; 0 at 2× budget_remaining.
            over_ratio = (est_cost - budget_remaining) / max(1, budget_remaining)
            budget_score = max(0.0, 20.0 * (1.0 - over_ratio / 2.0))

    return diversity + length_score + repetition_score + budget_score


# --- the planner ------------------------------------------------------------


class ToTPlanner:
    """L1 Tree-of-Thought planner.

    Generates K candidate plans in parallel via the L1 Lite model with varied
    temperatures, scores each with :func:`score_plan_heuristic`, optionally
    invokes an L1 Lite "judge" to break ties, and returns the best plan.

    Public API mirrors :class:`PlannerAgent.plan` so the pipeline can swap it
    in via ``--tot``.
    """

    def __init__(
        self,
        client: GigaClient,
        router: ModelRouter,
        budgets: BudgetTracker,
        tracer: Tracer,
        *,
        settings: Settings | None = None,
        max_branches: int | None = None,
        judge_enabled: bool | None = None,
    ) -> None:
        self._client = client
        self._router = router
        self._budgets = budgets
        self._tracer = tracer
        self._s = settings or get_settings()
        # Resolve the K cap. Precedence: ctor arg > env > default.
        if max_branches is not None:
            self._max_branches = max(1, int(max_branches))
        else:
            env_v = os.environ.get("GIGAFORGE_TOT_MAX_BRANCHES", "")
            try:
                self._max_branches = max(1, int(env_v)) if env_v else DEFAULT_TOT_MAX_BRANCHES
            except ValueError:
                self._max_branches = DEFAULT_TOT_MAX_BRANCHES
        # Resolve the judge flag. Precedence: ctor arg > env > default off.
        if judge_enabled is not None:
            self._judge_enabled = bool(judge_enabled)
        else:
            self._judge_enabled = os.environ.get(
                "GIGAFORGE_TOT_JUDGE", ""
            ).strip().lower() in {"1", "true", "yes", "on"}

    @property
    def max_branches(self) -> int:
        return self._max_branches

    @property
    def judge_enabled(self) -> bool:
        return self._judge_enabled

    async def plan(self, task: str, state: TaskState) -> list[PlanStep]:
        """Run the K-branch search + scoring + optional judge; return the plan.

        Raises :class:`PlanEscalation` if ALL branches fail to parse, or if
        the planner budget is exhausted (the underlying :meth:`BudgetTracker.check`
        raises :class:`BudgetExceeded`).
        """
        # Compose the shared user-body (identical to PlannerAgent).
        prefix = state.serialize_prefix()
        base_user = f"{_PLANNER_PROMPT}\n\nЗадача: {task}"
        messages_template: list[dict[str, Any]] = [
            {"role": "system", "content": json.dumps(prefix, ensure_ascii=False)},
            {"role": "user", "content": base_user},
        ]

        model = self._router.route("plan")  # Lite
        max_tokens = int(self._s.budget_plan_out)
        budget_remaining = self._budgets.remaining("plan")

        # --- Step 1: K parallel candidate generations ---------------------
        # Each branch is ONE Lite LLM call. asyncio.gather with
        # return_exceptions=True so a single branch's BudgetExceeded doesn't
        # kill the others — we want as many candidates as the budget allows.
        tasks = [
            self._gen_one(
                branch=i,
                temperature=_CANDIDATE_TEMPS[i % len(_CANDIDATE_TEMPS)],
                model=model,
                max_tokens=max_tokens,
                messages_template=messages_template,
                task_id=state.task_id,
            )
            for i in range(self._max_branches)
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        candidates: list[CandidatePlan] = []
        for i, res in enumerate(raw_results):
            if isinstance(res, BaseException):
                # BudgetExceeded in one branch — record as a failed candidate
                # but continue (we still want the others). If ALL branches
                # raised BudgetExceeded, we re-raise the first one.
                cand = CandidatePlan(
                    branch=i,
                    temperature=_CANDIDATE_TEMPS[i % len(_CANDIDATE_TEMPS)],
                    parse_error=f"exception:{res.__class__.__name__}: {res}",
                )
                candidates.append(cand)
                continue
            candidates.append(res)

        # --- Step 2: score each parsed candidate --------------------------
        parsed = [c for c in candidates if c.ok and c.steps]
        for c in parsed:
            assert c.steps is not None  # narrowed by .ok
            c.score = score_plan_heuristic(c.steps, budget_remaining=budget_remaining)

        # --- Step 3: pick the best, or escalate if all failed ------------
        if not parsed:
            # If all branches raised BudgetExceeded (or otherwise failed),
            # surface that — don't pretend we have a plan.
            budget_excs = [
                c for c in candidates
                if c.parse_error and "BudgetExceeded" in (c.parse_error or "")
            ]
            if budget_excs:
                # Propagate the FIRST BudgetExceeded — STOP semantics.
                raise BudgetExceeded(
                    f"ToT planner: all {len(candidates)} branches failed budget check"
                )
            # Otherwise all branches returned unparseable JSON.
            self._tracer.trace(
                state.task_id,
                step="tot_escalation",
                kind="decision",
                detail={
                    "reason": "all ToT branches returned unparseable JSON",
                    "candidates": [c.to_dict() for c in candidates],
                },
            )
            raise PlanEscalation(
                f"ToT planner: all {len(candidates)} branches failed to parse; "
                f"errors: {[c.parse_error for c in candidates]}"
            )

        # Sort by score DESC, branch ASC for determinism on ties.
        parsed.sort(key=lambda c: (-c.score, c.branch))
        best = parsed[0]
        runner_up = parsed[1] if len(parsed) > 1 else None

        # --- Step 4: optional judge to break ties -------------------------
        judge_dict: dict[str, Any] | None = None
        chosen = best
        if (
            self._judge_enabled
            and runner_up is not None
            and abs(best.score - runner_up.score) < _TIE_MARGIN
        ):
            judge_dict = await self._judge(
                task=task,
                best=best,
                runner_up=runner_up,
                model=model,
                max_tokens=max_tokens,
                task_id=state.task_id,
            )
            # The judge returns {"winner": "best"|"runner_up", "reason": str}.
            if judge_dict.get("winner") == "runner_up":
                chosen = runner_up

        self._tracer.trace(
            state.task_id,
            step="tot_plan_done",
            kind="decision",
            detail={
                "n_branches": self._max_branches,
                "n_parsed": len(parsed),
                "candidates": [c.to_dict() for c in candidates],
                "chosen_branch": chosen.branch,
                "chosen_score": round(chosen.score, 2),
                "judge": judge_dict,
            },
        )

        assert chosen.steps is not None  # narrowed by being in `parsed`
        return chosen.steps

    # ------------------------------------------------------------------ branch

    async def _gen_one(
        self,
        *,
        branch: int,
        temperature: float,
        model: str,
        max_tokens: int,
        messages_template: list[dict[str, Any]],
        task_id: str,
    ) -> CandidatePlan:
        """Generate ONE candidate plan with a specific temperature.

        Budget-checked before the call (STOP semantics). Commits actual usage
        after. Traced. Returns a :class:`CandidatePlan` (ok=False on parse
        failure; raises on BudgetExceeded so the caller can record + continue).
        """
        # Estimate: a JSON plan is small; reserve a third of the cap.
        est_in = sum(len(str(m.get("content", ""))) for m in messages_template) // 4
        est_out = min(max_tokens, max(max_tokens // 3, 200))
        self._budgets.check("plan", est_in, est_out, task_id=task_id)

        started = time.monotonic()
        result: ChatResult = await self._client.chat(
            model,
            messages_template,
            temperature=temperature,
            max_tokens=max_tokens,
            task_id=task_id,
            phase="plan",
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        text = result.content or ""
        usage = result.usage or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)

        try:
            self._budgets.commit_task(task_id, "plan", tokens_in, tokens_out)
            self._budgets.check_overshoot("plan", task_id=task_id)
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — accounting is best-effort
            pass

        self._tracer.trace(
            task_id,
            step=f"tot_branch_{branch}",
            kind="llm",
            detail={
                "model": model,
                "branch": branch,
                "temperature": temperature,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "estimated": bool(result.estimated),
                "ok": True,  # LLM returned something; parse may still fail
            },
            tokens=tokens_in + tokens_out,
            latency_ms=latency_ms,
        )

        steps, err = parse_plan_json(text)
        if steps is not None and err is None:
            return CandidatePlan(
                branch=branch,
                temperature=temperature,
                steps=steps,
                raw=text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        return CandidatePlan(
            branch=branch,
            temperature=temperature,
            raw=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            parse_error=err or "неизвестная ошибка разбора",
        )

    # ------------------------------------------------------------------ judge

    async def _judge(
        self,
        *,
        task: str,
        best: CandidatePlan,
        runner_up: CandidatePlan,
        model: str,
        max_tokens: int,
        task_id: str,
    ) -> dict[str, Any]:
        """One L1 Lite call to break ties when the top-2 scores are close.

        Returns a dict ``{"winner": "best"|"runner_up", "reason": str}`` or a
        default ``{"winner": "best", "reason": "judge parse failure"}`` on
        parse error (fail-safe: keep the deterministic winner).
        """
        assert best.steps is not None and runner_up.steps is not None
        prompt = (
            "Ты судья: выбери лучший план для задачи. Верни ТОЛЬКО JSON:\n"
            '{"winner":"best"|"runner_up","reason":"короткое объяснение"}\n'
            "Критерии: полнота шагов, отсутствие дублирования, реалистичность."
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"{prompt}\n\n"
                    f"Задача: {task}\n\n"
                    f"План A (score={best.score:.1f}):\n"
                    f"{json.dumps([s.model_dump(mode='json') for s in best.steps], ensure_ascii=False)}\n\n"
                    f"План B (score={runner_up.score:.1f}):\n"
                    f"{json.dumps([s.model_dump(mode='json') for s in runner_up.steps], ensure_ascii=False)}"
                ),
            }
        ]
        est_in = sum(len(str(m.get("content", ""))) for m in messages) // 4
        est_out = min(max_tokens, max(max_tokens // 3, 200))
        try:
            self._budgets.check("plan", est_in, est_out, task_id=task_id)
        except BudgetExceeded:
            # Can't afford the judge — fail-safe to deterministic winner.
            return {"winner": "best", "reason": "judge skipped: budget exceeded"}

        started = time.monotonic()
        result = await self._client.chat(
            model,
            messages,
            temperature=0.0,
            max_tokens=max_tokens,
            task_id=task_id,
            phase="plan",
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        text = result.content or ""
        usage = result.usage or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)
        try:
            self._budgets.commit_task(task_id, "plan", tokens_in, tokens_out)
            self._budgets.check_overshoot("plan", task_id=task_id)
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001
            pass

        self._tracer.trace(
            task_id,
            step="tot_judge",
            kind="llm",
            detail={
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "estimated": bool(result.estimated),
            },
            tokens=tokens_in + tokens_out,
            latency_ms=latency_ms,
        )

        repaired = json_repair(text)
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError:
            return {"winner": "best", "reason": "judge parse failure: kept deterministic winner"}
        if not isinstance(data, dict):
            return {"winner": "best", "reason": "judge returned non-object"}
        winner = data.get("winner")
        if winner not in ("best", "runner_up"):
            return {"winner": "best", "reason": "judge returned invalid winner"}
        return {
            "winner": winner,
            "reason": str(data.get("reason", ""))[:200],
        }


# --- a thin fallback that delegates to PlannerAgent for non-ToT runs --------


def make_planner(
    *,
    tot: bool,
    client: GigaClient,
    router: ModelRouter,
    budgets: BudgetTracker,
    tracer: Tracer,
    settings: Settings | None = None,
) -> PlannerAgent | ToTPlanner:
    """Factory: pick the ToT planner or the legacy one based on the flag.

    The pipeline calls this so it doesn't have to know about ToT details.
    """
    if tot:
        return ToTPlanner(client, router, budgets, tracer, settings=settings)
    return PlannerAgent(client, router, budgets, tracer, settings=settings)


# Re-export GigaForgeError for convenience in case callers want to catch the
# whole family (PlanEscalation already inherits from it).
_ = GigaForgeError  # silence "imported but unused" linters

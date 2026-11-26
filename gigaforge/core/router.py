"""Model routing with per-phase budgets (M4–M5).

Why a router instead of calling the client directly:

* **Cost control.**  Planning, summarising and reviewing do not need the same
  model as diff authoring.  Each phase declares a preferred model, fallbacks
  and a token/call budget.
* **Hard stops.**  When a phase exhausts its budget the router raises
  :class:`PhaseBudgetExceeded` *before* spending more — a runaway repair loop
  is a spend incident, not just a slow run.
* **Receipts.**  Every call is recorded as a :class:`PhaseUsage`, which lands in
  the trace and in ``metrics_points``.

The router never inspects HTTP or retries: those belong to the client layer.
It only decides *which* model and *whether* the phase may still spend.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.types import usage_tokens
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import PHASE_CAP_ATTRS, BudgetTracker

from .config import CoreConfig, PhaseBudget, PhaseRoute
from .contracts import RunContext
from .errors import NoModelForPhaseError, PhaseBudgetExceeded, UnknownPhaseError
from .ladder import Decision, Ladder, LadderPolicy, Level, Signals

_HARD_REASONING_TYPES = frozenset({"hard_reasoning"})
_LITE_TYPES = frozenset({"plan", "review", "classify", "summarize"})
_PRO_TYPES = frozenset({"code"})
_BASE_TYPES = frozenset({"explain"})
_UNSET = object()



def estimate_tokens(text: str, chars_per_token: float = 4.0) -> int:
    if not text:
        return 0
    ratio = chars_per_token if chars_per_token > 0 else 4.0
    return max(1, int(round(len(text) / ratio)))


def _limit(configured: int, used: int) -> Optional[int]:
    """Remaining allowance for a phase, or ``None`` when it is unlimited.

    ``0`` means "no budget configured" and stays unlimited, which is what an
    absent phase entry must do.  A negative value means the phase is disabled
    on purpose: that is the only way to make the very first call fail, since
    the check happens before the call is made.
    """
    if configured == 0:
        return None
    if configured < 0:
        return 0
    return max(0, configured - used)


def usage_dict(usage: Any) -> dict[str, Any]:
    """Normalise whatever a client returned into a flat token dict."""
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "source": "none"}
    if isinstance(usage, Mapping):
        data = dict(usage)
    elif hasattr(usage, "to_dict"):
        data = dict(usage.to_dict())
    else:
        data = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "source": getattr(usage, "source", "usage"),
            "estimated": bool(getattr(usage, "estimated", False)),
        }
    total = int(data.get("total_tokens", 0) or 0)
    prompt = int(data.get("prompt_tokens", 0) or 0)
    completion = int(data.get("completion_tokens", 0) or 0)
    if total == 0 and (prompt or completion):
        total = prompt + completion
    data["prompt_tokens"] = prompt
    data["completion_tokens"] = completion
    data["total_tokens"] = total
    data.setdefault("source", "usage")
    return data


@dataclass
class PhaseUsage:
    """Token/call accounting for a single phase."""

    phase: str
    model: str = ""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_tokens: int = 0
    latency_ms: float = 0.0
    fallback_used: int = 0
    cache_hits: int = 0

    def add(self, usage: Any, *, latency_ms: float = 0.0) -> None:
        """Accumulate one call's usage.

        ``usage`` is a :class:`~gigaforge.client.types.Usage` dataclass on the
        real client and a plain dict on the fake.  This method used ``.get()``
        unconditionally, so on the real client it raised ``AttributeError`` —
        and the caller swallowed it, which is why every live run reported zero
        tokens and the per-phase table was empty.
        """
        self.calls += 1
        prompt, completion = usage_tokens(usage)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        total = getattr(usage, "total_tokens", None)
        if total is None and isinstance(usage, Mapping):
            total = usage.get("total_tokens")
        self.total_tokens += int(total or (prompt + completion))
        source = getattr(usage, "source", None)
        estimated = getattr(usage, "estimated", None)
        if source is None and isinstance(usage, Mapping):
            source = usage.get("source")
            estimated = usage.get("estimated")
        if source == "estimate" or estimated:
            self.estimated_tokens += int(total or (prompt + completion))
        self.latency_ms += float(latency_ms or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "model": self.model,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_tokens": self.estimated_tokens,
            "latency_ms": round(self.latency_ms, 3),
            "fallback_used": self.fallback_used,
            "cache_hits": self.cache_hits,
        }


class Route(str):
    """The decision the router made for one call. Inherits from str so it acts as model name."""

    phase: str
    model: str
    temperature: float
    max_tokens: Optional[int]
    candidates: tuple[str, ...]
    fallback_index: int
    reason: str

    def __new__(
        cls,
        model: str,
        phase: str = "",
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        candidates: Sequence[str] = (),
        fallback_index: int = 0,
        reason: str = "",
    ) -> Route:
        inst = super().__new__(cls, model)
        inst.model = model
        inst.phase = phase
        inst.temperature = temperature
        inst.max_tokens = max_tokens
        inst.candidates = tuple(candidates)
        inst.fallback_index = fallback_index
        inst.reason = reason
        return inst

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "candidates": list(self.candidates),
            "fallback_index": self.fallback_index,
            "reason": self.reason,
        }


@dataclass
class RouterStats:
    calls: int = 0
    budget_rejections: int = 0
    fallbacks: int = 0
    unknown_phase: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "budget_rejections": self.budget_rejections,
            "fallbacks": self.fallbacks,
            "unknown_phase": self.unknown_phase,
        }


class ModelRouter:
    """Phase-aware wrapper around a :class:`ChatClient`.

    Thread-safe: the pipeline may run nodes concurrently, and a shared budget
    counter that races would let a phase overspend.
    """

    def __init__(
        self,
        client: Any = None,
        config: Optional[CoreConfig] = None,
        *,
        budget_tracker: BudgetTracker | None = None,
        settings: Settings | None = None,
        metrics: Any = None,
        trace: Any = None,
        exclude_models: Sequence[str] = (),
    ) -> None:
        self.client = client
        self.config = config or CoreConfig()
        self.metrics = metrics
        self.trace = trace
        self.exclude_models = tuple(exclude_models)
        self._lock = threading.Lock()
        self._usage: dict[str, PhaseUsage] = {}
        self._stats = RouterStats()
        self._last_route: Optional[Route] = None
        self._s = settings or get_settings()
        self._budget = budget_tracker or BudgetTracker(settings=self._s)
        self.last_fallback: str | None = None
        #: The decision ladder.  The router answers "which model for this
        #: phase"; the ladder answers "how much intelligence may this step
        #: buy".  They are separate because the second question has an answer
        #: the first cannot express: *no model call at all* (L0).
        #:
        #: The ladder is built with its ceiling open on purpose.  It reports
        #: what the step *needs*; whether the account may serve a flagship
        #: model is the router's gate (``GIGAFORGE_ALLOW_MAX``), applied in
        #: :meth:`_model_for_level`.  Building the ladder with the gate baked in
        #: made every escalation from an authoring phase a no-op — the ladder
        #: said "strong" for a step that had already failed twice at strong.
        self.ladder = Ladder(LadderPolicy(allow_flagship=True))
        #: The last ladder verdict, for the trace and the ledger.
        self.last_decision: Optional[Decision] = None

    def reconfigure(self, *, config: Optional[CoreConfig] = None,
                    client: Any = _UNSET) -> None:
        """Atomically replace the policy or client at a run boundary."""
        with self._lock:
            if config is not None:
                self.config = config
            if client is not _UNSET:
                self.client = client

    @property
    def budget(self) -> BudgetTracker:
        return self._budget

    def _allow_max(self) -> bool:
        v = os.environ.get("GIGAFORGE_ALLOW_MAX", "")
        return v.strip().lower() in {"1", "true", "yes", "on"}

    # ── introspection ────────────────────────────────────────────────────────
    @property
    def last_route(self) -> Optional[Route]:
        return self._last_route

    @property
    def stats(self) -> RouterStats:
        return self._stats

    def usage(self, phase: Optional[str] = None) -> Any:
        with self._lock:
            if phase is None:
                return {k: v.to_dict() for k, v in self._usage.items()}
            entry = self._usage.get(phase)
            return entry.to_dict() if entry else None

    def remaining(self, phase: str) -> dict[str, Optional[int]]:
        """Tokens and calls still available to ``phase`` (``None`` = unlimited)."""
        budget = self.config.budget_for(phase)
        with self._lock:
            used = self._usage.get(phase)
            tokens_used = used.total_tokens if used else 0
            calls_used = used.calls if used else 0
        tokens_left = _limit(budget.max_tokens, tokens_used)
        calls_left = _limit(budget.max_calls, calls_used)
        return {"tokens": tokens_left, "calls": calls_left,
                "exhausted": (tokens_left == 0 or calls_left == 0)}

    # ── routing ──────────────────────────────────────────────────────────────
    def decide_level(
        self,
        phase: str,
        *,
        attempt: int = 0,
        repeats: int = 0,
        contract_rejected: bool = False,
        files_touched: int = 0,
        sections: int = 0,
        task_kind: str = "",
        deterministic_available: bool = False,
        forced_level: Optional[Level] = None,
    ) -> Decision:
        """Ask the ladder how much intelligence this step may buy.

        The router's own budget counters supply the pressure signal, so a
        caller does not have to know the phase's remaining allowance to get an
        honest answer.  The verdict is kept on ``last_decision`` for the trace.
        """
        budget = self.config.budget_for(phase)
        with self._lock:
            used = self._usage.get(phase)
            tokens_used = used.total_tokens if used else 0
        decision = self.ladder.decide(
            Signals(
                phase=phase,
                attempt=attempt,
                repeats=repeats,
                contract_rejected=contract_rejected,
                files_touched=files_touched,
                sections=sections,
                tokens_used=tokens_used,
                tokens_budget=int(budget.max_tokens or 0),
                task_kind=task_kind,
                deterministic_available=deterministic_available,
                forced_level=forced_level,
            )
        )
        self.last_decision = decision
        return decision

    def _may_serve(self, model: str) -> bool:
        """True when this account is allowed to call *model*.

        A flagship is usually the ``Max`` family, which the account gates behind
        ``GIGAFORGE_ALLOW_MAX``.  Asking here means the ladder never promises a
        model the router will refuse.
        """
        if model in self.exclude_models:
            return False
        if "max" in model.lower() and not self._allow_max():
            return False
        return True

    def _model_for_level(self, phase: str, level: Level) -> Optional[str]:
        """The model a level maps to, or ``None`` when the level is L0.

        The mapping is deliberately coarse: a level is a *class* of model, and
        the per-phase route is the operator's choice within that class.  L1
        takes the cheap model, L2 the phase's configured model, L3 the flagship
        — and L3 is refused unless the account allows it, which is the router's
        gate, not the ladder's.

        **L3 must differ from L2 or it is not an escalation.**  The previous
        version returned ``strong_model`` for L3 while ``default_routes`` already
        put ``strong_model`` on plan/code/fix/react, so the top rung resolved to
        the model that had just failed.  Measured: L2 == L3 on four of six
        phases.  Now L3 returns ``flagship_model`` and ``None`` when no flagship
        is configured — the ladder then stops at L2 instead of pretending.
        """
        if level is Level.L0_DETERMINISTIC:
            return None
        if level is Level.L1_CHEAP:
            return self.config.cheap_model
        if level is Level.L3_FLAGSHIP:
            flagship = str(getattr(self.config, "flagship_model", "") or "").strip()
            if not flagship:
                return None
            if flagship == self.config.strong_model:
                # A flagship that is the strong model is not a rung.
                return None
            return flagship
        return None  # L2: the phase's own route decides

    def route(
        self,
        phase: str,
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        level: Optional[Level] = None,
    ) -> Route:
        """Decide the model and parameters for ``phase``, enforcing the budget.

        Priority is deliberate: an explicit ``model=`` argument, then the **V2
        per-phase route** (``CoreConfig.routes``, which is what the live model
        catalogue and the operator both write to), then the legacy
        ``Settings.models`` table, then the built-in defaults.

        This order used to be reversed, and the consequence was severe: every
        phase the legacy table knew about (plan, review, summarize, code…) was
        routed to ``GigaChat-3-Lightning`` — a model this account does not serve —
        while the correctly resolved route sat unused in ``config.routes``.  A live
        run failed with ``{"status":404,"message":"No such model"}`` on the very
        first planning call.
        """
        self.last_fallback = None
        models = self._s.models

        # A level the caller asked for overrides the phase's configured model.
        # L0 is not a model at all: it means "do not call one", and the caller
        # must handle it — returning a Route here would silently spend a call
        # the ladder said not to make.
        if level is not None:
            if level is Level.L0_DETERMINISTIC:
                raise NoModelForPhaseError(
                    f"phase {phase!r} was decided at L0 (deterministic): no model call",
                    detail={"level": int(level), "phase": phase},
                )
            levelled = self._model_for_level(phase, level)
            if levelled and self._may_serve(levelled):
                model = levelled
            elif level is Level.L3_FLAGSHIP:
                # The ladder asked for the flagship rung and this account cannot
                # serve it.  Falling through to the phase's own route is right —
                # but it must be *said*, because the trace otherwise reports an
                # escalation that did not happen.
                self.last_fallback = (
                    "flagship rung unavailable "
                    f"({levelled or 'no flagship configured'}); "
                    "served by the phase's own route"
                )

        spec: Optional[PhaseRoute] = self.config.routes.get(phase)
        if spec is not None and model is None:
            budget: PhaseBudget = self.config.budget_for(phase)
            self._enforce_budget(phase, budget)
            candidates = [spec.model, *spec.fallbacks]
            allowed = [c for c in candidates if c not in self.exclude_models]
            if not allowed:
                raise NoModelForPhaseError(
                    f"no usable model for phase {phase!r}",
                    detail={"candidates": candidates,
                            "excluded": list(self.exclude_models)},
                )
            chosen = allowed[0]
            route = Route(
                model=chosen,
                phase=phase,
                temperature=(float(temperature) if temperature is not None
                             else float(spec.temperature)),
                max_tokens=(int(max_tokens) if max_tokens is not None
                            else (budget.max_output_tokens or None)),
                candidates=tuple(allowed),
                fallback_index=0,
                reason=spec.rationale or f"route for {phase}",
            )
            self._last_route = route
            return route

        if phase in _HARD_REASONING_TYPES:
            if not self._allow_max():
                raise BudgetExceeded(
                    f"task_type {phase!r} requires GigaChat-2-Max, but "
                    "GIGAFORGE_ALLOW_MAX is not set to 1. Refusing to route."
                )
            chosen_model = models.get("hard_reasoning", "GigaChat-2-Max")
        elif phase in _LITE_TYPES:
            chosen_model = models.get(phase, models.get("plan", "GigaChat-2-Lite"))
        elif phase in _PRO_TYPES:
            chosen_model = models.get("code", "GigaChat-2-Pro")
        elif phase in _BASE_TYPES:
            chosen_model = models.get("explain", "GigaChat-2")
        elif phase in models:
            chosen_model = models[phase]
        else:
            self.last_fallback = f"unknown task_type {phase!r} -> explain (base)"
            chosen_model = models.get("explain", "GigaChat-2")

        if model:
            chosen_model = model

        # ``spec`` is None here: a configured route returned above.
        chosen_temperature = float(temperature) if temperature is not None else 0.0
        chosen_max = int(max_tokens) if max_tokens is not None else None

        route = Route(
            model=chosen_model,
            phase=phase,
            temperature=chosen_temperature,
            max_tokens=chosen_max,
            candidates=(chosen_model,),
            fallback_index=0,
            reason=getattr(spec, "rationale", "") or "default phase route",
        )
        self._last_route = route
        return route

    def available_budgets(self, phase: str) -> dict[str, Any]:
        """Return remaining budgets for the given phase (for CLI/observability)."""
        cap_in_attr, cap_out_attr = PHASE_CAP_ATTRS.get(
            phase, ("budget_plan_in", "budget_plan_out")
        )
        cap_in = int(getattr(self._s, cap_in_attr, 2000))
        cap_out = int(getattr(self._s, cap_out_attr, 1000))
        remaining = self._budget.remaining(phase)
        return {
            "phase": phase,
            "cap_in": cap_in,
            "cap_out": cap_out,
            "remaining": remaining,
            "task_total": self._budget.task_total(),
            "task_cap": int(self._s.budget_task_cap),
            "task_remaining": max(0, int(self._s.budget_task_cap) - self._budget.task_total()),
            "fixloop_iters_cap": int(self._s.budget_fixloop_iters),
        }

    def routing_table(self) -> dict[str, str]:
        """Return a copy of the routing table (task_type -> model)."""
        return dict(self._s.models)


    def _enforce_budget(self, phase: str, budget: PhaseBudget) -> None:
        """Refuse the call when the phase is out of allowance.

        The zero/negative convention is shared with :func:`_limit` — ``0`` is
        "no budget configured" (unlimited) and a negative value disables the
        phase.  Keeping the two in sync matters: when they disagreed, a phase
        configured as disabled was still allowed through by ``route`` and the
        budget silently did nothing.
        """
        with self._lock:
            used = self._usage.get(phase)
            tokens_used = used.total_tokens if used else 0
            calls_used = used.calls if used else 0
        max_calls = budget.max_calls
        max_tokens = budget.max_tokens
        if max_calls and calls_used >= (0 if max_calls < 0 else max_calls):
            with self._lock:
                self._stats.budget_rejections += 1
            raise PhaseBudgetExceeded(
                f"phase {phase!r} used its {max_calls} calls",
                detail={"phase": phase, "calls_used": calls_used, "max_calls": max_calls},
            )
        if max_tokens and tokens_used >= (0 if max_tokens < 0 else max_tokens):
            with self._lock:
                self._stats.budget_rejections += 1
            raise PhaseBudgetExceeded(
                f"phase {phase!r} used its {max_tokens} tokens",
                detail={"phase": phase, "tokens_used": tokens_used, "max_tokens": max_tokens},
            )

    # ── completion ───────────────────────────────────────────────────────────
    def complete(
        self,
        phase: str,
        messages: Sequence[Any],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        ctx: Optional[RunContext] = None,
        level: Optional[Level] = None,
        **kwargs: Any,
    ) -> Any:
        """Route, call the client, and account for the result.

        The client is called with the routed model first; if it raises, the
        router walks the fallback list.  Budget exhaustion is checked *once*
        before the attempt, and failures still consume a call — a phase that
        errors out has spent the attempt, and pretending otherwise would let a
        permanently broken phase loop forever.

        ``level`` is the ladder's verdict for this step.  It is passed through
        to :meth:`route`, so a step the ladder raised to L1 is actually served
        by the cheap model instead of the phase's configured one — a level that
        only appeared in a log would be a label, not a decision.
        """
        if ctx is not None:
            ctx.raise_if_cancelled()
        route = self.route(phase, model=model, temperature=temperature,
                           max_tokens=max_tokens, level=level)

        model_sequence = list(route.candidates)
        last_error: Optional[BaseException] = None
        for index, candidate in enumerate(model_sequence):
            started = ctx.clock.now() if ctx is not None else None
            try:
                result = self.client.complete(
                    messages,
                    model=candidate,
                    temperature=route.temperature,
                    max_tokens=route.max_tokens,
                    **kwargs,
                )
            except PhaseBudgetExceeded:
                raise
            except BaseException as exc:  # noqa: BLE001 - fallback walking
                if ctx is not None:
                    ctx.raise_if_cancelled()
                last_error = exc
                self._record_failure(phase, candidate, exc, ctx)
                if index == len(model_sequence) - 1:
                    raise
                with self._lock:
                    self._stats.fallbacks += 1
                continue

            if ctx is not None:
                ctx.raise_if_cancelled()
            latency_ms = (ctx.clock.now() - started) * 1000.0 if started is not None else 0.0
            self._record_success(phase, candidate, result, latency_ms, ctx, fallback_index=index)
            return result

        # Unreachable: the loop either returns or re-raises.
        raise NoModelForPhaseError(f"phase {phase!r} produced no result", detail={"error": str(last_error)})

    # ── accounting ───────────────────────────────────────────────────────────
    def _entry(self, phase: str, model: str) -> PhaseUsage:
        # Usage accumulates per *phase*, across models.  Resetting on a model
        # change would let a phase that failed over to a fallback forget the
        # tokens it already spent on the model that failed — and the budget is
        # exactly what must not be forgettable.
        entry = self._usage.get(phase)
        if entry is None:
            entry = PhaseUsage(phase=phase, model=model)
            self._usage[phase] = entry
        if model:
            entry.model = model
        return entry

    def _record_success(
        self,
        phase: str,
        model: str,
        result: Any,
        latency_ms: float,
        ctx: Optional[RunContext],
        *,
        fallback_index: int = 0,
    ) -> None:
        usage = usage_dict(getattr(result, "usage", None))
        if usage.get("total_tokens", 0) == 0:
            # The provider omitted usage (common on streamed answers): fall back
            # to a chars/4 estimate and mark it, so a dashboard never silently
            # mixes measured and guessed numbers.
            text = ""
            for attr in ("content", "text"):
                candidate = getattr(result, attr, None)
                if isinstance(candidate, str) and candidate:
                    text = candidate
                    break
            usage = {
                "prompt_tokens": 0,
                "completion_tokens": estimate_tokens(text, self.config.chars_per_token),
                "total_tokens": estimate_tokens(text, self.config.chars_per_token),
                "source": "estimate",
                "estimated": True,
            }

        with self._lock:
            entry = self._entry(phase, model)
            entry.add(usage, latency_ms=latency_ms)
            entry.fallback_used += fallback_index
            self._stats.calls += 1

        if self.metrics is not None:
            # Every point carries the run and node it belongs to.  Without that the
            # dashboard can only show global totals: per-run KPIs silently become
            # "sum of every run ever", which reads as a wrong number next to a
            # run-scoped token count.
            scope = {
                "run_id": ctx.run_id if ctx is not None else "",
                "node": ctx.node if ctx is not None else "",
            }
            self.metrics.incr("core.router.calls", 1, {"phase": phase, "model": model}, **scope)
            self.metrics.incr(
                "core.router.tokens", int(usage.get("total_tokens", 0) or 0),
                {"phase": phase, "source": usage.get("source", "usage")}, **scope,
            )
            self.metrics.observe("core.router.latency_ms", latency_ms, {"phase": phase}, **scope)
            if fallback_index:
                self.metrics.incr("core.router.fallback", fallback_index,
                                  {"phase": phase}, **scope)

        if self.trace is not None:
            self.trace.event(
                "router.call",
                phase=phase,
                model=model,
                tokens=int(usage.get("total_tokens", 0) or 0),
                source=usage.get("source", "usage"),
                latency_ms=round(latency_ms, 3),
                fallback_index=fallback_index,
                run_id=ctx.run_id if ctx else None,
                node=ctx.node if ctx else None,
            )

        if ctx is not None and getattr(ctx, "state", None) is not None:
            # The model travels with the usage so the state can price the call:
            # the same token count on a different model is a different bill.
            priced = dict(usage)
            priced.setdefault("model", model)
            ctx.state.record_phase_usage(phase, priced)

    def _record_failure(
        self, phase: str, model: str, exc: BaseException, ctx: Optional[RunContext]
    ) -> None:
        code = getattr(exc, "code", "GF-CORE-RT-099")
        with self._lock:
            entry = self._entry(phase, model)
            entry.calls += 1
            self._stats.calls += 1
        if self.metrics is not None:
            self.metrics.incr(
                "core.router.errors", 1, {"phase": phase, "code": code},
                run_id=ctx.run_id if ctx is not None else "",
                node=ctx.node if ctx is not None else "",
            )
        if self.trace is not None:
            self.trace.event(
                "router.error",
                phase=phase,
                model=model,
                code=code,
                error=str(exc)[:500],
                run_id=ctx.run_id if ctx else None,
                node=ctx.node if ctx else None,
            )

    def reset(self) -> None:
        with self._lock:
            self._usage.clear()
            self._stats = RouterStats()

    def child(self) -> "ModelRouter":
        """A router for one isolated run: own counters, own budget, shared client.

        ``reset()`` was added so a phase budget would be per-run, but the
        sub-agent fan-out hands every child the *same* router object
        (``pipeline.py``: ``router=self.router``).  A child's ``run()`` then
        called ``reset()`` on the parent's router, and the parent forgot what it
        had already spent: measured, a parent that used 3 calls in ``plan`` saw
        ``usage("plan") is None`` after the fan-out.

        The phase caps had the mirror-image defect: ``BudgetTracker`` keeps
        ``_phase_used`` on the instance, and the instance was process-wide, so
        three children each got the parent's full phase allowance and the
        parent's budget was consumed three times over.

        A child router shares everything that must be shared — the client, the
        route table, the metrics sink, the tracer — and owns everything that
        must not be: the per-phase usage counters and the budget tracker.  The
        circuit breaker lives on the client, so it stays shared, which is what
        you want: a provider outage is a property of the provider, not of one
        sub-agent.
        """
        child = ModelRouter(
            self.client,
            self.config,
            budget_tracker=BudgetTracker(settings=self._s),
            settings=self._s,
            metrics=self.metrics,
            trace=self.trace,
            exclude_models=self.exclude_models,
        )
        return child

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"ModelRouter(client={type(self.client).__name__}, phases={len(self.config.routes)})"


__all__ = [
    "ModelRouter",
    "Route",
    "PhaseUsage",
    "RouterStats",
    "estimate_tokens",
    "usage_dict",
]

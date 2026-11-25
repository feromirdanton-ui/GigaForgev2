"""Core configuration (M4–M5).

Every knob lives here and is overridable from the environment with the
``GF_CORE__`` prefix (double underscore = nesting), matching the
``ClientConfig`` / ``L0Config`` / ``FirewallConfig`` conventions used by the
lower layers.  Nothing in ``gigaforge.core`` hardcodes a limit.

Example::

    GF_CORE__DEFAULT_MODEL=GigaChat-Pro
    GF_CORE__PHASE_BUDGETS__plan__max_tokens=30000
    GF_CORE__BREAKER__failure_threshold=3
    GF_CORE__FIXLOOP__max_iterations=3
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

ENV_PREFIX = "GF_CORE__"


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PhaseBudget:
    """Token and call budget for one routing phase."""

    max_tokens: int = 0        # 0 = unlimited
    max_calls: int = 0         # 0 = unlimited
    max_output_tokens: int = 0  # per-call completion cap; 0 = model default

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "max_calls": self.max_calls,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class PhaseRoute:
    """Which model a phase prefers and why."""

    model: str
    fallbacks: tuple[str, ...] = ()
    temperature: float = 0.2
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "fallbacks": list(self.fallbacks),
            "temperature": self.temperature,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class RetryConfig:
    """Node-level retry tuning.

    ``backoff_max`` is 30 s, not 5 s, because the limit that actually stops a
    run is a provider quota window, and GigaChat's is a minute.  Measured: two
    concurrent runs hit 429 on the ``plan`` node, the node had ``retries=0`` so
    it failed outright, and the whole run died with ``GF-CORE-DAG-026`` — a
    transient quota blip reported as a failed run.  A ceiling below the window
    it is waiting for cannot outlast that window.
    """

    max_attempts: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    jitter: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_base": self.backoff_base,
            "backoff_max": self.backoff_max,
            "jitter": self.jitter,
        }


@dataclass(frozen=True)
class BreakerConfig:
    """Circuit breaker tuning.  ``failure_threshold`` defaults to 3."""

    failure_threshold: int = 3
    success_threshold: int = 2      # consecutive successes to close from half-open
    reset_timeout: float = 30.0     # seconds in open before probing half-open
    half_open_max_calls: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_threshold": self.failure_threshold,
            "success_threshold": self.success_threshold,
            "reset_timeout": self.reset_timeout,
            "half_open_max_calls": self.half_open_max_calls,
        }


@dataclass(frozen=True)
class FixLoopConfig:
    """Repair budget.  ``max_iterations`` counts *fix* attempts after the first.

    ``max_iterations = 3`` therefore means at most four proposals in total for
    the whole loop — the initial attempt plus three repairs.
    """

    max_iterations: int = 3
    stop_on_identical: bool = True
    escalate_after_repeats: int = 2  # identical failures before giving up early

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_iterations": self.max_iterations,
            "stop_on_identical": self.stop_on_identical,
            "escalate_after_repeats": self.escalate_after_repeats,
        }


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool = True
    ttl_seconds: float = 3600.0     # 0 = no TTL
    namespace: str = "task"
    max_entries: int = 5000
    invalidate_on_git_change: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ttl_seconds": self.ttl_seconds,
            "namespace": self.namespace,
            "max_entries": self.max_entries,
            "invalidate_on_git_change": self.invalidate_on_git_change,
        }


@dataclass(frozen=True)
class SubAgentConfig:
    use_worktrees: bool = True
    worktree_root: str = ""         # empty → <repo>/.gigaforge/worktrees
    isolate_context: bool = True
    max_parallel: int = 4
    inherit_state: bool = False     # sub-agents start from a fresh scratchpad
    aggregate_in_spawn_order: bool = True
    keep_worktrees: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "use_worktrees": self.use_worktrees,
            "worktree_root": self.worktree_root,
            "isolate_context": self.isolate_context,
            "max_parallel": self.max_parallel,
            "inherit_state": self.inherit_state,
            "aggregate_in_spawn_order": self.aggregate_in_spawn_order,
            "keep_worktrees": self.keep_worktrees,
        }


@dataclass(frozen=True)
class CoreConfig:
    """Top-level orchestration configuration."""

    # ── bookkeeping ──────────────────────────────────────────────────────────
    tenant: str = "default"
    repo_root: str = "."
    db_path: str = ".gigaforge/core.sqlite3"
    trace_path: str = ".gigaforge/traces/core.jsonl"
    log_level: str = "INFO"

    # ── routing ──────────────────────────────────────────────────────────────
    default_model: str = "GigaChat"
    cheap_model: str = "GigaChat"
    strong_model: str = "GigaChat-Pro"
    #: The model L3 buys.  It must be a *different* model from ``strong_model``,
    #: or the ladder's top rung is a no-op: measured on the previous build, L2
    #: and L3 resolved to ``GigaChat-Pro`` on plan/code/fix/react, so escalating
    #: a step that had failed twice bought the same model that had just failed
    #: twice.  Empty means "no flagship is configured", and the ladder then stops
    #: at L2 rather than pretending to escalate.
    flagship_model: str = ""
    max_concurrency: int = 4

    # ── DAG execution ────────────────────────────────────────────────────────
    node_timeout: float = 480.0
    react_timeout: float = 480.0
    pipeline_timeout: float = 2400.0
    checkpoint: bool = True
    dlq_enabled: bool = True
    #: Run independent nodes of one DAG level concurrently.  Nodes in a wave see
    #: the same artifact snapshot and merge in declaration order, so a wave cannot
    #: change the outcome of a sequential run — it only overlaps work that never
    #: depended on itself (``intake`` ∥ ``index``).
    parallel_waves: bool = True
    wave_workers: int = 4

    # ── observability ────────────────────────────────────────────────────────
    metrics_enabled: bool = True
    trace_enabled: bool = True
    trace_max_value_chars: int = 2000

    # ── prompt stability ─────────────────────────────────────────────────────
    stable_prefix: bool = True
    max_scratchpad_chars: int = 8000
    max_plan_steps: int = 24
    chars_per_token: float = 4.0

    # ── sub-configs (frozen, but replaceable via ``with_override``) ──────────
    retry: RetryConfig = field(default_factory=RetryConfig)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    fixloop: FixLoopConfig = field(default_factory=FixLoopConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    subagents: SubAgentConfig = field(default_factory=SubAgentConfig)

    # ── per-phase routing + budgets ──────────────────────────────────────────
    routes: Mapping[str, PhaseRoute] = field(default_factory=dict)
    budgets: Mapping[str, PhaseBudget] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.routes:
            object.__setattr__(self, "routes", default_routes(self))
        if not self.budgets:
            object.__setattr__(self, "budgets", default_budgets())

    # ── lookups ──────────────────────────────────────────────────────────────
    def route_for(self, phase: str) -> Optional[PhaseRoute]:
        return self.routes.get(phase)

    def budget_for(self, phase: str) -> PhaseBudget:
        return self.budgets.get(phase, PhaseBudget())

    def phases(self) -> tuple[str, ...]:
        return tuple(self.routes)

    def with_override(self, **kwargs: Any) -> "CoreConfig":
        return replace(self, **kwargs)

    def with_model(self, model: str) -> "CoreConfig":
        """Return a config pinned to *model*, preserving prior choices as fallbacks."""
        selected = str(model or "").strip()
        if not selected:
            raise ValueError("empty model name")
        fallbacks = tuple(
            candidate for candidate in
            (self.default_model, self.cheap_model, self.strong_model)
            if candidate and candidate != selected
        )
        routes = {
            phase: replace(route, model=selected, fallbacks=fallbacks)
            for phase, route in (self.routes or {}).items()
        }
        return replace(
            self,
            routes=routes,
            strong_model=selected,
            cheap_model=selected,
            default_model=selected,
        )

    # ── env loading ──────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "CoreConfig":
        """Build a config from ``GF_CORE__*`` variables.

        Nested groups use the abbreviated flat keys
        ``GF_CORE__BREAKER__FAILURE_THRESHOLD`` → ``breaker.failure_threshold``.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        flat: dict[str, Any] = {}
        for raw_key, value in source.items():
            if not raw_key.startswith(ENV_PREFIX):
                continue
            path = raw_key[len(ENV_PREFIX):].lower().split("__")
            flat[".".join(path)] = value

        def text(key: str, default: str) -> str:
            return str(flat.get(key, default))

        def integer(key: str, default: int) -> int:
            return _as_int(flat.get(key), default)

        def number(key: str, default: float) -> float:
            return _as_float(flat.get(key), default)

        def flag(key: str, default: bool) -> bool:
            return _as_bool(flat.get(key), default)

        retry = RetryConfig(
            max_attempts=integer("retry.max_attempts", 3),
            backoff_base=number("retry.backoff_base", 0.25),
            backoff_max=number("retry.backoff_max", 5.0),
            jitter=flag("retry.jitter", True),
        )
        breaker = BreakerConfig(
            failure_threshold=integer("breaker.failure_threshold", 3),
            success_threshold=integer("breaker.success_threshold", 2),
            reset_timeout=number("breaker.reset_timeout", 30.0),
            half_open_max_calls=integer("breaker.half_open_max_calls", 1),
        )
        fixloop = FixLoopConfig(
            max_iterations=integer("fixloop.max_iterations", 3),
            stop_on_identical=flag("fixloop.stop_on_identical", True),
            escalate_after_repeats=integer("fixloop.escalate_after_repeats", 2),
        )
        cache = CacheConfig(
            enabled=flag("cache.enabled", True),
            ttl_seconds=number("cache.ttl_seconds", 3600.0),
            namespace=text("cache.namespace", "task"),
            max_entries=integer("cache.max_entries", 5000),
            invalidate_on_git_change=flag("cache.invalidate_on_git_change", True),
        )
        subagents = SubAgentConfig(
            use_worktrees=flag("subagents.use_worktrees", True),
            worktree_root=text("subagents.worktree_root", ""),
            isolate_context=flag("subagents.isolate_context", True),
            max_parallel=integer("subagents.max_parallel", 4),
            inherit_state=flag("subagents.inherit_state", False),
            aggregate_in_spawn_order=flag("subagents.aggregate_in_spawn_order", True),
            keep_worktrees=flag("subagents.keep_worktrees", False),
        )

        base = cls(
            tenant=text("tenant", "default"),
            repo_root=text("repo_root", "."),
            db_path=text("db_path", ".gigaforge/core.sqlite3"),
            trace_path=text("trace_path", ".gigaforge/traces/core.jsonl"),
            log_level=text("log_level", "INFO"),
            default_model=text("default_model", "GigaChat"),
            cheap_model=text("cheap_model", "GigaChat"),
            strong_model=text("strong_model", "GigaChat-Pro"),
            flagship_model=text("flagship_model", ""),
            max_concurrency=integer("max_concurrency", 4),
            node_timeout=number("node_timeout", 120.0),
            react_timeout=number("react_timeout", 480.0),
            pipeline_timeout=number("pipeline_timeout", 900.0),
            checkpoint=flag("checkpoint", True),
            dlq_enabled=flag("dlq_enabled", True),
            metrics_enabled=flag("metrics_enabled", True),
            trace_enabled=flag("trace_enabled", True),
            trace_max_value_chars=integer("trace_max_value_chars", 2000),
            stable_prefix=flag("stable_prefix", True),
            max_scratchpad_chars=integer("max_scratchpad_chars", 8000),
            max_plan_steps=integer("max_plan_steps", 24),
            chars_per_token=number("chars_per_token", 4.0),
            retry=retry,
            breaker=breaker,
            fixloop=fixloop,
            cache=cache,
            subagents=subagents,
        )

        routes = dict(default_routes(base))
        budgets = dict(default_budgets())
        for key, value in flat.items():
            parts = key.split(".")
            if len(parts) == 3 and parts[0] == "phase_budgets":
                phase, knob = parts[1], parts[2]
                budgets[phase] = override_budget(
                    budgets.get(phase, PhaseBudget()), knob, flat.get(key)
                )
            if len(parts) == 3 and parts[0] == "phase_models":
                phase, knob = parts[1], parts[2]
                current = routes.get(phase, PhaseRoute(model=base.default_model))
                if knob == "model":
                    routes[phase] = PhaseRoute(
                        model=str(value),
                        fallbacks=current.fallbacks,
                        temperature=current.temperature,
                        rationale=current.rationale,
                    )
                elif knob == "temperature":
                    routes[phase] = PhaseRoute(
                        model=current.model,
                        fallbacks=current.fallbacks,
                        temperature=number(key, current.temperature),
                        rationale=current.rationale,
                    )

        return replace(base, routes=routes, budgets=budgets)

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "repo_root": self.repo_root,
            "db_path": self.db_path,
            "trace_path": self.trace_path,
            "default_model": self.default_model,
            "cheap_model": self.cheap_model,
            "strong_model": self.strong_model,
            "flagship_model": self.flagship_model,
            "max_concurrency": self.max_concurrency,
            "node_timeout": self.node_timeout,
            "react_timeout": self.react_timeout,
            "pipeline_timeout": self.pipeline_timeout,
            "checkpoint": self.checkpoint,
            "dlq_enabled": self.dlq_enabled,
            "metrics_enabled": self.metrics_enabled,
            "trace_enabled": self.trace_enabled,
            "stable_prefix": self.stable_prefix,
            "retry": self.retry.to_dict(),
            "breaker": self.breaker.to_dict(),
            "fixloop": self.fixloop.to_dict(),
            "cache": self.cache.to_dict(),
            "subagents": self.subagents.to_dict(),
            "phase_models": {k: v.to_dict() for k, v in self.routes.items()},
            "phase_budgets": {k: v.to_dict() for k, v in self.budgets.items()},
        }


# ── defaults ─────────────────────────────────────────────────────────────────
PHASES: tuple[str, ...] = (
    "plan",       # decompose the goal into ordered steps
    "retrieve",   # pull context (slice is L0-only, but summaries can use a model)
    "code",       # write the diff
    "fix",        # repair a rejected diff
    "review",     # critique / sanity triage
    "summarize",  # compress results for the user
)


def default_routes(cfg: CoreConfig) -> dict[str, PhaseRoute]:
    """Default model per phase, cheapest-capable first.

    Rationale lives in the route itself so the trace explains *why* a model was
    chosen, which matters when a per-phase budget is later tightened.
    """
    return {
        "plan": PhaseRoute(
            model=cfg.strong_model,
            fallbacks=(cfg.default_model,),
            temperature=0.1,
            rationale="planning benefits from the strongest reasoning model",
        ),
        # A ReAct episode explores before it commits, so it wants the strong
        # model at a low temperature — same reasoning as ``plan``, but with the
        # loop's own (larger) budget above.
        "react": PhaseRoute(
            model=cfg.strong_model,
            fallbacks=(cfg.default_model,),
            temperature=0.1,
            rationale="exploration needs the strong model; the loop budget bounds it",
        ),
        "retrieve": PhaseRoute(
            model=cfg.cheap_model,
            fallbacks=(),
            temperature=0.0,
            rationale="retrieval is mechanical; the L0 index does the work",
        ),
        "code": PhaseRoute(
            model=cfg.strong_model,
            fallbacks=(cfg.default_model,),
            temperature=0.15,
            rationale="diff authoring needs the strong model",
        ),
        "fix": PhaseRoute(
            model=cfg.strong_model,
            fallbacks=(cfg.default_model,),
            temperature=0.05,
            rationale="repair needs determinism, so the temperature drops",
        ),
        "review": PhaseRoute(
            model=cfg.default_model,
            fallbacks=(),
            temperature=0.2,
            rationale="triage is a lighter task than authoring",
        ),
        "summarize": PhaseRoute(
            model=cfg.cheap_model,
            fallbacks=(),
            temperature=0.3,
            rationale="summarisation is cheap and user-facing",
        ),
    }


def override_budget(budget: PhaseBudget, knob: str, raw: Any) -> PhaseBudget:
    """Return ``budget`` with a single field overridden from a raw env value.

    Unknown knobs are ignored rather than raising: an env var for a field this
    build does not know is not a reason to refuse to start.
    """
    if knob == "max_tokens":
        return replace(budget, max_tokens=_as_int(raw, budget.max_tokens))
    if knob == "max_calls":
        return replace(budget, max_calls=_as_int(raw, budget.max_calls))
    if knob == "max_output_tokens":
        return replace(budget, max_output_tokens=_as_int(raw, budget.max_output_tokens))
    return budget


def default_budgets() -> dict[str, PhaseBudget]:
    """Per-phase budgets.  0 means unlimited.

    The fix phase gets a larger *call* budget than ``code`` on purpose: a repair
    loop is expected to call several times, and a per-call token cap keeps each
    attempt from ballooning.
    """
    return {
        "plan": PhaseBudget(max_tokens=40_000, max_calls=4, max_output_tokens=2_000),
        "retrieve": PhaseBudget(max_tokens=8_000, max_calls=8, max_output_tokens=1_000),
        "code": PhaseBudget(max_tokens=120_000, max_calls=8, max_output_tokens=8_000),
        "fix": PhaseBudget(max_tokens=160_000, max_calls=12, max_output_tokens=8_000),
        "review": PhaseBudget(max_tokens=30_000, max_calls=4, max_output_tokens=1_500),
        "summarize": PhaseBudget(max_tokens=20_000, max_calls=3, max_output_tokens=2_000),
        # A ReAct episode is a LOOP, not one planning call.  It used to bill
        # ``plan`` (4 calls / 40 000 tokens), which a live episode exhausted on
        # its second step because the harness prompt alone is ~1 100 tokens.
        # The call budget is what actually binds here, so it is the one that
        # grows.
        "react": PhaseBudget(max_tokens=200_000, max_calls=24, max_output_tokens=4_000),
    }


__all__ = [
    "CoreConfig",
    "PhaseBudget",
    "PhaseRoute",
    "RetryConfig",
    "BreakerConfig",
    "FixLoopConfig",
    "CacheConfig",
    "SubAgentConfig",
    "ENV_PREFIX",
    "PHASES",
    "default_routes",
    "default_budgets",
    "override_budget",
]

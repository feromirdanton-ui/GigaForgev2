"""End-to-end task pipeline (M4–M5).

One task, twelve nodes, one trace.  The shape follows the architecture
specification but is rearranged where a naive transcription would have produced
a graph that cannot express its own control flow:

    intake → index → slice → cache → plan → act → validate → fix → review
                                                              → settle
                                                              → summarize
                                                              → persist

``plan`` and ``review`` are *policy* stages: the DAG owns their position, order
and trace, while :mod:`gigaforge.core.strategies` owns what they do.  The default
pair — a linear plan and no review — reproduces the pre-strategy behaviour
exactly; ``GIGAFORGE_PLAN_STRATEGY=tot`` and
``GIGAFORGE_REVIEW_STRATEGY=constitutional`` swap in the tree search and the
principle gate.

Two structural notes:

**No conditional edges.**  A DAG executor that supports "run ``fix`` only if
``validate`` rejected" needs either conditional edges or a join node.  The second
is simpler and keeps the executor free of branching logic: ``fix`` always runs
but is a passthrough when the diff was already accepted, and ``settle`` is the
join that picks whichever diff won.  The node still appears in every trace, which
is exactly what you want when reading back why a run did nothing.

**The cache check is its own node.**  Folding it into ``act`` would hide a cache
hit inside a node that sometimes calls a model, and the trace would then be
unable to answer "did this run spend tokens?" without reading model metrics.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .agents import SubAgentRunner
from .breaker import BreakerRegistry
from .cache import CacheManager, hash_text, task_hash
from .cancellation import CancellationToken
from .config import CoreConfig
from .contracts import (
    Clock,
    NodeSpec,
    ProposeResult,
    RunContext,
    SubAgentSpec,
    SystemClock,
    ValidateResult,
)
from .dag import CheckpointStore, DagExecutor, DagResult
from .errors import CoreError, PipelineError
from .fixloop import FixIteration, FixLoop, FixLoopResult
from .metrics import MetricsCollector
from ..harness.diff_parse import normalise_path, split_diff
from .prompt import build_phase_prompt, extract_diff
from .router import ModelRouter
from .state import TaskState
from .strategies import (
    PlanOutcome,
    PlanStrategy,
    ReviewOutcome,
    ReviewStrategy,
    build_plan_strategy,
    build_review_strategy,
)
from .trace import Tracer

log = logging.getLogger(__name__)

PIPELINE_NAME = "task"
PIPELINE_VERSION = "4.1.0"

#: Sentinel recorded when there is no context slice (no index wired in).  Named
#: rather than inlined so the slice node and the tests cannot drift apart.
_NO_SLICE = "no-slice"
_UNSET = object()

NODE_ORDER: tuple[str, ...] = (
    "intake", "index", "slice", "cache", "plan",
    "act", "validate", "fix", "review", "settle", "summarize", "persist",
)


class RetryableFailure(CoreError):
    """A node failure that the executor is allowed to retry."""

    code = "GF-CORE-PIPE-071"
    retryable = True


@dataclass
class PipelineResult:
    """Everything a caller needs to judge a run."""

    # Fields used by TaskPipeline (M4-M5 DAG)
    run_id: str = ""
    goal: str = ""
    status: str = "succeeded"
    accepted: bool = False
    final_diff: str = ""
    changed_files: list[str] = field(default_factory=list)
    summary: str = ""
    plan: list[str] = field(default_factory=list)
    cache_hit: bool = False
    slice_hash: str = ""
    git_sha: str = ""
    tokens: int = 0
    calls: int = 0
    phase_usages: dict[str, Any] = field(default_factory=dict)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    dlq: list[dict[str, Any]] = field(default_factory=list)
    resumed_from: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    error: str = ""
    dag: Optional[DagResult] = None

    # Fields used by multi-agent Pipeline (M4c Agent)
    success: bool = False
    applied_files: list[str] = field(default_factory=list)
    diff: str = ""
    llm_calls: int = 0
    commits: list[str] = field(default_factory=list)
    escalation: Optional[dict[str, Any]] = None
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Did the plan the exploration phase produced reach the executor?  This is
    #: the field that makes N1 a fact a caller can check instead of a review
    #: finding.  It was added to the ledger and then never populated by the
    #: pipeline — the same "field exists, product never reaches it" shape the
    #: field was added to expose.  The live probe caught it: ``plan_used: false``
    #: on a run whose plan had demonstrably been used.
    plan_used: bool = False
    plan_steps_executed: int = 0
    plan_salvaged: bool = False
    #: Cost of this run in integer micro-USD and in USD.  Tokens alone do not
    #: answer what a run cost: the same token count on a different model is a
    #: different bill.
    cost_micros: int = 0
    cost_usd: float = 0.0
    #: Per-phase cost, so a report can say where the money went.
    phase_cost_micros: dict[str, int] = field(default_factory=dict)
    #: Fraction of cache lookups served from cache.  A bare ``cache_hit`` bool
    #: says whether the LAST lookup hit; this says whether the cache works.
    cache_hit_rate: float = 0.0
    cache_hits: int = 0
    cache_lookups: int = 0
    #: The reply, when the goal asked for one.  Empty for a diff-producing run.
    #: This is the field the harness did not have: every run had to end in a
    #: unified diff, so a goal whose correct output is a sentence was rejected
    #: ("diff could not be parsed") or died in the planner ("steps: 0").
    answer: str = ""
    #: URLs the answer's search actually returned.  Kept apart from the reply so
    #: a report can tell "cited what it read" from "wrote a plausible URL".
    answer_citations: list[str] = field(default_factory=list)
    #: Did the episode use ``web_search`` at all?
    answer_searched: bool = False
    #: Things the run could not do and did not fail on.  A silent ``except``
    #: that swallows a broken check makes a run look clean when it was merely
    #: unverified; every such degradation is recorded here instead.
    degraded: list[dict[str, Any]] = field(default_factory=list)
    #: Files whose creation hunk was rewritten into a modification because the
    #: file already existed (see ``harness.diff_repair``).
    diff_repaired: list[str] = field(default_factory=list)
    #: Requirements from the goal that the produced artifact does not satisfy.
    #: Non-empty means the run was rejected by the contract gate, not by the
    #: model's own judgement — the distinction a caller needs to tell "the model
    #: gave up" from "the harness refused to call this done".
    contract_unmet: list[str] = field(default_factory=list)
    #: Fraction of the contract's requirements the artifact satisfies, 0..1.
    #: Computed from the verdict (``met / (met + unmet)``), not typed by hand:
    #: a number that cannot be recomputed is not a metric.  ``None`` when no
    #: contract ran (a code goal with no contract, a run that produced nothing).
    completeness: float | None = None

    def __post_init__(self) -> None:
        if self.success and not self.accepted:
            self.accepted = True
        elif self.accepted and not self.success:
            self.success = True
        if self.diff and not self.final_diff:
            self.final_diff = self.diff
        elif self.final_diff and not self.diff:
            self.diff = self.final_diff
        if self.applied_files and not self.changed_files:
            self.changed_files = list(self.applied_files)
        elif self.changed_files and not self.applied_files:
            self.applied_files = list(self.changed_files)
        if self.llm_calls and not self.calls:
            self.calls = self.llm_calls
        elif self.calls and not self.llm_calls:
            self.llm_calls = self.calls

    @property
    def ok(self) -> bool:
        return (self.status == "succeeded" and self.accepted) or self.success

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status,
            "accepted": self.accepted,
            "changed_files": list(self.changed_files),
            "cache_hit": self.cache_hit,
            "slice_hash": self.slice_hash,
            "git_sha": self.git_sha,
            "tokens": self.tokens,
            "calls": self.calls,
            "phase_usages": self.phase_usages,
            "diff_chars": len(self.final_diff or self.diff or ""),
            "contract_unmet": list(self.contract_unmet),
            "completeness": self.completeness,
            "summary": self.summary,
            # The reply, when the goal asked for one.  ``to_dict`` is what the
            # CLI's ``--json`` and a report are built from, so a field missing
            # here is a field the product never delivers: the answer was
            # produced, accepted, and then dropped by the serialiser.
            "answer": self.answer,
            "answer_citations": list(self.answer_citations),
            "answer_searched": self.answer_searched,
            "plan": list(self.plan),
            "duration_ms": round(self.duration_ms, 3),
            "resumed_from": list(self.resumed_from),
            "dlq": list(self.dlq),
            "error": self.error,
            "nodes": list(self.nodes),
            "success": self.success,
            "applied_files": list(self.applied_files),
            "diff": self.diff,
            "llm_calls": self.llm_calls,
            "commits": list(self.commits),
            "escalation": self.escalation,
            "metrics": self.metrics,
            "cost_micros": self.cost_micros,
            "cost_usd": round(self.cost_usd, 6),
            "phase_cost_micros": dict(self.phase_cost_micros),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "cache_hits": self.cache_hits,
            "cache_lookups": self.cache_lookups,
            "degraded": list(self.degraded),
        }

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return self.to_dict()

    def dict(self) -> dict[str, Any]:
        return self.to_dict()


def _goal_of(ctx: Any) -> str:
    """The goal text of a run, from wherever the context actually keeps it.

    ``RunContext`` has no ``task`` attribute — it never did.  The goal lives on
    ``ctx.state.goal``, and the prefix builder read ``ctx.task``, so both the
    skills block and the task brief raised ``AttributeError`` on every run and
    were swallowed by their own ``except`` clauses.  Measured live: a
    2 742-character prefix where 9 514 were expected, and a document goal that
    invented the system it was supposed to describe.

    One resolver, so the next reader of the goal cannot pick a different
    attribute and reintroduce the same silent failure.
    """
    state = getattr(ctx, "state", None)
    goal = getattr(state, "goal", "") if state is not None else ""
    if goal:
        return str(goal)
    # Older call sites and tests pass a bare object with ``task``.
    return str(getattr(ctx, "task", "") or "")


class TaskPipeline:
    """Builds and runs the task DAG over the client / L0 / firewall layers."""

    def __init__(
        self,
        *,
        config: Optional[CoreConfig] = None,
        client: Any = None,
        store: Any = None,
        firewall: Any = None,
        router: Optional[ModelRouter] = None,
        cache: Optional[CacheManager] = None,
        metrics: Optional[MetricsCollector] = None,
        checkpoints: Optional[CheckpointStore] = None,
        fixloop: Optional[FixLoop] = None,
        breakers: Optional[BreakerRegistry] = None,
        clock: Optional[Clock] = None,
        index: Any = None,
        indexer: Any = None,
        git: Any = None,
        test_runner: Optional[Callable[[str], str]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        plan_strategy: Optional[PlanStrategy] = None,
        review_strategy: Optional[ReviewStrategy] = None,
        memory: Any = None,
        optimizer: Any = None,
    ) -> None:
        self.config = config or CoreConfig()
        self.client = client
        self.store = store
        self.firewall = firewall
        #: Cross-run lessons (``core/memory.py``) and context trimming
        #: (``core/optimize.py``).  Both are optional and both fail open: a memory
        #: or trimming error is telemetry, never a failed run.
        self.memory = memory
        self.optimizer = optimizer
        self.clock: Clock = clock or SystemClock()
        #: Measured system prefixes by run id.  The ReAct runner is built with a
        #: fresh ``TaskState`` and cannot see the pipeline's, so the brief and
        #: the skills are published here for it to pick up.
        self._prefixes: dict[str, Any] = {}
        self.indexer = indexer if indexer is not None else index
        self.index = self.indexer
        self.git = git
        self.test_runner = test_runner
        self._sleep = sleeper or time.sleep
        # Policy stages.  Resolved from the environment when not injected, so a
        # deployment can switch reasoning style without touching call sites.
        self.plan_strategy: PlanStrategy = plan_strategy or build_plan_strategy(
            react_runner_factory=self._build_react_runner
        )
        # An *injected* strategy arrives without the factory, because the
        # composition root builds it with ``build_plan_strategy()`` and has no
        # pipeline to take the factory from.  That made the ReAct layer
        # unreachable on the product path: ``runtime.build_runtime`` always
        # injects a strategy, so the branch above never ran, ``_build_runner``
        # returned ``None``, and every live run silently degraded to the
        # one-shot linear plan while the trace still said ``strategy: react``.
        # Measured live: a "build a cyberpunk site" goal produced a single
        # 2.5 KB page in one model call, with ``react->linear`` in the trace.
        #
        # The factory is rebound *unconditionally*, not only when it is absent.
        # A goal run builds a second, isolated pipeline and hands it the same
        # strategy object; keeping the first pipeline's factory made the
        # isolated run write into the *destination* instead of its workspace,
        # and the publish then failed with "destination changed while the goal
        # was running" — the run had changed it itself.
        setattr(self.plan_strategy, "_runner_factory", self._build_react_runner)
        self.review_strategy: ReviewStrategy = review_strategy or build_review_strategy()

        self._owns_metrics = metrics is None
        self._owns_cache = cache is None
        self._owns_checkpoints = checkpoints is None
        self.metrics = metrics if metrics is not None else MetricsCollector(
            self.config,
            db_path=self.config.db_path,
            clock=self.clock.timestamp,
        )
        self.cache = cache if cache is not None else CacheManager(
            self.config.db_path, self.config, metrics=self.metrics, trace=self.metrics.trace,
            clock=self.clock.timestamp,
        )
        self.checkpoints = checkpoints if checkpoints is not None else CheckpointStore(
            self.config.db_path, clock=self.clock.timestamp
        )
        self.breakers = breakers if breakers is not None else BreakerRegistry(
            self.config.breaker, clock=self.clock.now, metrics=self.metrics,
            trace=self.metrics.trace,
        )
        self.router = router if router is not None else (
            ModelRouter(client, self.config, metrics=self.metrics, trace=self.metrics.trace)
            if client is not None else None
        )
        self.fixloop = fixloop if fixloop is not None else FixLoop(
            config=self.config,
            validator=getattr(firewall, "validator", None) if firewall is not None else None,
            firewall=firewall,
            metrics=self.metrics,
            trace=self.metrics.trace,
            clock=self.clock,
            breaker=self.breakers.get("model"),
            sleep=self._sleep,
        )
        #: The run-level JSONL tracer.  The DAG writes through
        #: ``metrics.trace`` (an event stream); the agent layer
        #: (``PlannerAgent``, ``ReActRunner``) writes through a ``Tracer``
        #: (one file per task).  Both must be the *same* object graph, or a
        #: ReAct plan's tokens land in a file nobody reads.
        self.tracer = Tracer(
            trace_dir=Path(str(getattr(self.config, "trace_path", ".gigaforge/traces/core.jsonl"))).parent
        )
        self._lock = threading.RLock()
        self._active_runs: dict[str, CancellationToken] = {}
        self._pending_reconfiguration: dict[str, Any] = {}
        self._idle_callbacks: list[Callable[[], None]] = []
        self._closed = False

    @property
    def has_active_runs(self) -> bool:
        with self._lock:
            return bool(self._active_runs)

    def cancel(self, run_id: str, reason: str = "cancelled by caller") -> bool:
        """Request cooperative cancellation of one active run."""
        with self._lock:
            token = self._active_runs.get(str(run_id))
        return token.cancel(reason) if token is not None else False

    def when_idle(self, callback: Callable[[], None]) -> None:
        """Run *callback* after active runs drain, or immediately when idle."""
        with self._lock:
            if self._active_runs:
                self._idle_callbacks.append(callback)
                return
        callback()

    def reconfigure(
        self,
        *,
        config: Optional[CoreConfig] = None,
        client: Any = _UNSET,
        plan_strategy: Any = _UNSET,
        review_strategy: Any = _UNSET,
    ) -> None:
        """Update pipeline dependencies through one lifecycle boundary.

        Changes requested during a run are queued and become visible only after
        all active runs finish. This keeps one run from switching models or
        configuration halfway through its DAG.
        """
        changes: dict[str, Any] = {}
        if config is not None:
            changes["config"] = config
        if client is not _UNSET:
            changes["client"] = client
        if plan_strategy is not _UNSET:
            changes["plan_strategy"] = plan_strategy
        if review_strategy is not _UNSET:
            changes["review_strategy"] = review_strategy
        if not changes:
            return
        with self._lock:
            if self._closed:
                raise PipelineError("cannot reconfigure a closed pipeline")
            new_config = changes.get("config")
            if new_config is not None:
                topology_fields = ("repo_root", "db_path", "trace_path", "tenant")
                changed_topology = [
                    field for field in topology_fields
                    if getattr(new_config, field) != getattr(self.config, field)
                ]
                if changed_topology:
                    raise PipelineError(
                        "repository and storage configuration requires rebuilding Runtime",
                        detail={"fields": changed_topology},
                    )
            if self._active_runs:
                self._pending_reconfiguration.update(changes)
            else:
                self._apply_reconfiguration(changes)

    def _apply_reconfiguration(self, changes: Mapping[str, Any]) -> None:
        config = changes.get("config")
        if config is not None:
            self.config = config
            self.fixloop.config = config
            self.fixloop.settings = config.fixloop
            self.cache.config = config
            self.cache.settings = config.cache
            self.cache.enabled = bool(config.cache.enabled)
            self.breakers.reconfigure(config.breaker)
            if self.router is not None:
                self.router.reconfigure(config=config)
        if "client" in changes:
            self.client = changes["client"]
            if self.router is not None:
                self.router.reconfigure(client=self.client)
        if "plan_strategy" in changes:
            self.plan_strategy = changes["plan_strategy"]
            # A strategy swapped in at runtime arrives from
            # ``build_plan_strategy(name)`` with no factory, exactly like the
            # injected one in ``__init__``.  Without this the switch to ``react``
            # would look applied and still plan linearly.
            setattr(self.plan_strategy, "_runner_factory", self._build_react_runner)
        if "review_strategy" in changes:
            self.review_strategy = changes["review_strategy"]

    # ── graph construction ───────────────────────────────────────────────────
    def build(self) -> DagExecutor:
        """Assemble the DAG and validate it."""
        nodes = [
            NodeSpec(
                name="intake",
                fn=self._node_intake,
                requires=("task",),
                provides=("task", "task_normalized", "memory_hits"),
                retries=0,
                parallel_safe=True,
                description="normalise the goal and open the run",
            ),
            NodeSpec(
                name="index",
                fn=self._node_index,
                requires=("task",),
                provides=("git_sha", "index_stats"),
                retries=1,
                idempotent=True,
                parallel_safe=True,
                description="refresh the L0 index incrementally, capture the commit",
            ),
            NodeSpec(
                name="slice",
                fn=self._node_slice,
                requires=("task", "git_sha"),
                provides=("slice", "slice_hash", "cache_key"),
                retries=1,
                idempotent=True,
                description="assemble the context slice from the symbol index (no model)",
            ),
            NodeSpec(
                name="cache",
                fn=self._node_cache,
                requires=("task", "cache_key"),
                provides=("cache_hit", "cached_diff"),
                retries=0,
                description="look up the task hash: normalized task + slice + commit",
            ),
            NodeSpec(
                name="plan",
                fn=self._node_plan,
                requires=("task", "slice", "cache_hit"),
                provides=("plan", "plan_meta"),
                retries=0,
                timeout=(
                    self.config.react_timeout
                    if self.plan_strategy.name == "react" else None
                ),
                description=f"decompose the goal (strategy: {self.plan_strategy.name})",
            ),
            NodeSpec(
                name="act",
                fn=self._node_act,
                # ``cache_hit`` is declared so the executor actually *passes* it:
                # nodes only receive the inputs they require, so reading an
                # undeclared key always yields ``None`` — which made the cache
                # hit invisible here and re-ran the model on every warm lookup.
                requires=("task", "slice", "plan", "cache_hit", "cached_diff"),
                provides=("proposal", "proposal_source"),
                retries=0,
                description="author the diff, or reuse the cached one",
            ),
            NodeSpec(
                name="validate",
                fn=self._node_validate,
                requires=("proposal",),
                provides=("validation",),
                retries=0,
                description="single-pass validation of the proposal",
            ),
            NodeSpec(
                name="fix",
                fn=self._node_fix,
                requires=("proposal", "validation"),
                provides=("fix_report", "fixed_diff"),
                retries=0,
                description="bounded repair loop when the proposal was rejected",
            ),
            NodeSpec(
                name="repair",
                fn=self._node_repair,
                requires=("validation",),
                provides=("repair_report",),
                retries=0,
                critical=False,
                description="deterministic structural repair of generated HTML, before the contract",
            ),
            NodeSpec(
                name="review",
                fn=self._node_review,
                # Reviews the diff that ``fix``/``act`` produced, so it must run
                # after the repair loop and before the join that accepts it.
                requires=("task", "slice", "proposal", "validation", "fixed_diff", "cache_hit"),
                provides=("review_verdict",),
                retries=0,
                critical=False,
                description=f"adjudicate the diff (strategy: {self.review_strategy.name})",
            ),
            NodeSpec(
                name="settle",
                fn=self._node_settle,
                requires=("proposal", "validation", "fix_report", "review_verdict", "repair_report"),
                provides=("final_diff", "accepted", "changed_files",
                          "answer", "answer_citations", "answer_searched"),
                retries=0,
                description="join node: pick the diff that won, then apply the review veto",
            ),
            NodeSpec(
                name="summarize",
                fn=self._node_summarize,
                requires=("task", "final_diff", "cache_hit", "accepted"),
                provides=("summary",),
                retries=0,
                critical=False,
                description="write the user-facing summary (cheap model)",
            ),
            NodeSpec(
                name="persist",
                fn=self._node_persist,
                requires=("final_diff", "accepted", "summary", "cache_key"),
                provides=("persisted",),
                retries=0,
                critical=False,
                description="write the cache entry, metrics and run report",
            ),
        ]
        return DagExecutor(
            nodes,
            config=self.config,
            checkpoints=self.checkpoints,
            metrics=self.metrics,
            trace=self.metrics.trace,
            clock=self.clock,
            pipeline_name=PIPELINE_NAME,
            sleeper=self._sleep,
        )

    # ── public entry points ──────────────────────────────────────────────────
    def run(
        self,
        goal: str,
        *,
        run_id: Optional[str] = None,
        files: Sequence[str] = (),
        symbols: Sequence[str] = (),
        focus_paths: Sequence[str] = (),
        budget_tokens: int = 0,
        use_cache: bool = True,
        dry_run: bool = False,
        use_subagents: bool = False,
        react: bool = False,
        approve: Optional[Callable[[str], bool]] = None,
        resume: bool = False,
        max_fix_iterations: Optional[int] = None,
        conversation_context: str = "",
        cancellation: Optional[CancellationToken] = None,
        worktree: Optional[str | Path] = None,
    ) -> PipelineResult:
        """Run the task DAG end to end.

        Pass a :class:`CancellationToken` to let a caller request a cooperative
        stop; ``cancel(run_id)`` is the runtime-facing convenience API.

        ``worktree`` is the tree this run actually writes to.  A goal run works
        in an isolated copy of the repository, and without this the pipeline
        measured the *destination* while editing the *copy*: the task brief
        reported ``files: 0`` and quoted no source, so a document goal about the
        decision ladder invented a ladder that does not exist.  ``None`` keeps
        the configured root, which is what a plain ``run`` wants.
        """
        started = self.clock.timestamp()
        run_id = run_id or self._new_run_id()
        token = cancellation or CancellationToken()
        with self._lock:
            if self._closed:
                raise PipelineError("cannot run a closed pipeline")
            if not self._active_runs and self._pending_reconfiguration:
                pending = self._pending_reconfiguration
                self._pending_reconfiguration = {}
                self._apply_reconfiguration(pending)
            if run_id in self._active_runs:
                raise PipelineError(f"run {run_id!r} is already active")
            self._active_runs[run_id] = token

        try:
            return self._run_registered(
                goal, run_id=run_id, files=files, symbols=symbols,
                focus_paths=focus_paths, budget_tokens=budget_tokens,
                use_cache=use_cache, dry_run=dry_run, use_subagents=use_subagents,
                react=react, approve=approve, resume=resume,
                max_fix_iterations=max_fix_iterations, cancellation=token,
                conversation_context=conversation_context,
                started_ms=started,
                worktree=worktree,
            )
        finally:
            budget_tracker = getattr(getattr(self, "router", None), "budget", None)
            clear_task_cap = getattr(budget_tracker, "clear_task_cap", None)
            if callable(clear_task_cap):
                clear_task_cap(run_id)
            callbacks: list[Callable[[], None]] = []
            with self._lock:
                self._active_runs.pop(run_id, None)
                if not self._active_runs:
                    if self._pending_reconfiguration:
                        pending = self._pending_reconfiguration
                        self._pending_reconfiguration = {}
                        self._apply_reconfiguration(pending)
                    callbacks, self._idle_callbacks = self._idle_callbacks, []
            for callback in callbacks:
                try:
                    callback()
                except Exception:  # noqa: BLE001 - lifecycle callbacks are best effort
                    self._event("pipeline.idle_callback_error", run_id=run_id)

    def _run_registered(
        self,
        goal: str,
        *,
        run_id: str,
        files: Sequence[str],
        symbols: Sequence[str],
        focus_paths: Sequence[str],
        budget_tokens: int,
        use_cache: bool,
        dry_run: bool,
        use_subagents: bool,
        react: bool,
        approve: Optional[Callable[[str], bool]],
        resume: bool,
        max_fix_iterations: Optional[int],
        conversation_context: str,
        cancellation: CancellationToken,
        started_ms: float,
        worktree: Optional[str | Path] = None,
    ) -> PipelineResult:
        state = TaskState(run_id=run_id, goal=goal)
        conversation_context = str(conversation_context or "").strip()[-8000:]
        if conversation_context:
            state.add_history("user", conversation_context)
        # ``router.reset()`` existed and was never called, so the per-phase
        # call/token counters accumulated across every run in the process.  A
        # goal run invokes ``pipeline.run()`` once per cycle, so from cycle 5
        # onward the planner was refused with "phase 'plan' used its 4 calls"
        # and spent zero tokens: measured live, 4 of 8 cycles were empty
        # (`tokens 0`) and the goal gave up on work it had already done.
        # A run is the unit a phase budget is specified for.
        self.router.reset()
        with self._lock:
            is_only_active_run = len(self._active_runs) == 1
        if is_only_active_run:
            reset_phase_usage = getattr(
                getattr(self.router, "budget", None), "reset_phase_usage", None
            )
            if callable(reset_phase_usage):
                reset_phase_usage()
        if budget_tokens:
            task_budget = getattr(getattr(self.router, "budget", None), "set_task_cap", None)
            if callable(task_budget):
                task_budget(run_id, int(budget_tokens))
        # The active state, so helpers without a RunContext in hand (the
        # ``_degraded`` recorder) can still report what they could not do.
        self._active_state = state
        ctx = RunContext(
            run_id=run_id,
            clock=self.clock,
            metrics=self.metrics,
            trace=self.metrics.trace,
            router=self.router,
            cache=self.cache,
            store=self.store,
            firewall=self.firewall,
            client=self.client,
            state=state,
            cancellation=cancellation,
            deadline=None,
            extras={
                "files": list(files),
                "symbols": list(symbols),
                "focus_paths": list(focus_paths),
                "budget_tokens": int(budget_tokens),
                "use_cache": bool(use_cache),
                "dry_run": bool(dry_run),
                "max_fix_iterations": max_fix_iterations,
                "use_subagents": bool(use_subagents),
                "react": bool(react),
                "approve": approve,
                "conversation_context": conversation_context,
                "worktree": str(worktree) if worktree else "",
            },
        )
        executor = self.build()
        dag = executor.run({"task": goal}, run_id=run_id, ctx=ctx, resume=resume,
                           parallel=bool(getattr(self.config, "parallel_waves", True)))
        return self._collect(run_id, goal, dag, ctx, started_ms=started_ms)

    def resume(self, run_id: str, goal: str = "", **kwargs: Any) -> PipelineResult:
        """Continue a checkpointed run, skipping nodes already succeeded."""
        return self.run(goal, run_id=run_id, resume=True, **kwargs)

    # ── nodes ────────────────────────────────────────────────────────────────
    def _node_intake(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        goal = str(inputs.get("task", "") or ctx.state.goal or "").strip()
        if not goal:
            raise RetryableFailure("the pipeline was started with an empty goal")
        ctx.state.goal = goal
        ctx.state.add_history("user", goal)
        recalled: list[str] = []
        memory = getattr(self, "memory", None)
        if memory is not None:
            # Episodic recall: earlier runs on a similar task are the cheapest
            # context there is — they cost no tokens and carry the lessons the
            # summariser already extracted (what failed, what worked).
            try:
                for hit in memory.retrieve(goal, k=int(ctx.extras.get("memory_k") or 3)):
                    # ``MemoryHit`` wraps the stored outcome; the verdict and the
                    # lesson live on it, not on the hit itself (that mismatch made
                    # every recall raise and silently disable memory).
                    outcome = getattr(hit, "outcome", None)
                    if outcome is None:
                        continue
                    verdict = "ok" if getattr(outcome, "success", False) else "fail"
                    detail = (getattr(outcome, "failed_step", "")
                              or getattr(outcome, "task", ""))
                    recalled.append(f"{verdict}: {detail}"[:240])
            except Exception as exc:  # noqa: BLE001 - memory is an optimisation
                self._event("pipeline.memory_error", run_id=ctx.run_id,
                            error=str(exc)[:200])
        if recalled:
            ctx.extras["recalled"] = recalled
            self._event("pipeline.memory_recall", run_id=ctx.run_id, hits=len(recalled))
        # Self-debug: the previous run's failure on this task family is the best
        # repair hint the model can get, and it costs no tokens to recall.
        lessons = getattr(self, "memory", None)
        if lessons is not None and not recalled:
            try:
                failures = [h for h in lessons.retrieve(goal, k=2)
                            if not getattr(getattr(h, "outcome", None), "success", True)]
                if failures:
                    ctx.extras["recalled"] = [
                        "previous attempt failed: "
                        + (str(getattr(getattr(h, "outcome", None), "failed_step", "")
                               or "unknown"))
                        for h in failures
                    ]
                    self._event("pipeline.memory_failures", run_id=ctx.run_id,
                                hits=len(failures))
            except Exception as exc:  # noqa: BLE001 - memory is an optimisation
                # Not fatal, but not silent either: a run that could not recall
                # its own failures is a run that may repeat them, and the report
                # must be able to say so.
                ctx.state.note_degraded(
                    what="memory_recall", why=f"{type(exc).__name__}: {exc}",
                    where="intake",
                )
                self._event("pipeline.memory_recall_failed", run_id=ctx.run_id,
                            error=str(exc)[:200])
        self._build_prefix(ctx)
        return {"task": goal, "task_normalized": " ".join(goal.split()).lower(),
                "memory_hits": len(recalled)}

    def _build_prefix(self, ctx: RunContext) -> None:
        """Assemble the stable system prefix once, at the start of the run.

        ``build_system_prefix`` existed and was exported, and nothing called it:
        ``state.prefix`` stayed ``None``, so every phase fell back to
        ``DEFAULT_PERSONA``.  Three things were lost with it — the repo map never
        reached the model, the tool contract was never in the system message, and
        the prefix hash was empty, so GigaChat's prefix cache could not hit.

        Built here rather than per phase because the whole point is that the
        bytes are identical across every call in the run.
        """
        from .state import build_system_prefix

        if ctx.state.prefix is not None:
            return
        repo_map = ""
        try:
            indexer = self.indexer
            discover = getattr(indexer, "discover", None)
            if callable(discover):
                files = sorted(str(p) for p in list(discover())[:60])
                if files:
                    repo_map = "\n".join(files)
        except Exception as exc:  # noqa: BLE001 - a repo map is an optimisation
            self._event("pipeline.prefix_error", run_id=ctx.run_id, error=str(exc)[:200])
        tool_contract = ""
        try:
            from .tool_contract import build_contract_text

            tool_contract = build_contract_text(
                ["grep", "read_structure", "run_tests", "edit_function"]
            )
        except Exception as exc:  # noqa: BLE001 - the contract is advisory
            tool_contract = ""
            ctx.state.note_degraded(
                what="tool_contract", why=f"{type(exc).__name__}: {exc}", where="prefix"
            )
        # Skills are procedural knowledge for THIS task class.  Selecting them
        # here — once, from the classifier's verdict — is what keeps a code-fix
        # run from paying for the page-section procedure.  A model call to
        # decide which skill to load would be a call to decide what to tell the
        # model, and the classifier already knows the answer.
        skills_text = ""
        try:
            from gigaforge.skills import render_skills, select_skills

            kind = ""
            try:
                kind = _task_kind_for(self, ctx).kind
            except Exception:  # noqa: BLE001 - a prefix must not fail a run
                kind = ""
            chosen = select_skills(
                kind=kind, goal=_goal_of(ctx), budget_tokens=1200
            )
            skills_text = render_skills(chosen)
            self._event(
                "pipeline.skills",
                run_id=ctx.run_id,
                task_kind=kind,
                selected=[s.name for s in chosen],
                tokens=sum(s.token_cost for s in chosen),
            )
        except Exception as exc:  # noqa: BLE001 - skills are an optimisation
            ctx.state.note_degraded(
                what="skills", why=f"{type(exc).__name__}: {exc}", where="prefix"
            )
        # The brief is what the harness MEASURED about this task: which paths
        # the goal names and whether they exist, the repository's shape, the
        # test command, and whether the goal forbids changes.  Every line is a
        # fact, so the model does not spend tokens guessing at the tree — and a
        # wrong guess about the tree is the most expensive kind, because it
        # produces an edit to a file that is not there.
        brief_text = ""
        try:
            from gigaforge.harness.brief import build_brief, render_brief

            kind_obj = None
            try:
                kind_obj = _task_kind_for(self, ctx)
            except Exception:  # noqa: BLE001 - a brief must not fail a run
                kind_obj = None
            brief = build_brief(
                _goal_of(ctx),
                # ``RunContext`` has no ``worktree`` attribute — it never did.
                # The brief was therefore always built against ``None``, which
                # means "no tree": no named-path resolution, no file count, and
                # no grounding.  Measured live: a document goal about the
                # decision ladder produced a confident, entirely invented
                # description of it, and the trace showed a 2 742-character
                # prefix where the brief and the skills should have been.
                # ``_repo_root()`` is the one resolver the rest of the pipeline
                # uses, so the brief now sees the same tree the run writes to.
                repo_root=self._brief_root(ctx),
                task_kind=getattr(kind_obj, "kind", "") or "",
                task_evidence=getattr(kind_obj, "evidence", "") or "",
            )
            brief_text = render_brief(brief)
            self._event(
                "pipeline.brief",
                run_id=ctx.run_id,
                task_kind=brief.task_kind,
                named_paths=len(brief.named_paths),
                files=brief.file_count,
                test_command=brief.test_command,
                read_only=brief.read_only,
            )
        except Exception as exc:  # noqa: BLE001 - the brief is an optimisation
            ctx.state.note_degraded(
                what="brief", why=f"{type(exc).__name__}: {exc}", where="prefix"
            )
        ctx.state.prefix = build_system_prefix(
            repo_map=repo_map, tool_contract=tool_contract, skills=skills_text,
            extra={"brief": brief_text} if brief_text else None,
        )
        # The ReAct runner is built with a fresh ``TaskState`` and cannot see
        # this one, so the prefix is published by run id for it to pick up.
        # ``getattr`` rather than ``self._prefixes``: a pipeline assembled
        # without ``__init__`` (tests, and any future partial construction)
        # must still build a prefix rather than fail on a missing attribute.
        registry = getattr(self, "_prefixes", None)
        if registry is None:
            registry = self._prefixes = {}
        registry[ctx.run_id] = ctx.state.prefix
        self._event(
            "pipeline.prefix",
            run_id=ctx.run_id,
            prefix_hash=ctx.state.prefix.hash[:12],
            chars=len(ctx.state.prefix.text),
        )

    def _node_index(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        git_sha = ""
        stats: dict[str, Any] = {}
        if self.git is not None:
            try:
                git_sha = str(self.git.head() or "")
            except Exception as exc:  # noqa: BLE001 - the commit is best effort
                stats["git_error"] = str(exc)[:300]
        if self.indexer is not None:
            try:
                result = self.indexer.index_incremental()
                stats = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
            except Exception as exc:  # noqa: BLE001 - a stale index still works
                stats = {"index_error": str(exc)[:300]}
                self._event("pipeline.index_error", run_id=ctx.run_id, error=str(exc)[:300])
        # ``"no-git"`` rather than ``""`` is stored *and* published so the cache
        # lookup and the cache write key the same bytes.  An empty string here
        # against a placeholder in the artifact produced two different hashes for
        # the same run, which silently disabled the cache.
        ctx.state.git_sha = git_sha or "no-git"
        ctx.extras["git_sha"] = ctx.state.git_sha
        if git_sha:
            self.cache.note_git_sha(git_sha)
        return {"git_sha": ctx.state.git_sha, "index_stats": stats}

    def _node_slice(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.firewall is None:
            # No index was wired in, so the slice is empty.  It is a real object
            # rather than ``None`` on purpose: the executor reads ``None`` as
            # "the upstream node did not run", and a legitimately empty slice must
            # not look like a wiring bug.
            empty = _NullSlice()
            # No slice means no context hash; fold in a worktree fingerprint so a
            # changed repository cannot be served the previous run's diff.
            key = task_hash(ctx.state.goal, empty,
                            f"{ctx.state.git_sha}|{self._repo_fingerprint()}"
                            f"|{self._cache_scope()}")
            ctx.state.slice_hash = _NO_SLICE
            return {"slice": empty, "slice_hash": _NO_SLICE, "cache_key": key}
        files = tuple(ctx.extras.get("files") or ())
        symbols = tuple(ctx.extras.get("symbols") or ())
        budget = int(ctx.extras.get("budget_tokens") or 0) or None
        focus = tuple(ctx.extras.get("focus_paths") or ())

        def attempt(file_seeds: Sequence[str]) -> Any:
            return self.firewall.slice(
                ctx.state.goal,
                files=tuple(file_seeds),
                symbols=symbols,
                budget_tokens=budget,
                focus_paths=focus,
            )

        slice_obj: Any = None
        try:
            slice_obj = attempt(files)
        except Exception as initial_error:  # noqa: BLE001 - a wider retry beats an empty slice
            slice_error = initial_error
            # A goal like "improve error handling" names no symbol, so keyword
            # seeding finds nothing and the slicer (correctly) refuses to invent a
            # context.  Retrying against the highest-ranked files turns that dead
            # end into a usable run: the model still sees the code it must reason
            # about, and the cache key covers exactly those files.
            fallback_files = self._fallback_slice_files()
            if fallback_files:
                try:
                    slice_obj = attempt(fallback_files)
                    ctx.state.add_note(
                        f"slice: keyword seeding found nothing; used {len(fallback_files)} "
                        f"ranked file(s) instead"
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    slice_error = retry_exc
                    slice_obj = None
            if slice_obj is None:
                self._event(
                    "pipeline.slice_empty", run_id=ctx.run_id, error=str(slice_error)[:300]
                )
                empty = _NullSlice()
                key = task_hash(ctx.state.goal, empty,
                                f"{ctx.state.git_sha}|{self._cache_scope()}")
                ctx.state.slice_hash = _NO_SLICE
                return {"slice": empty, "slice_hash": _NO_SLICE, "cache_key": key}

        key = task_hash(ctx.state.goal, slice_obj,
                        f"{ctx.state.git_sha}|{self._cache_scope()}")
        ctx.state.slice_hash = key[:16]
        ctx.state.artifacts["slice_hash"] = ctx.state.slice_hash
        # The key is computed exactly once, here, and then carried.  Recomputing
        # it downstream from a different set of inputs is how the read and the
        # write ended up on two different hashes — and a cache that never hits
        # looks exactly like a cache that is simply cold.
        ctx.extras["slice_obj"] = slice_obj
        return {"slice": slice_obj, "slice_hash": ctx.state.slice_hash, "cache_key": key}

    def _cache_scope(self) -> str:
        """A stable id for the tree this pipeline writes to.

        The cache key was ``task_hash(goal, slice, git_sha)``, and a sub-agent's
        worktree is a *copy* of the repository at the same commit — so two
        children with the same goal text produced the same key and one could be
        served the other's diff.  Measured: two children with identical goals
        hashed identically.

        The scope is the resolved repo root, hashed.  It is stable across runs
        of the same tree (so the cache still hits where it should) and distinct
        between worktrees (so it cannot cross-contaminate).
        """
        root = str(getattr(self.config, "repo_root", "") or "")
        if not root:
            return ""
        try:
            resolved = str(Path(root).resolve())
        except OSError:  # pragma: no cover - an unresolvable root is used as given
            resolved = root
        return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]

    def _fallback_slice_files(self, limit: int = 3) -> list[str]:
        """Seeds for a vague goal: ranked files, else the filesystem itself.

        The repo map is built from indexed *symbols*, so a repository whose files
        the indexer cannot parse (HTML, CSS, config) produced an empty ranking and
        therefore an empty slice — which is how a real ``index.html`` task reached
        the coder with no context at all.  When the map is empty, fall back to the
        discoverable file list, preferring names that match the goal's words.
        """
        store = getattr(self.firewall, "store", None)
        paths: list[str] = []
        if store is not None:
            try:
                from gigaforge.l0.repo_map import build_repo_map

                repo_map = build_repo_map(store)
                for symbol in getattr(repo_map, "ranked", []) or []:
                    path = getattr(symbol, "path", "")
                    if path and path not in paths:
                        paths.append(path)
                    if len(paths) >= limit:
                        return paths
            except Exception as exc:  # noqa: BLE001 - an unbuilt index is not fatal
                # Not fatal, but not silent either: a repo map that never built
                # is why a run picks the wrong files, and the report must be able
                # to say so instead of showing an empty slice with no reason.
                self._degraded("repo_map", exc, "context")
        if paths:
            return paths[:limit]
        try:
            files = list(self.indexer.discover()) if self.indexer is not None else []
        except Exception as exc:  # noqa: BLE001
            self._degraded("indexer_discover", exc, "context")
            return []
        words = {w.lower() for w in re.findall(r"[A-Za-z0-9_]{3,}",
                                               getattr(self, "_goal_hint", "") or "")}
        def prefer(path: str) -> tuple[int, int, str]:
            stem = Path(path).stem.lower()
            return (0 if stem in words else 1, len(path), path)
        return sorted(files, key=prefer)[:limit]
    def _node_cache(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        if not ctx.extras.get("use_cache", True):
            return {"cache_hit": False, "cached_diff": ""}
        # Taken verbatim from the slice node — the only place the key is built.
        key = str(inputs.get("cache_key") or "")
        if not key:
            return {"cache_hit": False, "cached_diff": ""}
        entry = self.cache.get(key)
        # Every lookup is counted, hit or miss.  A bare ``cache_hit`` bool says
        # whether the LAST lookup hit; the rate says whether the cache earns its
        # keep, which is the number a report needs.
        ctx.state.note_cache(hit=entry is not None)
        if entry is not None and isinstance(entry.value, Mapping):
            diff = str(entry.value.get("diff", ""))
            if diff:
                ctx.state.bump("cache_hit")
                # Stashed so the summarize node can return the stored summary: a
                # full cache hit should cost zero model calls, not "zero except the
                # cheap one".
                ctx.extras["cached_value"] = dict(entry.value)
                self._event("pipeline.cache_hit", run_id=ctx.run_id, key=key[:16])
                return {"cache_hit": True, "cached_diff": diff}
        return {"cache_hit": False, "cached_diff": ""}

    def _independent_subagent_files(self, goal: str) -> list[str]:
        """Find explicit independent code targets that need no model planning."""
        from gigaforge.firewall.slicer_keywords import Extractor
        from gigaforge.harness.task_kind import classify_task

        from .acceptance import allocate_work

        root = self._repo_root()
        if not classify_task(goal, root).is_code:
            return []
        config = getattr(getattr(self, "firewall", None), "config", None)
        paths = Extractor(config).extract(goal).paths
        named = [
            rel for raw in dict.fromkeys(paths)
            if (rel := _workspace_relative_path(raw, root))
            and (root / rel).is_file()
        ]
        if not 2 <= len(named) <= 3:
            return []
        for rel in named:
            lowered = rel.replace("\\", "/").lower()
            name = lowered.rsplit("/", 1)[-1]
            if (
                name.startswith("test_")
                or name.endswith("_test.py")
                or "/tests/" in f"/{lowered}"
                or "/test/" in f"/{lowered}"
            ):
                return []
        return named if len(allocate_work(goal, named, coordinated=False)) >= 2 else []

    def _subagent_symbol_owners(
        self, goal: str, paths: Sequence[str]
    ) -> dict[str, str]:
        """Associate explicitly requested functions with their related module."""
        import ast

        requested = list(dict.fromkeys(re.findall(
            r"\b(?:public\s+)?(?:function|method)\s+([A-Za-z_]\w*)\s*\(",
            goal,
            re.I,
        )))
        if not requested:
            return {}

        root = self._repo_root()
        module_functions: dict[str, list[str]] = {}
        for rel in paths:
            try:
                tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, SyntaxError):
                continue
            module_functions[rel] = [
                node.name for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not node.name.startswith("_")
            ]

        def tokens(name: str) -> set[str]:
            return {
                part.lower()
                for part in re.split(r"[^A-Za-z0-9]+|_", name)
                if part and part.lower() not in {"to", "from", "and"}
            }

        owners: dict[str, str] = {}
        for symbol in requested:
            wanted = tokens(symbol)
            scores = []
            for path, existing in module_functions.items():
                score = max(
                    (
                        len(wanted & tokens(name)) / max(1, len(wanted | tokens(name)))
                        for name in existing
                        if name != symbol
                    ),
                    default=0.0,
                )
                if score > 0:
                    scores.append((score, path))
            scores.sort(key=lambda item: (-item[0], item[1]))
            if scores and scores[0][0] >= 0.5 and (
                len(scores) == 1 or scores[0][0] > scores[1][0]
            ):
                owners[symbol] = scores[0][1]
        return owners

    def _prefix_for(self, run_id: str) -> Any:
        """The measured prefix for *run_id*, or ``None`` when none was built.

        A missing prefix is not an error: a run that never reached intake has
        nothing measured to deliver, and the ReAct loop composes a prompt
        without it.
        """
        if not run_id:
            return None
        return (getattr(self, "_prefixes", None) or {}).get(run_id)

    def _build_react_runner(
        self,
        goal: str = "",
        *,
        run_id: str = "",
        max_steps: int | None = None,
    ) -> Any:
        """Build a :class:`ReActRunner` from this pipeline's own collaborators.

        This is the seam that makes the ReAct layer reachable from the product.
        The runner is constructed lazily, only when ``GIGAFORGE_PLAN_STRATEGY``
        selects ``react``, so a default run pays nothing for it.

        **The runner shares the run's accounting.**  It is handed the router's
        own :class:`BudgetTracker` and the pipeline's :class:`Tracer`, and its
        ``TaskState`` carries the *caller's* ``run_id``.  A runner built with a
        fresh ``BudgetTracker()`` would spend tokens the budget never sees —
        exactly the bypass the contract DAG exists to prevent.

        Returns ``None`` when the pipeline cannot supply what the runner needs
        (no client, no router).  :class:`ReActPlan` then degrades to the
        one-shot plan instead of failing the run.

        An *import* failure is deliberately **not** caught: a missing module is
        a build defect, and swallowing it turns a loud error into a silent
        feature that never runs.
        """
        if self.client is None or self.router is None:
            return None

        from gigaforge.agents.react import ReActContext, ReActRunner, ToolRegistry
        from gigaforge.core.task_state import TaskState
        from gigaforge.sandbox.executor import SandboxExecutor

        root = Path(str(getattr(self.config, "repo_root", ".") or ".")).resolve()
        # ``SandboxExecutor`` already defaults to ``ALLOWED``; passing it
        # explicitly documents the contract at the call site.
        sandbox = SandboxExecutor(worktree=root)
        # ``ToolRegistry()`` starts EMPTY \u2014 the built-ins are installed by
        # ``install_defaults``.  Without this call the runner had no tools at
        # all: the model asked for ``grep``, the registry returned ``None``, and
        # every episode burned its whole step budget on "unknown tool" and
        # finished with an empty plan.  The ReAct layer was reachable but inert.
        registry = ToolRegistry()
        registry.install_defaults(sandbox=sandbox)
        runner = ReActRunner(
            self.client,
            self.router,
            self.router.budget,
            self.tracer,
            registry=registry,
            max_steps=max_steps,
        )
        # The strategy adapter reads these two attributes off the runner.
        # ``TaskState`` keys its trace and budget accounting on ``task_id``, so
        # the DAG's run id goes there — a synthetic id would split one run's
        # trace across two files.
        runner.state = TaskState(goal=goal, task_id=run_id or "react-plan")
        # The runner's state is a FRESH ``TaskState``, so its ``prefix`` is
        # ``None`` — and the measured brief, the skills and the repo map live on
        # the pipeline's own state prefix.  Measured live: a document goal about
        # the decision ladder produced a confident, entirely invented
        # description of it while a 17 324-character prefix quoting
        # ``core/ladder.py`` was built and never delivered.  The tokens were
        # paid for and thrown away.
        #
        # The prefix is looked up by run id rather than passed in because the
        # strategy factories that build this runner are constructed before the
        # run exists; the registry is filled by ``_build_prefix`` at intake.
        prefix = self._prefix_for(run_id)
        if prefix is not None:
            runner.state.prefix = prefix
        runner.ctx = ReActContext(
            repo_root=root,
            worktree=root,
            index=self.index,
            sandbox=sandbox,
        )
        return runner

    def _task_kind(self, ctx: RunContext) -> Any:
        """The run's task kind, classified **once** and cached on the context.

        ``classify_task`` was called from two places, both under conditions
        (``_subagent_diff`` after an early return, ``_node_act`` only when
        nothing was prefetched), so ``ctx.extras["task_kind"]`` could stay unset
        on an ordinary run — and ``_check_render`` gates on it, which meant the
        browser check silently did not run.  One call, at the first node that
        needs it, and every later reader gets the same verdict.
        """
        cached = ctx.extras.get("task_kind_obj")
        if cached is not None:
            return cached
        from gigaforge.harness.task_kind import classify_task

        kind = classify_task(_original_goal(ctx.state.goal), self._repo_root())
        ctx.extras["task_kind_obj"] = kind
        ctx.extras["task_kind"] = kind.to_dict()
        self._event("pipeline.task_kind", run_id=ctx.run_id,
                    task_kind=kind.kind, evidence=kind.evidence[:300])
        return kind

    def _node_plan(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        if inputs.get("cache_hit"):
            # The cached entry already holds the diff this exact task + slice +
            # commit produced, so re-planning would spend the most expensive phase
            # to arrive at the same place.
            self._event("pipeline.plan_cached", run_id=ctx.run_id)
            return {
                "plan": [],
                "plan_meta": {"strategy": self.plan_strategy.name, "cached": True, "steps": 0},
            }
        # An answer goal has no executable steps to plan: its output is a reply.
        # The planner demands a step with a target file, so it returned
        # ``steps: 0`` and the run died here — measured live on "What is 2+2?
        # Answer in chat, do not change any files."  The plan for a reply is the
        # reply itself, produced by the episode in ``act``.
        if _task_kind_for(self, ctx).is_answer:
            self._event("pipeline.plan_answer", run_id=ctx.run_id,
                        reason="the goal asks for a reply, not a change to the tree")
            return {
                "plan": ["Answer the question from the repository and the web."],
                "plan_meta": {"strategy": "answer", "steps": 1, "answer": True},
            }
        # An artifact goal has no file to target either: the page does not exist
        # yet, and the ReAct planner demands a step naming an existing path, so
        # it returned ``steps: 0`` and the run died in ``plan``.  Measured live
        # on the site goal: cycle 1 failed the contract on a section name, then
        # cycles 2 and 3 failed at ``plan`` with ``{'steps': 0}`` and the goal
        # stopped with "the same output and failure repeated" — the second
        # failure was the harness's own, not the model's.  The plan for a page
        # is its sections, which ``plan_generation`` already decides.
        kind = _task_kind_for(self, ctx)
        if kind.is_artifact and not kind.is_code:
            try:
                from gigaforge.harness.generation import plan_generation

                plan_obj = plan_generation(ctx.state.goal)
                sections = list(getattr(plan_obj, "kinds", lambda: [])() or [])
                path = str(getattr(plan_obj, "path", "") or "index.html")
            except Exception as exc:  # noqa: BLE001 - fall back to the planner
                self._degraded("generation_plan", exc, "plan")
                sections, path = [], "index.html"
            if sections:
                self._event(
                    "pipeline.plan_artifact",
                    run_id=ctx.run_id,
                    sections=sections,
                    path=path,
                    reason="a page decomposes into sections, not file edits",
                )
                return {
                    "plan": [
                        f"Build {path} section by section: " + ", ".join(sections)
                    ],
                    "plan_meta": {
                        "strategy": "artifact",
                        "steps": len(sections),
                        "sections": sections,
                        "path": path,
                        "artifact": True,
                    },
                }
        if self.router is None:
            raise PipelineError("no model client is wired into the pipeline")
        slice_obj = inputs.get("slice")
        slice_text = slice_obj.render() if slice_obj is not None else ""

        budget = int(ctx.extras.get("budget_tokens") or 0)
        budget_remaining = max(0, budget - ctx.state.total_tokens()) if budget else 0

        # Tell a stateful strategy which run it is planning for, so the ReAct
        # runner's TaskState and trace carry the DAG's own run id.
        binder = getattr(self.plan_strategy, "bind_run", None)
        if callable(binder):
            binder(ctx.run_id)

        # A strategy that can run under the harness does so: the harness owns
        # the retry policy, the overflow recovery and the ledger.  A strategy
        # that cannot (linear, ToT) keeps the plain call — the harness is for
        # episodes, and those strategies have none.
        harness_plan = getattr(self.plan_strategy, "plan_under_harness", None)
        ledger = None
        subagent_paths: list[str] = []
        if ctx.extras.get("use_subagents"):
            try:
                subagent_paths = self._independent_subagent_files(ctx.state.goal)
            except Exception as exc:  # noqa: BLE001 - fall back to the read-only planner
                self._event(
                    "pipeline.subagent_plan_error",
                    run_id=ctx.run_id,
                    error=str(exc)[:300],
                )
        if subagent_paths:
            steps = [
                "Delegate independent implementation work for "
                + ", ".join(subagent_paths)
                + " to isolated subagents and verify the merged changes."
            ]
            outcome = PlanOutcome(
                steps=steps,
                strategy="subagents",
                score=1.0,
            )
            self._event(
                "pipeline.subagent_plan",
                run_id=ctx.run_id,
                files=subagent_paths,
                reason="explicit independent code targets do not need model planning",
            )
        elif callable(harness_plan):
            planning_goal = ctx.state.goal
            conversation_context = str(ctx.extras.get("conversation_context") or "").strip()
            if conversation_context:
                planning_goal = (
                    f"{planning_goal}\n\nRelevant earlier conversation:\n"
                    f"{conversation_context}"
                )
            if ctx.extras.get("use_subagents"):
                planning_goal += (
                    "\n\nSubagent planning constraint: do not read or edit the target "
                    "files in this stage; isolated child agents will inspect their "
                    "assignments. Finish immediately with a concise executable plan, "
                    "using one targeted coder step per named file when the work is "
                    "independent. If it is not eligible for delegation, the normal "
                    "implementation stage will handle it. Do not write or edit files."
                )
            path_hints = list(
                dict.fromkeys(
                    str(path).strip().replace("\\", "/")
                    for path in (
                        *(ctx.extras.get("files") or ()),
                        *(ctx.extras.get("focus_paths") or ()),
                    )
                    if str(path).strip()
                )
            )
            if path_hints:
                planning_goal += (
                    "\n\nUser-designated files to inspect and modify "
                    "(use these paths directly; do not search for them):\n"
                    + "\n".join(f"- {path}" for path in path_hints)
                )
            outcome, ledger = harness_plan(
                planning_goal,
                slice_text,
                ask=lambda phase, prompt, temperature=None: self._ask(
                    phase, prompt, ctx, temperature=temperature
                ),
                parse=_parse_plan,
                cancellation=getattr(ctx, "cancellation", None),
                pipeline_state=ctx.state,
                fallback_paths=path_hints,
                read_only=bool(ctx.extras.get("use_subagents")),
            )
        else:
            outcome = self.plan_strategy.plan(
                ctx.state.goal,
                slice_text,
                ask=lambda phase, prompt, temperature=None: self._ask(
                    phase, prompt, ctx, temperature=temperature
                ),
                parse=_parse_plan,
                budget_remaining=budget_remaining,
            )
        ctx.state.set_plan(outcome.steps)
        # The plan is not just a telemetry line: the coder reads it.  Keep the
        # structured form and the localisation on the run context, because
        # ``_node_act`` is where they are consumed and the DAG only passes
        # declared inputs between nodes.
        ctx.extras["plan_structured"] = list(getattr(outcome, "structured", None) or [])
        # Files the exploration episode wrote itself.  The coder is asked for a
        # diff, so a file that already exists on disk is invisible to it unless
        # the harness turns it into one — see ``_episode_diff``.
        ctx.extras["episode_written_files"] = list(
            getattr(outcome, "written_files", None) or []
        )
        ctx.extras["localization"] = dict(getattr(outcome, "localization", None) or {})
        # Record the plan's fate where the result assembly can read it.  A plan
        # that came from the ReAct episode is "used" only if it produced steps;
        # a fallback to the linear plan is not.
        ctx.extras["plan_used"] = bool(outcome.steps) and outcome.strategy != "linear"
        ctx.extras["plan_steps_executed"] = len(outcome.steps or [])
        ctx.extras["plan_salvaged"] = bool(
            getattr(outcome, "salvaged", False)
            or (
                getattr(outcome, "structured", None) is None
                and getattr(outcome, "steps", None)
                and outcome.strategy != "linear"
            )
        )
        if ledger is not None:
            ctx.state.record_phase_usage(
                "react",
                {
                    "prompt_tokens": int(getattr(ledger, "tokens_in", 0) or 0),
                    "completion_tokens": int(getattr(ledger, "tokens_out", 0) or 0),
                    "total_tokens": (
                        int(getattr(ledger, "tokens_in", 0) or 0)
                        + int(getattr(ledger, "tokens_out", 0) or 0)
                    ),
                    "calls": int(getattr(ledger, "llm_calls", 0) or 0),
                    "cost_micros": int(getattr(ledger, "cost_micros", 0) or 0),
                },
            )
        plan = [s.description for s in ctx.state.plan]
        self._event(
            "pipeline.plan",
            run_id=ctx.run_id,
            steps=len(plan),
            strategy=outcome.strategy,
            score=outcome.score,
            judge=outcome.judge_used,
            compactions=getattr(ledger, "compactions", 0) if ledger else 0,
            retries=getattr(ledger, "retries", 0) if ledger else 0,
        )
        if ledger is not None:
            # The ledger is the run's cost record; keep it next to the trace so
            # a report can read one file instead of grepping three.
            try:
                ledger.write(Path(str(getattr(self.config, "trace_path", ".gigaforge/traces/core.jsonl"))).parent / f"{ctx.run_id}.ledger.json")
            except Exception as exc:  # noqa: BLE001 - reporting must not fail a run
                log.debug("could not write the run ledger: %s", exc)
        return {"plan": plan, "plan_meta": outcome.to_dict()}

    def _plan_block(self, ctx: RunContext, inputs: Mapping[str, Any]) -> str:
        """Render the plan and its localisation for the coder's prompt.

        Two things the coder needs and did not have:

        * **The plan.**  A list of the steps the episode decided on.  Without it
          the model re-derives the approach from the goal, which is exactly the
          work the episode already did.
        * **The localisation.**  ``target_function`` / ``target_file``.  This is
          the difference between "change the code" and "change ``add`` in
          ``math_utils.py``", and it is the part that stops the model guessing
          a file.

        Returns an empty string when there is nothing to say, so a linear plan
        with no localisation produces byte-identical prompts to before.
        """
        lines: list[str] = []
        plan = inputs.get("plan") or [s.description for s in ctx.state.plan]
        plan = [str(line).strip() for line in plan if str(line).strip()]
        if plan:
            lines.append("## Plan (from the exploration phase)")
            lines.extend(f"{i + 1}. {line}" for i, line in enumerate(plan))

        loc = ctx.extras.get("localization") or {}
        target_function = str(loc.get("target_function") or "").strip()
        target_file = str(loc.get("target_file") or "").strip()
        if target_function or target_file:
            lines.append("## Localization")
            if target_file:
                lines.append(f"- file: {target_file}")
            if target_function:
                lines.append(f"- symbol: {target_function}")
            lines.append(
                "Change exactly this target; do not widen the edit to other "
                "files or symbols unless the plan says so."
            )
        return "\n".join(lines)

    def _file_context(self, goal: str, *, limit: int = 4, max_chars: int = 12000) -> str:
        """The real text of the files a goal names — for the coder's prompt.

        A symbol slice carries signatures and excerpts; a unified diff needs
        *verbatim lines*.  Without them the model invents context (a live run
        emitted ``-from . import add`` for a file that says
        ``from math_utils import add``), the hunk cannot anchor, and the repair loop
        has nothing valid to work from.

        Three additions over "read the named file":

        * **absolute paths** (``M:\\YESTER\\index.html``) are resolved — a path that
          points outside the repository contributes read-only context;
        * **siblings with the same stem** are included (``index.html`` pulls in
          ``index1.html`` / ``index2.html``), which is exactly the "create index2
          and fix index1" request;
        * **the directory listing** goes with the first file, so the model can see
          which files exist before inventing one.
        """
        try:
            from gigaforge.firewall.slicer_keywords import Extractor

            config = getattr(self.firewall, "config", None)
            paths = Extractor(config).extract(goal).paths
        except Exception as exc:  # noqa: BLE001 - extra context must never fail a run
            self._degraded("slicer_keywords", exc, "context")
            return ""
        # The repository root comes from the pipeline's own config, NOT from the
        # firewall.  ``getattr(self.firewall, "config", None)`` is ``None``
        # whenever no firewall is wired in — which is the case for the plain
        # ``TaskPipeline(config=…)`` the live probe and the integration test
        # build — and the root then fell back to ``"."``, i.e. the *process's*
        # working directory.  Measured: a goal naming ``math_utils.py`` in a
        # temp repository produced "files mentioned but not found:
        # math_utils.py — create them at exactly these relative paths", so the
        # coder was told to CREATE a file that already existed and never saw its
        # text.  It then invented the file's contents, the hunk could not
        # anchor, and the run reported a diff that does not apply.
        root = Path(str(getattr(self.config, "repo_root", ".") or ".")).resolve()
        candidates: list[Path] = []
        unreadable_absolute: list[str] = []

        for raw in dict.fromkeys(paths):
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                candidate = candidate.resolve()
            except OSError:
                continue
            if candidate.is_file():
                candidates.append(candidate)
                # siblings that share the stem (index.html -> index1.html, ...)
                stem = candidate.stem.rstrip("0123456789")
                if len(stem) >= 3:
                    try:
                        for sibling in sorted(candidate.parent.iterdir()):
                            if (sibling.is_file() and sibling != candidate
                                    and sibling.suffix == candidate.suffix
                                    and sibling.stem.startswith(stem)):
                                candidates.append(sibling)
                    except OSError:
                        pass
            elif candidate.is_absolute() and str(candidate) not in unreadable_absolute:
                unreadable_absolute.append(str(candidate))

        blocks: list[str] = []
        seen: set[str] = set()
        listing_done = False
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                if not candidate.is_file():
                    continue
                size = candidate.stat().st_size
            except OSError:
                continue
            self_repo = True
            try:
                rel = candidate.relative_to(root)
                label = str(rel).replace("\\", "/")
            except ValueError:
                rel = candidate
                label = str(candidate)
                self_repo = False

            if not listing_done:
                listing_done = True
                try:
                    names = sorted(p.name for p in candidate.parent.iterdir()
                                   if p.is_file())[:40]
                    blocks.append(
                        f"### directory {candidate.parent} contains: " + ", ".join(names))
                except OSError:
                    pass

            if size > max_chars:
                head = candidate.read_text(encoding="utf-8", errors="replace")[:max_chars]
                blocks.append(
                    f"### {label} (first {max_chars} chars of {size}; "
                    f"{'editable' if self_repo else 'READ-ONLY, outside the repo'})\n"
                    + "\n".join(f"{i:>4} | {line}" for i, line in enumerate(
                        head.splitlines(), start=1)))
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            blocks.append(
                f"### {label} (current content; line numbers are a guide, not part of "
                f"the file; {'editable' if self_repo else 'READ-ONLY, outside the repo'})\n"
                + "\n".join(f"{i:>4} | {line}" for i, line in enumerate(
                    text.splitlines(), start=1)))
            if len(blocks) >= limit:
                break

        if unreadable_absolute and not blocks:
            # Show the paths *relative to the repository*: an absolute path in the
            # prompt is what taught the model to write ``<repo>/index.html`` and
            # create a nested copy of the repository.
            missing = []
            for raw in unreadable_absolute:
                try:
                    missing.append(str(Path(raw).relative_to(root)).replace("\\", "/"))
                except ValueError:
                    missing.append(Path(raw).name)
            blocks.append("### files mentioned but not found: " + ", ".join(missing)
                          + " — create them at exactly these relative paths.")
        if not blocks:
            return ""
        return ("\n\nTarget files — copy hunk context lines VERBATIM from here and "
                "match their real line numbers:\n\n" + "\n\n".join(blocks))

    def _brief_root(self, ctx: Any) -> Path:
        """The tree the brief should measure: the one this run writes to.

        A goal run works in an isolated copy of the repository.  The brief was
        built against the configured root — the *destination* — while the run
        edited the *copy*, so it reported ``files: 0`` and quoted no source.
        Measured live: a document goal about the decision ladder invented a
        ladder that does not exist, because the module it was describing was
        never in the tree the brief looked at.

        The run's own worktree wins when it is set and is a directory; the
        configured root is the fallback, which is what a plain ``run`` wants.
        """
        raw = str(getattr(ctx, "extras", {}).get("worktree") or "")
        if raw:
            candidate = Path(raw)
            try:
                if candidate.is_dir():
                    return candidate.resolve()
            except OSError:
                pass
        return self._repo_root()

    def _repo_root(self) -> Path:
        """The repository this pipeline writes to.

        One resolver, used everywhere.  The root was previously read as
        ``getattr(self.firewall, "config", None)`` and then
        ``getattr(config, "repo_root", ".")`` — which is ``None`` whenever no
        firewall is wired in, so the root silently became ``"."``, the
        *process's* working directory.  Measured: a goal naming
        ``math_utils.py`` in a temp repository produced "files mentioned but not
        found: math_utils.py — create them at exactly these relative paths", so
        the coder was told to create a file that already existed, never saw its
        text, invented its contents, and emitted a hunk that could not anchor.

        The pipeline's own config is authoritative: it is what the caller set,
        and it is what the firewall is built from when there is one.
        """
        raw = str(getattr(self.config, "repo_root", "") or "")
        if not raw:
            firewall_config = getattr(getattr(self, "firewall", None), "config", None)
            raw = str(getattr(firewall_config, "repo_root", "") or ".")
        try:
            return Path(raw).resolve()
        except OSError:  # pragma: no cover - an unresolvable root is used as given
            return Path(raw)

    def _repo_fingerprint(self, limit: int = 400) -> str:
        """Cheap content identity of the worktree, for cache keys without a slice.

        When the slice is empty the key used to be ``goal + git_sha``.  In a repo
        with no commits (``no-git``) that made every run on the same goal a cache
        hit — including runs after the files had changed.  A count+bytes+newest
        stamp invalidates the key as soon as the worktree moves.
        """
        root = self._repo_root()
        try:
            files = list(self.indexer.discover())[:limit] if self.indexer else []
        except Exception as exc:  # noqa: BLE001
            files = []
            self._degraded("indexer_discover", exc, "slice_fingerprint")
        if not files:
            # No index wired in: walk the tree, skipping the directories that never
            # carry task content.  A fingerprint of "0:0:0" would make every run in
            # such a deployment a cache hit.
            skip = {".git", ".gigaforge", ".venv", "node_modules", "__pycache__"}
            for path in root.rglob("*"):
                if len(files) >= limit:
                    break
                if any(part in skip for part in path.parts):
                    continue
                if path.is_file():
                    files.append(str(path.relative_to(root)).replace("\\", "/"))
        total = 0
        newest = 0
        for rel in files:
            try:
                stat = (root / rel).stat()
            except OSError:
                continue
            total += stat.st_size
            newest = max(newest, stat.st_mtime_ns)
        return f"{len(files)}:{total}:{newest}"

    def _episode_diff(self, ctx: RunContext, files: Sequence[str] | None = None) -> str:
        """Compute the diff for files the exploration episode wrote itself.

        The episode's ``write_file`` tool edits the worktree directly.  The
        pipeline's coder, however, is asked to *produce a diff* — so a file the
        episode already wrote is invisible to the rest of the run unless the
        harness turns it into one.  Measured live: a 4 KB page was written, the
        coder returned nothing, the fix loop rejected three times with "diff
        could not be parsed", and the run settled ``accepted: false, files: 0``
        with the file sitting on disk.

        The diff is computed, not asked for: ``git diff`` against HEAD, plus the
        untracked files the episode created.  Returns an empty string when the
        episode wrote nothing, so a run that never used the tool is unaffected.

        ``files`` overrides the episode's own list.  The repair node uses it to
        recompute the diff for a file it rewrote on disk: ``final_diff`` was
        computed before the repair, so without this the run reports a page that
        is not the page on disk.
        """
        written = list(files if files is not None
                       else (ctx.extras.get("episode_written_files") or []))
        if not written:
            return ""
        root = Path(str(getattr(self.config, "repo_root", ".") or ".")).resolve()
        if not (root / ".git").exists():
            # No repository: ``git diff`` cannot run, and returning "" here made
            # the work invisible — the run wrote a page and reported no change.
            # A new-file diff is the correct representation of a file that did
            # not exist before, and it is what the apply path already accepts.
            return self._new_file_diff(root, written)
        import subprocess

        parts: list[str] = []
        try:
            tracked = subprocess.run(
                ["git", "diff", "--no-color", "--", *written],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
            if tracked.returncode == 0 and tracked.stdout.strip():
                parts.append(tracked.stdout)
            # Untracked files produce no ``git diff`` output at all, so they are
            # diffed against /dev/null explicitly.
            for rel in written:
                path = root / rel
                if not path.is_file():
                    continue
                check = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", rel],
                    cwd=root, capture_output=True, text=True, timeout=15,
                )
                if check.returncode == 0:
                    continue
                body = path.read_text(encoding="utf-8", errors="replace")
                lines = body.splitlines()
                parts.append(
                    f"diff --git a/{rel} b/{rel}\n"
                    f"new file mode 100644\n"
                    f"--- /dev/null\n"
                    f"+++ b/{rel}\n"
                    f"@@ -0,0 +1,{len(lines)} @@\n"
                    + "".join(f"+{line}\n" for line in lines)
                )
        except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
            self._event("pipeline.episode_diff_error", run_id=ctx.run_id,
                        error=str(exc)[:200])
            return ""
        if parts:
            self._event("pipeline.episode_diff", run_id=ctx.run_id,
                        files=len(written), chars=sum(len(p) for p in parts))
        return "\n".join(parts)

    def _new_file_diff(self, root: Path, written: Sequence[str]) -> str:
        """Render files as ``new file`` diffs when there is no repository.

        Used by the generation path, which may run in a bare directory.  The
        shape is the one ``git apply`` accepts for a creation, so the rest of
        the pipeline treats it exactly like a diff from a repository.
        """
        parts: list[str] = []
        for rel in written:
            path = root / str(rel)
            if not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = body.splitlines()
            parts.append(
                f"diff --git a/{rel} b/{rel}\n"
                f"new file mode 100644\n"
                f"--- /dev/null\n"
                f"+++ b/{rel}\n"
                f"@@ -0,0 +1,{len(lines)} @@\n"
                + "".join(f"+{line}\n" for line in lines)
            )
        return "\n".join(parts)

    def _subagent_diff(self, ctx: RunContext, *, episode_diff: str = "") -> str:
        """Delegate a multi-file goal to sub-agents with exclusive ownership.

        Opt-in (``use_subagents``).  Each sub-agent runs the whole pipeline in
        its own worktree, and its diff is **accepted or rejected before it is
        merged** — see :mod:`gigaforge.core.acceptance`.

        Three things changed here, and each one was a measured defect:

        * **The child is handed only its own work.**  It used to receive
          ``f"Only touch {rel}. Goal: {full_goal}"``, and ``_ensure_creations``
          reads the file names out of the goal — so a child told to touch only
          ``a.html`` created ``index.html`` and ``b.html`` too, and three
          children rewrote each other's files.  The scoped goal names one path.
        * **Ownership is enforced, not requested.**  ``focus_paths`` was a hint
          for the slicer; nothing stopped a write.  The child's firewall now
          carries ``apply_allow_paths``, so a write outside its assignment is
          refused by the same guard that refuses a path outside the repo.
        * **The merge is sequential and conflict-aware.**  It was
          ``"\\n".join(diffs)`` followed by an apply in which the last writer
          won: two children creating the same file merged to ``ok: True`` with
          one child's work silently gone.
        """
        if not ctx.extras.get("use_subagents"):
            return ""
        if not bool(getattr(self.config.subagents, "use_worktrees", True)):
            return ""
        # A generation goal names no existing file, so the code fan-out below
        # could never fire for it: ``named`` was empty, the run logged
        # ``subagents_skipped`` and fell back to one model call that wrote the
        # whole page.  Measured live: ``--subagents`` on the site goal produced
        # ``{"files": 1}`` and a 2564-byte page with no feature grid.  The unit
        # of decomposition for an artifact is a section, not a file.
        try:
            from gigaforge.harness.task_kind import classify_task

            kind = classify_task(ctx.state.goal, self._repo_root())
            ctx.extras["task_kind"] = kind.to_dict()
            self._event("pipeline.task_kind", run_id=ctx.run_id,
                        task_kind=kind.kind, evidence=kind.evidence[:300])
            if kind.kind == "mixed":
                # A mixed request needs one coordinated authoring pass.  Sending
                # it through the artifact-only section builder drops its code
                # paths; splitting it into file children can do the same for a
                # newly-created artifact.
                self._event(
                    "pipeline.subagents_skipped", run_id=ctx.run_id,
                    reason="mixed code and artifact work requires coordinated execution",
                )
                return ""
            if kind.is_artifact:
                if episode_diff:
                    self._event(
                        "pipeline.subagents_skipped",
                        run_id=ctx.run_id,
                        reason="ReAct already wrote the artifact; preserving its content",
                    )
                    return ""
                # ``--subagents`` used to be a silent no-op on a page goal: the
                # artifact branch returned the sectional path and the flag was
                # never mentioned again, so a user who asked for sub-agents got
                # ``subagent events: []`` and ``fan_out called: False`` with no
                # explanation.  The flag *is* honoured — the unit of
                # decomposition for a page is a section, and the sections are
                # generated concurrently — but it is not a worktree fan-out, and
                # saying so is the difference between a documented design and a
                # flag that appears broken.
                self._event("pipeline.subagents_sections", run_id=ctx.run_id,
                            requested=True,
                            reason="a page decomposes into sections, not worktrees; "
                                   "sections are generated concurrently")
                return self._sectional_generation(ctx)
            if kind.is_code and episode_diff:
                # Child worktrees start from the repository base, not ReAct's
                # uncommitted writes. Their diffs cannot safely replace files
                # the planning episode has already changed.
                self._event(
                    "pipeline.subagents_skipped",
                    run_id=ctx.run_id,
                    reason="ReAct already wrote code; avoiding overlapping worktrees",
                )
                return ""
        except Exception as exc:  # noqa: BLE001 - fall through to the code path
            self._event("pipeline.generation_plan_error", run_id=ctx.run_id,
                        error=str(exc)[:300])
        try:
            from gigaforge.firewall.slicer_keywords import Extractor

            config = getattr(self.firewall, "config", None)
            paths = Extractor(config).extract(ctx.state.goal).paths
        except Exception as exc:  # noqa: BLE001
            self._degraded("slicer_keywords", exc, "generation")
            return ""
        root = self._repo_root()
        named = [
            rel for raw in dict.fromkeys(paths)
            if (rel := _workspace_relative_path(raw, root))
            and (root / rel).is_file()
        ]

        def is_test(rel: str) -> bool:
            lowered = rel.replace("\\", "/").lower()
            name = lowered.rsplit("/", 1)[-1]
            return (name.startswith("test_") or name.endswith("_test.py")
                    or "/tests/" in f"/{lowered}" or "/test/" in f"/{lowered}")

        # Fan-out is only safe for *independent* files.  A goal that also names the
        # tests is a coordinated change: `mul` in calc.py exists for a test in
        # test_calc.py, and splitting them makes both halves fail — observed live,
        # where four per-file children produced one diff and two hard failures.
        coordinated = any(is_test(rel) for rel in named)
        if len(named) > 3 or len(named) < 2:
            self._event("pipeline.subagents_skipped", run_id=ctx.run_id,
                        files=len(named), reason="coordinated change or too many files")
            return ""

        from .acceptance import allocate_work, check_acceptance, merge_diffs
        from .agents import SubAgentSpec

        items = allocate_work(ctx.state.goal, named, coordinated=coordinated)
        if len(items) < 2:
            self._event("pipeline.subagents_skipped", run_id=ctx.run_id,
                        files=len(named), reason="not enough independent work")
            return ""
        symbol_owners = self._subagent_symbol_owners(ctx.state.goal, named)

        task_budget = int(ctx.extras.get("budget_tokens") or 0)
        child_budget = _allocate_subagent_budget(
            task_budget, ctx.state.total_tokens(), len(items)
        )
        if task_budget and child_budget <= 0:
            self._event(
                "pipeline.subagents_skipped", run_id=ctx.run_id,
                reason="remaining token budget is too small to allocate across children",
                budget_tokens=task_budget, tokens_used=ctx.state.total_tokens(),
            )
            return ""

        specs = []
        for index, item in enumerate(items):
            scoped_goal = _scoped_goal(ctx.state.goal, item.owns_paths, named)
            ownership = [
                (
                    f"Implement {symbol} only in {owner}."
                    if owner in item.owns_paths
                    else f"Do not implement or duplicate {symbol} in this file; "
                         f"that requirement belongs exclusively to {owner}."
                )
                for symbol, owner in symbol_owners.items()
            ]
            if ownership:
                scoped_goal += "\n\nSymbol ownership:\n" + "\n".join(
                    f"- {rule}" for rule in ownership
                )
            specs.append(
                SubAgentSpec(
                    name=f"work-{index}",
                    goal=scoped_goal,
                    focus_paths=item.owns_paths,
                    budget_tokens=child_budget,
                    use_cache=bool(ctx.extras.get("use_cache", True)),
                    dry_run=bool(ctx.extras.get("dry_run", False)),
                    max_fix_iterations=ctx.extras.get("max_fix_iterations"),
                )
            )
        try:
            results = self.fan_out(specs, parent_ctx=ctx, parallel=True)
        except Exception as exc:  # noqa: BLE001 - fall back to one agent
            self._event("pipeline.subagents_error", run_id=ctx.run_id,
                        error=str(exc)[:300])
            return ""

        child_budget_overruns: list[dict[str, Any]] = []
        for spec, result in zip(specs, results, strict=True):
            if spec.budget_tokens:
                child_tokens = max(0, int(getattr(result, "tokens", 0) or 0))
                if child_tokens > spec.budget_tokens:
                    child_budget_overruns.append(
                        {
                            "name": spec.name,
                            "tokens": child_tokens,
                            "budget_tokens": spec.budget_tokens,
                        }
                    )

            observed_tokens = 0
            observed_calls = 0
            child_usage = getattr(result, "phase_usages", {}) or {}
            child_costs = getattr(result, "phase_cost_micros", {}) or {}
            if isinstance(child_usage, Mapping):
                for phase, usage in child_usage.items():
                    if not isinstance(usage, Mapping):
                        continue
                    phase_name = str(phase)
                    bucket = ctx.state.phase_usages.setdefault(
                        phase_name,
                        {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "calls": 0,
                        },
                    )
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "calls"):
                        value = max(0, int(usage.get(key, 0) or 0))
                        bucket[key] = int(bucket.get(key, 0) or 0) + value
                    observed_tokens += max(0, int(usage.get("total_tokens", 0) or 0))
                    observed_calls += max(0, int(usage.get("calls", 0) or 0))
                    if isinstance(child_costs, Mapping):
                        phase_cost = max(0, int(child_costs.get(phase, 0) or 0))
                        if phase_cost:
                            ctx.state.phase_cost_micros[phase_name] = (
                                int(ctx.state.phase_cost_micros.get(phase_name, 0))
                                + phase_cost
                            )

            reported_tokens = max(0, int(getattr(result, "tokens", 0) or 0))
            reported_calls = max(0, int(getattr(result, "calls", 0) or 0))
            untracked_tokens = max(0, reported_tokens - observed_tokens)
            untracked_calls = max(0, reported_calls - observed_calls)
            if untracked_tokens or untracked_calls:
                bucket = ctx.state.phase_usages.setdefault(
                    "subagents",
                    {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "calls": 0,
                    },
                )
                bucket["total_tokens"] = (
                    int(bucket.get("total_tokens", 0)) + untracked_tokens
                )
                bucket["calls"] = int(bucket.get("calls", 0)) + untracked_calls
            ctx.state.cost_micros += max(
                0, int(getattr(result, "cost_micros", 0) or 0)
            )

        if child_budget_overruns:
            ctx.extras["subagent_budget_overrun"] = child_budget_overruns
            self._event(
                "pipeline.subagents_budget_exceeded", run_id=ctx.run_id,
                children=child_budget_overruns[:4],
            )

        # Acceptance gate.  A rejected child is reported with its reason and
        # excluded from the merge; it is never silently dropped and never
        # silently merged.
        entries: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        accepted_paths: list[str] = []
        for item, result in zip(items, results, strict=True):
            misplaced_symbols = [
                f"{symbol} was added to {path}, but belongs in {owner}"
                for path, symbol in _added_function_definitions(
                    str(getattr(result, "diff", "") or "")
                )
                if (owner := symbol_owners.get(symbol)) and owner != path
            ]
            if misplaced_symbols:
                reasons = misplaced_symbols
                verdict = None
            else:
                verdict = check_acceptance(
                    result, item, accepted_paths=accepted_paths, config=config
                )
                reasons = verdict.reasons
            if verdict is not None and verdict.accepted:
                accepted_paths.extend(verdict.paths)
                entries.append({
                    "name": item.id,
                    "diff": str(getattr(result, "diff", "") or ""),
                    "owns_paths": item.owns_paths,
                })
            else:
                rejected.append({
                    "name": item.id,
                    "reasons": reasons,
                    "ok": bool(getattr(result, "ok", False)),
                    "error": str(getattr(result, "error", "") or "")[:200],
                })

        if not entries:
            self._event(
                "pipeline.subagents_empty", run_id=ctx.run_id, specs=len(specs),
                detail=rejected[:4],
            )
            return ""

        report = merge_diffs(entries, root=root, config=config)
        ctx.extras["subagent_merge"] = report.to_dict()
        # A partial fan-out is not a finished run.
        #
        # Measured: two children, one accepted and merged, one rejected with a
        # reason — and the run settled ``accepted: True`` with one of the two
        # files fixed.  The goal asked for both.  Reporting success on half the
        # work is the same defect as reporting success on a rejected artifact:
        # the verdict does not follow the evidence.
        #
        # The accepted child's work is kept (it is already in the tree and its
        # diff is returned), and the run is marked partial so the goal loop
        # spends another cycle on what is left instead of stopping.
        if rejected or report.rejected:
            ctx.extras["subagent_partial"] = {
                "accepted": list(report.accepted),
                "rejected": (rejected + report.rejected)[:6],
            }
            self._event("pipeline.subagents_rejected", run_id=ctx.run_id,
                        rejected=(rejected + report.rejected)[:4])
        if report.conflicts:
            # A conflict means two children claimed one path.  The allocator
            # makes that impossible for the paths it was given, so reaching here
            # means a child wrote outside its assignment and the guard let it
            # through — worth a loud event rather than a quiet merge.
            self._event("pipeline.subagents_conflict", run_id=ctx.run_id,
                        conflicts=report.conflicts[:4])
        if not report.diff:
            self._event("pipeline.subagents_empty", run_id=ctx.run_id,
                        specs=len(specs), detail=report.rejected[:4])
            return ""
        self._event("pipeline.subagents", run_id=ctx.run_id,
                    files=len(named), diffs=len(report.accepted),
                    rejected=len(rejected) + len(report.rejected))
        return report.diff

    def _answer_proposal(self, ctx: RunContext) -> Mapping[str, Any]:
        """Answer the goal with an episode, and return the reply as the proposal.

        **Why this is a separate path and not a prompt tweak.**  The harness had
        exactly one output channel: a unified diff.  A goal whose correct output
        is a sentence therefore had nowhere to go — measured live, "What is
        2+2? Answer in chat, do not change any files." died at ``plan`` with
        ``steps: 0``, and a web-search goal was rejected with "diff could not be
        parsed: no file sections found in diff" *after* the search had really
        run and returned real results.  The tool worked; the product refused to
        deliver what it produced.

        The episode is the right executor because it already has both halves:
        the repository tools (so "what does this repo do" is answerable) and
        ``web_search`` (budgeted, cached, honest when offline).  Its ``finish``
        prose is the deliverable, and the citations it saw are carried out so
        the acceptance gate can check that a search-backed answer names its
        sources instead of inventing them.

        The proposal carries the reply in ``text`` and an **empty** ``diff``.
        That is the point: ``validate`` must not demand a diff for a reply, and
        ``settle`` must accept on the answer's own terms.
        """
        goal = _original_goal(ctx.state.goal)
        runner = self._build_react_runner(goal, run_id=ctx.run_id)
        if runner is None:
            return {
                "proposal": ProposeResult(
                    text="", diff="", phase="answer", model="",
                ),
                "proposal_source": "answer-unavailable",
            }
        from gigaforge.agents.engine import run_react

        result = run_react(
            runner, goal, runner.state, ctx=runner.ctx,
            thread_id=f"{ctx.run_id}-answer",
        )
        text = str(getattr(result, "final_text", "") or "").strip()
        citations = _citations_from(result)
        searched = any(str(s.get("action") or "") == "web_search"
                       for s in (getattr(result, "steps", None) or []))
        # The episode's ``finish`` prose is frequently a *narration of its own
        # progress* — measured live: "The calculation of 2+2 returned 4, which is
        # correct. Now, I need to finish the task as required." That is a status
        # line, not an answer, and delivering it would be the harness mistaking
        # its own telemetry for the user's deliverable.
        #
        # So the episode's job is to *gather*; one clean call writes the answer
        # from what was gathered.  That is also the cheapest possible structure:
        # the synthesis sees the observations, not the whole trajectory.
        text = self._synthesize_answer(ctx, goal, text, citations, searched, result)
        # A search-backed answer must name its sources, and the gate enforces it.
        # Measured live: the episode really searched (6 real Wikipedia URLs were
        # observed) and the synthesis wrote a good answer — but it cited none of
        # them, so ``_validate_answer`` rejected the run with "the answer used
        # web results but cites none of them".  The gate was right; the writer
        # was wrong.
        #
        # Asking the model to remember to cite is a hope.  Appending the
        # observed URLs is a fact: they came from the tool, not from the model,
        # so this cannot invent a source.  It runs only when the reply cites
        # nothing, so a model that already cites keeps its own formatting.
        if searched and citations and not re.search(r"https?://", text):
            listed = "\n".join(f"- {url}" for url in citations[:8])
            text = f"{text}\n\nSources:\n{listed}" if text else f"Sources:\n{listed}"
        ctx.extras["answer_text"] = text
        ctx.extras["answer_citations"] = citations
        ctx.extras["answer_searched"] = searched
        self._event(
            "pipeline.answer", run_id=ctx.run_id,
            chars=len(text), citations=len(citations),
            searched=bool(ctx.extras["answer_searched"]),
            finish_reason=str(getattr(result, "finish_reason", "")),
        )
        return {
            "proposal": ProposeResult(
                text=text, diff="", phase="answer",
                model=str(getattr(self.router, "last_model", "") or ""),
                raw=result,
            ),
            "proposal_source": "answer",
        }

    def _synthesize_answer(self, ctx: RunContext, goal: str, narration: str,
                           citations: Sequence[str], searched: bool,
                           episode: Any) -> str:
        """Write the reply from what the episode observed, not from its notes.

        Two inputs, in order of authority:

        1. the **observations** — tool output the episode actually saw;
        2. the episode's own ``finish`` prose — often a status line.

        When there are no observations at all (a pure arithmetic question needs
        no tools), the narration is the only content there is and it is passed
        through.  Otherwise one cheap call writes the answer, with a prompt that
        forbids narrating its own process: the user asked the question, not for
        a transcript of the run.

        A synthesis failure is a degradation, not a run failure: the narration
        is still delivered rather than losing an accepted answer to a second
        call going wrong.
        """
        observations: list[str] = []
        for step in getattr(episode, "steps", None) or []:
            observed = str(step.get("observation") or "").strip()
            if observed:
                observations.append(f"- {str(step.get('action') or 'tool')}: {observed[:1500]}")
        if not observations and not searched:
            return narration
        material = "\n".join(observations[:12]) or "(no tool output)"
        prompt = build_phase_prompt(
            "summarize",
            goal=goal,
            extra_blocks=[
                ("What the tools returned", material),
                ("Citation URLs (use only these)", "\n".join(citations[:20]) or "(none)"),
                ("Instruction",
                 "Write the final answer for the person who asked. Address the "
                 "question directly and completely. Do not describe your process, "
                 "your plan, or what you are about to do — no 'now I need to' and "
                 "no narration of steps. If you cite a source, use only a URL "
                 "from the list above. Be concise."),
            ],
        )
        try:
            produced = self._ask("summarize", prompt.render(), ctx).strip()
        except Exception as exc:  # noqa: BLE001 - the narration is still deliverable
            self._degraded("answer_synthesis", exc, "act")
            return narration
        return produced or narration

    def _node_act(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        if inputs.get("cache_hit"):
            return {
                "proposal": ProposeResult(diff=str(inputs.get("cached_diff", "")),
                                          phase="code", cached=True, model="cache"),
                "proposal_source": "cache",
            }
        if self.router is None:
            raise PipelineError("no model client is wired into the pipeline")

        # An answer goal is answered, not coded.  The coder is asked for a diff;
        # a reply has none, so the run either produced nothing or was rejected
        # for "diff could not be parsed".  The episode is the right executor: it
        # has the repository tools AND ``web_search``, and its ``finish`` prose
        # is the deliverable.
        if _task_kind_for(self, ctx).is_answer:
            return self._answer_proposal(ctx)

        slice_obj = inputs.get("slice")
        slice_text = slice_obj.render() if slice_obj is not None else ""
        # The plan the episode paid for.  ``_node_act`` declared ``plan`` as a
        # required input and then never read it, so every ReAct run — 4–6 model
        # calls plus tool calls — was billed and thrown away while the coder
        # re-derived the target from the goal.  Hand it over explicitly.
        plan_block = self._plan_block(ctx, inputs)
        if plan_block:
            slice_text = f"{slice_text}\n\n{plan_block}" if slice_text else plan_block
        extra = self._file_context(ctx.state.goal)
        if extra:
            slice_text = f"{slice_text}\n{extra}" if slice_text else extra
        recalled = ctx.extras.get("recalled") or []
        if recalled:
            slice_text += ("\n\nEarlier runs on similar tasks (lessons, not diffs):\n"
                           + "\n".join(f"- {line}" for line in recalled))
        optimizer = getattr(self, "optimizer", None)
        if optimizer is not None and slice_text:
            try:
                trimmed = optimizer.optimize_text(
                    slice_text,
                    budget_tokens=int(ctx.extras.get("budget_tokens") or 0) or None,
                )
                if getattr(trimmed, "text", ""):
                    ctx.extras["context_stats"] = {
                        "before": len(slice_text),
                        "after": len(trimmed.text),
                        "dropped": getattr(trimmed, "dropped", 0),
                    }
                    slice_text = trimmed.text
            except Exception as exc:  # noqa: BLE001 - trimming is an optimisation
                self._event("pipeline.optimize_error", run_id=ctx.run_id,
                            error=str(exc)[:200])
        # The exploration episode may have written files itself (its ``write_file``
        # tool).  Those writes are real work the run already paid for, and the
        # coder cannot see them: it is asked to express the change as a diff, and
        # a model that has already produced the file has no reason to produce it
        # again.  Measured live: the episode wrote a 4 KB page, the coder returned
        # no diff, the fix loop rejected three times with "diff could not be
        # parsed", and the run settled ``accepted: false, files: 0`` while the
        # file sat on disk.  The harness computes the diff from the worktree —
        # that is a computation, not a judgement call.
        episode_diff = self._episode_diff(ctx)
        prefetched = self._subagent_diff(ctx, episode_diff=episode_diff)

        # A page goal is built section by section in every mode, not only under
        # ``--subagents``.  Measured live: ``run`` on the site goal produced a
        # 2549-byte skeleton — "Feature 1 / Feature 2 / Feature 3" — because one
        # model call was asked to write a whole page in one shot.  The harness
        # owns the plan and the skeleton; that is true whether or not the user
        # asked for sub-agents.
        #
        # This must be decided **before** the episode's own writes are adopted as
        # the proposal.  The episode runs first and its ``write_file`` tool often
        # drops a draft page; appending that draft made ``prefetched`` non-empty,
        # so the sectional path below was skipped and the draft *became* the
        # artifact.  Measured live on the cyberpunk landing page: the file went
        # from the workspace scaffold straight to one model call's output —
        # ``Feature 1``…``Feature 4`` in inline styles, 1597 bytes, a typo in the
        # <title> — and every structural check passed, because the page was
        # valid and merely poor.  For an artifact goal the draft is context, not
        # the deliverable.
        artifact_only = False
        try:
            from gigaforge.harness.task_kind import classify_task

            kind = classify_task(_original_goal(ctx.state.goal), self._repo_root())
            ctx.extras["task_kind"] = kind.to_dict()
            self._event("pipeline.task_kind", run_id=ctx.run_id,
                        task_kind=kind.kind, evidence=kind.evidence[:300])
            artifact_only = kind.is_artifact and not kind.is_code
        except Exception as exc:  # noqa: BLE001 - fall back to the coder
            self._event("pipeline.generation_plan_error", run_id=ctx.run_id,
                        error=str(exc)[:300])

        if artifact_only:
            built = self._sectional_generation(ctx)
            if built:
                # The harness's assembly is authoritative: it is structurally
                # valid by construction and every section is present.  The
                # episode's draft is dropped rather than concatenated onto it.
                prefetched = built
                ctx.extras["artifact_draft_discarded"] = bool(episode_diff)
            elif episode_diff:
                prefetched = "\n".join(p for p in (prefetched, episode_diff) if p)
        elif episode_diff:
            prefetched = "\n".join(p for p in (prefetched, episode_diff) if p)

        if prefetched:
            creation_diff = self._ensure_creations(ctx, prefetched, slice_text, ctx)
            if creation_diff:
                prefetched = "\n".join(
                    part for part in (prefetched, creation_diff) if part
                )

        def propose(phase: str, prompt_text: str, iteration: int, hint: str) -> ProposeResult:
            text = self._ask(phase, prompt_text, ctx)
            route = self.router.last_route if self.router else None
            diff = extract_diff(text)
            blocks = self._file_blocks(text)
            if blocks:
                from .fileblocks import files_to_diff

                diff = "\n".join(part for part in (diff, files_to_diff(blocks)) if part)
            diff = self._repair_creation_hunks(ctx, diff)
            if diff:
                creation_diff = self._ensure_creations(ctx, diff, slice_text, ctx)
                if creation_diff:
                    diff = "\n".join(part for part in (diff, creation_diff) if part)
            return ProposeResult(text=text, diff=diff, phase=phase,
                                 model=route.model if route else "")

        result = self.fixloop.run(
            ctx.state.goal,
            propose_fn=propose,
            initial_diff=prefetched,
            ctx=ctx,
            slice_text=slice_text,
            max_iterations=int(ctx.extras.get("max_fix_iterations") or 0) or None,
            dry_run=bool(ctx.extras.get("dry_run")),
            run_id=ctx.run_id,
            approve=ctx.extras.get("approve"),
            # A model told "here is the page, change it" often answers with a
            # bare hunk and no ``--- a/path`` header.  The harness knows which
            # file is meant, so it says so instead of failing the run with
            # "hunk without a file header and no path hint" — measured live on
            # the ``run`` mode, which rejected a correct page for this reason.
            path_hint=_diff_path_hint(ctx),
        )
        ctx.extras["fix_result"] = result
        result.diff = self._repair_creation_hunks(ctx, result.diff)
        creation_diff = self._ensure_creations(
            ctx, result.diff, slice_text, ctx, validated=result.accepted
        )
        if creation_diff:
            creation_diff = self._repair_creation_hunks(ctx, creation_diff)
            creation_validation = self.fixloop.validate_once(
                creation_diff,
                task=ctx.state.goal,
                dry_run=bool(ctx.extras.get("dry_run")),
                path_hint=_diff_path_hint(ctx),
            )
            creation_status = _merge_focused_creation(
                result,
                creation_diff,
                creation_validation,
                ctx.extras.get("goal_named_paths") or (),
            )
            if creation_status["missing"]:
                ctx.extras["focused_creation_partial"] = creation_status["missing"]
            self._event(
                "pipeline.focused_creation",
                run_id=ctx.run_id,
                accepted=creation_status["accepted"],
                files=creation_status["files"],
                missing=creation_status["missing"],
                reason=creation_validation.reason[:300],
            )
        # Ownership is ONE invariant, applied once.  Three mechanisms used to
        # approximate it — drop the model's hunks, put the harness's hunks back,
        # write the harness's bytes over the tree — and the invariant lived only
        # in comments.  The consequence: ``_restore_harness_owned`` overwrote the
        # file AFTER the model's diff, so a model that fixed a real bug in that
        # file had its fix silently discarded.  Ownership was absolute because
        # nothing in the code could say it was not.
        #
        # ``OwnedFiles.resolve`` applies the whole rule in one call, and the
        # policy is a value: AUTHORITATIVE (the harness assembled this file from
        # a plan — its bytes are the artifact) or SEED (the harness wrote a
        # baseline — the model's edits are the work and are never overwritten).
        owned = self._owned_files(ctx)
        if owned:
            result.diff, resolution = owned.resolve(
                result.diff, self._repo_root(), harness_diff=prefetched or ""
            )
            if resolution.dropped:
                self._event("pipeline.harness_owned_hunks_dropped", run_id=ctx.run_id,
                            paths=resolution.dropped)
            if resolution.restored:
                self._event("pipeline.harness_owned_restored", run_id=ctx.run_id,
                            paths=resolution.restored)
            if resolution.model_kept:
                self._event("pipeline.harness_seed_kept", run_id=ctx.run_id,
                            paths=resolution.model_kept)
            ctx.extras["ownership"] = resolution.to_dict()

        route = self.router.last_route if self.router else None
        proposal = ProposeResult(
            diff=result.diff,
            phase="code",
            model=route.model if route else "",
            attempts=result.proposals,
            latency_ms=result.duration_ms,
        )
        return {
            "proposal": proposal,
            "proposal_source": "model",
        }

    def _repair_creation_hunks(self, ctx: RunContext, diff: str) -> str:
        """Turn a creation hunk for an existing file into a real modification.

        The live probe found this on its first real run: asked to *fix*
        ``math_utils.py``, the model returned the corrected function as a **new
        file** (``--- /dev/null``).  ``git apply`` rejects that — the file
        exists — so the run reported a diff that looked right and did not apply.

        Deterministic, no model call: see :mod:`gigaforge.harness.diff_repair`.
        """
        if not diff:
            return diff
        try:
            from gigaforge.harness.diff_repair import normalize_creation_hunks

            root = Path(str(getattr(self.config, "repo_root", ".") or ".")).resolve()
            repaired, report = normalize_creation_hunks(diff, root)
        except Exception as exc:  # noqa: BLE001 - a repair must never fail a run
            self._event("pipeline.diff_repair_error", run_id=ctx.run_id,
                        error=str(exc)[:200])
            return diff
        if report.changed:
            ctx.extras["diff_repaired"] = report.to_dict()
            ctx.state.add_note(f"diff repaired: {', '.join(report.repaired)}")
            self._event("pipeline.diff_repaired", run_id=ctx.run_id,
                        files=report.repaired)
        return repaired

    def _ensure_creations(self, ctx: RunContext, diff: str, slice_text: str,
                          ctx_any: Any = None, *, validated: bool = False) -> str:
        """Ask for the missing *new* files the goal names, one focused call each.

        Models merge several files into a single ``diff --git`` block; the parser
        drops the stray creation hunk (it cannot know which file it belonged to), so
        a request like "create index2.html and fix index1.html" produced only the
        edit.  When the goal names a file that does not exist and the diff does not
        create it, one create-only call per file closes the gap deterministically.
        """
        try:
            from gigaforge.firewall.slicer_keywords import Extractor

            config = getattr(self.firewall, "config", None)
            paths = Extractor(config).extract(ctx.state.goal).paths
        except Exception as exc:  # noqa: BLE001
            self._degraded("path_extractor", exc, "create_missing")
            return ""
        root = self._repo_root()
        paths = [
            rel for raw in dict.fromkeys(paths)
            if (rel := _workspace_relative_path(raw, root))
        ]
        ctx.extras["goal_named_paths"] = paths
        created_markers = []
        for rel in paths:
            escaped = re.escape(rel)
            creation = re.search(
                rf"(?ms)^--- /dev/null\r?\n\+\+\+ (?:b/)?{escaped}\r?\n"
                rf"(?P<body>.*?)(?=^--- |\Z)",
                diff,
            )
            if creation and re.search(r"(?m)^\+[^+]", creation.group("body")):
                created_markers.append(rel)
        missing = [
            p for p in paths
            if not (root / p).exists() and p not in created_markers
        ][:2]
        if not missing:
            return ""
        extra_blocks: list[str] = []
        for rel in missing:
            prompt = (
                f"Create the new file `{rel}` that the goal requires.\n"
                f"Goal: {ctx.state.goal}\n\n"
                "Return one unified diff that creates ONLY this file:\n"
                f"```diff\n--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,N @@\n<full content>\n```\n"
                "No other files, no prose."
            )
            try:
                text = self._ask("code", prompt, ctx)
            except Exception as exc:  # noqa: BLE001 - a missing creation is reported, not fatal
                self._degraded("focused_create", exc, "generation")
                raise PipelineError(
                    f"could not generate required file {rel}: {type(exc).__name__}: {exc}"
                ) from exc
            proposal = extract_diff(text)
            headers = set(proposal.splitlines())
            if f"+++ b/{rel}" not in headers:
                raise PipelineError(
                    f"focused creation for {rel} did not return a diff creating that file"
                )
            extra_blocks.append(proposal.strip())
            ctx.state.add_note(f"create: {rel} (focused proposal)")
        return "\n".join(extra_blocks).strip()

    def _file_blocks(self, text: str) -> dict[str, str]:
        """Whole-file blocks from an answer, restricted to files that do not exist.

        Creating a large file through a diff asks the model to prefix every line
        and count hunks; one miscount rejects the whole file.  A ``### FILE:``
        block is the natural shape for creation, so it is accepted here and turned
        into a "new file" diff (one apply/verify path for both).

        Blocks naming an *existing* file are ignored: a whole-file rewrite silently
        destroys whatever the model did not reproduce, and edits already travel as
        diffs with quoted context.
        """
        try:
            from .fileblocks import extract_file_blocks

            blocks = extract_file_blocks(text)
        except Exception as exc:  # noqa: BLE001 - a parse failure just means "no blocks"
            self._degraded("file_blocks", exc, "generation")
            return {}
        if not blocks:
            return {}
        config = getattr(self.firewall, "config", None)
        root = self._repo_root()
        keep: dict[str, str] = {}
        for path, content in blocks.items():
            try:
                if (root / path).exists():
                    continue
            except OSError:
                pass
            keep[path] = content
        return keep

    def _react_repair(self, ctx: RunContext, proposal: Any, findings: Mapping[str, Any],
                      rounds: int = 2) -> str:
        """Observe → act loop: tools report, the model repairs, tools re-check.

        This is the ReAct shape without a second agent framework: the observation is
        the real output of ruff/pytest on the files just written, the action is a
        corrected diff, and the loop ends when the tools are quiet or the round
        budget is spent.  Opt-in (``react``), because every round is a model call.
        """
        diff = str(getattr(proposal, "diff", "") or "")
        if not diff or self.router is None:
            return ""
        observation = ""
        if findings.get("lint_errors"):
            observation += "\n".join(str(x) for x in findings["lint_errors"])[:1500]
        if findings.get("tests_failed"):
            observation += "\n" + str(findings["tests_failed"])[:1500]
        for round_index in range(1, max(1, rounds) + 1):
            prompt = (
                f"Goal: {ctx.state.goal}\n\n"
                "Your previous change was applied and the checks reported:\n"
                f"{observation or 'no findings'}\n\n"
                "Return one corrected ```diff fenced unified diff that fixes exactly "
                "these findings against the current files. No prose."
            )
            try:
                text = self._ask("fix", prompt, ctx)
            except Exception as exc:  # noqa: BLE001 - a failed round ends the loop
                self._event("pipeline.react_error", run_id=ctx.run_id, round=round_index,
                            error=str(exc)[:200])
                return ""
            candidate = extract_diff(text)
            if not candidate:
                return ""
            validation = self.fixloop.validate_once(candidate, task=ctx.state.goal)
            self._event("pipeline.react_round", run_id=ctx.run_id, round=round_index,
                        accepted=bool(validation.accepted),
                        reason=str(validation.reason)[:160])
            if validation.accepted:
                return candidate
            diff = candidate
            observation = str(validation.reason or "rejected")[:1500]
        return ""

    def _self_debug(self, ctx: RunContext, validation: Any) -> dict[str, Any]:
        """Run allowed checks on what was just written, and report what broke.

        This is the tool half of self-debug: the harness does not ask the model
        whether its change is sound — it lints the changed files and, when the
        project ships tests, runs them.  The result is a compressed repair hint that
        the ``fix`` node hands to the loop, so "verification failed" becomes
        "here is the exact error, try again".
        """
        if not ctx.extras.get("self_debug", True):
            return {}
        config = getattr(getattr(self, "firewall", None), "config", None)
        root = self._repo_root()
        changed = [str(p) for p in (getattr(validation, "changed_files", []) or [])]
        if not changed:
            return {}
        try:
            from gigaforge.sandbox.allowlist import DEFAULT_ALLOWLIST
            from gigaforge.sandbox.executor import SandboxExecutor

            sandbox = SandboxExecutor(worktree=root, allowlist=DEFAULT_ALLOWLIST)
        except Exception as exc:  # noqa: BLE001 - tools are optional
            return {"error": f"sandbox unavailable: {exc}"}

        findings: dict[str, Any] = {"files": changed[:6]}
        lint_errors: list[str] = []
        for rel in changed[:6]:
            path = root / rel
            if not path.is_file() or path.suffix.lower() != ".py":
                continue
            try:
                result = sandbox.run_lint(path)
            except Exception as exc:  # noqa: BLE001
                lint_errors.append(f"{rel}: {exc}")
                continue
            text = str(getattr(result, "stdout", "") or "") + str(
                getattr(result, "stderr", "") or "")
            if getattr(result, "returncode", 0) not in (0, None) and text.strip():
                lint_errors.append(f"{rel}: {text.strip()[:400]}")
        if lint_errors:
            findings["lint_errors"] = lint_errors[:4]
        try:
            tests = sandbox.run_tests()
            code = getattr(tests, "returncode", None)
            # pytest exit codes: 0 = passed, 1 = failed, 2 = interrupted,
            # 4 = usage error, 5 = nothing collected.  Only 1 means "the suite ran
            # and rejected the change"; treating 4/5 as failure would mark every
            # repository without tests as broken.
            if code == 1:
                findings["tests_failed"] = (
                    str(getattr(tests, "stdout", "") or "")[-800:]
                    + str(getattr(tests, "stderr", "") or "")[-400:]
                ).strip()[:1200]
            elif code == 0:
                findings["tests_passed"] = True
        except Exception as exc:  # noqa: BLE001 - no test suite is not an error
            # "No test suite" and "the test runner is broken" are different
            # facts, and the second one used to look exactly like the first.
            ctx.state.note_degraded(
                what="self_debug_tests", why=f"{type(exc).__name__}: {exc}",
                where="self_debug",
            )
        if findings.get("lint_errors") or findings.get("tests_failed"):
            ctx.extras["self_debug"] = findings
            self._event("pipeline.self_debug", run_id=ctx.run_id,
                        lint=len(findings.get("lint_errors") or []),
                        tests=bool(findings.get("tests_failed")))
        return findings

    def _validate_answer(self, ctx: RunContext, proposal: Any) -> ValidateResult:
        """Accept a reply on the facts a reply can be held to.

        Three checks, all deterministic:

        * the reply is not empty (an empty answer is a failed run, not a terse one);
        * if the episode searched, the reply names at least one URL it actually
          saw — a search-backed answer with no citation is a claim without a
          source, and the harness has the observations to prove it;
        * the reply does not cite a URL that appears in no observation — that is
          an invented source, the one failure mode a search tool exists to
          prevent.

        No diff is required, and none is expected.
        """
        text = str(getattr(proposal, "text", "") or "").strip()
        if not text:
            return ValidateResult(
                accepted=False, applied=False,
                reason="the run produced no answer text",
            )
        citations = list(ctx.extras.get("answer_citations") or [])
        searched = bool(ctx.extras.get("answer_searched"))
        cited_in_reply = [
            m.group(0).rstrip(".,;:")
            for m in re.finditer(r"https?://[^\s<>\"')\]]+", text)
        ]
        if searched and not citations:
            return ValidateResult(
                accepted=False, applied=False,
                reason="the episode searched the web but no result URL was observed",
            )
        invented = [u for u in cited_in_reply if u not in citations]
        if invented:
            return ValidateResult(
                accepted=False, applied=False,
                reason="the answer cites URL(s) no observation returned: "
                       + ", ".join(invented[:3]),
            )
        if searched and not cited_in_reply:
            return ValidateResult(
                accepted=False, applied=False,
                reason="the answer used web results but cites none of them",
            )
        return ValidateResult(
            accepted=True, applied=False, changed_files=[],
            reason=f"answer accepted ({len(text)} chars, {len(citations)} citation(s))",
        )

    def _node_validate(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        proposal = inputs.get("proposal")
        diff = getattr(proposal, "diff", "") or ""
        # An answer is validated on its own terms: a non-empty reply, and — when
        # the episode searched — at least one citation that came from a real
        # observation.  Demanding a diff here is what made every reply fail.
        if _task_kind_for(self, ctx).is_answer:
            return {"validation": self._validate_answer(ctx, proposal)}
        if not diff:
            return {"validation": ValidateResult(accepted=False, reason="no diff to validate")}

        # A proposed diff is not evidence that it is safe to apply. Keep the
        # proposal available to the caller, but never report it as accepted
        # without the configured validator.
        if self.fixloop.validator is None:
            return {"validation": ValidateResult(
                accepted=False, applied=False,
                changed_files=list(getattr(proposal, "changed_files", []) or []),
                reason="cannot validate the proposed diff: no validator is configured",
            )}

        # When the fix loop already validated a diff, reuse its verdict instead of
        # validating a second time: a second pass could disagree with the loop's
        # last iteration and the run would then report two different truths.
        loop_result: Optional[FixLoopResult] = ctx.extras.get("fix_result")
        if loop_result is not None and loop_result.iterations:
            last = loop_result.iterations[-1]
            if last.validate is not None:
                result = last.validate
            else:
                result = self.fixloop.validate_once(
                    diff, task=ctx.state.goal, dry_run=bool(ctx.extras.get("dry_run"))
                )
        else:
            result = self.fixloop.validate_once(
                diff, task=ctx.state.goal, dry_run=bool(ctx.extras.get("dry_run"))
            )
        # The project's own tests are the acceptance criterion, not a hint.  Until
        # now "accepted" meant "the diff parsed, applied and linted", which is how a
        # run reported success while two tests were red.  A red suite now rejects the
        # run and the reason travels into the next attempt.
        if result.accepted and not ctx.extras.get("dry_run") and ctx.extras.get(
                "test_gate", True) and self.test_runner is not None:
            try:
                outcome = self.test_runner()
                passed = bool(getattr(outcome, "passed", True))
                if not passed:
                    from .contracts import ValidateResult as _VR

                    return {"validation": _VR(
                        accepted=False, applied=True,
                        changed_files=list(getattr(result, "changed_files", []) or []),
                        reason="the test suite failed after applying the change: "
                               + str(getattr(outcome, "summary", "") or "")[:600],
                    )}
            except Exception as exc:  # noqa: BLE001 - a missing runner is not a failure
                self._event("pipeline.test_gate_error", run_id=ctx.run_id,
                            error=str(exc)[:200])
        return {"validation": result}

    def _repair_goal_contract(
        self, ctx: RunContext, validation: Any,
    ) -> Optional[Mapping[str, Any]]:
        """Repair deterministic artifact-contract failures before the run settles."""
        if not validation or not getattr(validation, "accepted", False):
            return None
        changed = list(getattr(validation, "changed_files", []) or [])
        contract = self._check_goal_contract(ctx, changed)
        if contract is None or contract.ok or self.router is None:
            return None

        initial_unmet = list(contract.unmet)
        self._event(
            "pipeline.contract_repair_started",
            run_id=ctx.run_id,
            unmet=contract.unmet[:6],
        )

        def propose(phase: str, prompt_text: str, iteration: int, hint: str) -> ProposeResult:
            text = self._ask(phase, prompt_text, ctx)
            return ProposeResult(text=text, diff=extract_diff(text), phase=phase)

        context = self._file_context(ctx.state.goal)
        max_rounds = max(1, int(ctx.extras.get("max_fix_iterations") or 2))
        repaired = None
        applied = False
        for repair_round in range(max_rounds):
            guidance: list[str] = []
            unmet_text = " ".join(contract.unmet).lower()
            if "reduced-motion" in unmet_text:
                guidance.append(
                    "Use a valid CSS query exactly like "
                    "`@media (prefers-reduced-motion: reduce) { ... }`."
                )
            if "responsive mobile navigation" in unmet_text:
                guidance.append(
                    "Add a real mobile menu button with aria-expanded and "
                    "aria-controls, hide/show the navigation at the mobile "
                    "breakpoint, and use a click handler to toggle both the "
                    "menu and aria-expanded."
                )
            if "colour 'cyan'" in unmet_text:
                guidance.append(
                    "Use cyan in an actual CSS color declaration, such as "
                    "`rgba(0, 255, 255, 0.18)`; a variable name or comment alone "
                    "does not count."
                )
            if "colour 'magenta'" in unmet_text:
                guidance.append(
                    "Use magenta in an actual CSS color declaration, such as "
                    "`rgba(255, 0, 255, 0.16)`; a variable name or comment alone "
                    "does not count."
                )
            if "working form with validation and a success state" in unmet_text:
                guidance.append(
                    "Bind submit handling to the actual <form>, prevent its "
                    "default navigation, validate the required fields, and show "
                    "a visible aria-live success message without reloading."
                )
            if guidance:
                guidance.append(
                    "Edit the existing document in place. Keep CSS inside the "
                    "existing <style> in <head>; do not append markup or styles "
                    "after </html>."
                )
            feedback = (
                "The deterministic artifact acceptance contract rejected the current "
                "files. Repair these specific unmet requirements without replacing "
                "unrelated content:\n"
                + "\n".join(f"- {item}" for item in contract.unmet)
                + (
                    "\n\nImplementation details:\n"
                    + "\n".join(f"- {item}" for item in guidance)
                    if guidance else ""
                )
            )
            if repair_round:
                feedback += (
                    "\n\nA previous diff passed syntax/application checks but these "
                    "requirements remain unmet. Inspect the current file context and "
                    "make a targeted correction."
                )
            repaired = self.fixloop.run(
                ctx.state.goal,
                propose_fn=propose,
                initial_diff="",
                ctx=ctx,
                slice_text=context,
                tests_output=feedback,
                max_iterations=1,
                run_id=ctx.run_id,
                approve=ctx.extras.get("approve"),
                path_hint=_diff_path_hint(ctx),
            )
            if not repaired.accepted:
                break
            applied = True
            contract = self._check_goal_contract(ctx, changed)
            if contract is None or contract.ok:
                break
            context = self._file_context(ctx.state.goal)

        contract_satisfied = contract is not None and contract.ok
        remaining = list(contract.unmet) if contract is not None else initial_unmet
        final_diff = self._episode_diff(ctx, changed) if applied else ""
        self._event(
            "pipeline.contract_repair_finished",
            run_id=ctx.run_id,
            accepted=contract_satisfied,
            attempts=repair_round + 1,
            unmet=len(remaining),
        )
        return {
            "fix_report": {
                "verdict": "contract satisfied" if contract_satisfied else "contract repair incomplete",
                "initial_unmet": initial_unmet,
                "contract_unmet": remaining,
                "repair": repaired.to_dict() if repaired is not None else {},
            },
            "fixed_diff": final_diff or (repaired.diff if applied and repaired else ""),
        }

    def _node_fix(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        validation = inputs.get("validation")
        loop_result: Optional[FixLoopResult] = ctx.extras.get("fix_result")
        self_debug = self._self_debug(ctx, validation) if validation is not None else {}

        # ReAct: only when the tools actually complained and the caller opted in.
        # One observation → one repair → re-validate is the whole loop; a clean
        # tool run never costs an extra model call.
        react_fix = ""
        if ctx.extras.get("react") and (self_debug.get("lint_errors")
                                        or self_debug.get("tests_failed")):
            react_fix = self._react_repair(ctx, inputs.get("proposal"), self_debug)
            if react_fix:
                ctx.extras["react_fix"] = True
                self_debug["react"] = "repaired in one observation loop"
                return {"fix_report": {"iterations": 1, "self_debug": self_debug,
                                       "verdict": "repaired via react"},
                        "fixed_diff": react_fix}

        contract_repair = self._repair_goal_contract(ctx, validation)
        if contract_repair is not None:
            return contract_repair

        if validation is not None and validation.accepted:
            return {"fix_report": {"skipped": "validation already accepted",
                                   "iterations": 0,
                                   "self_debug": self_debug},
                    "fixed_diff": ""}

        if loop_result is not None:
            # The loop already spent its budget inside ``act``; report it rather
            # than starting a second, competing loop that could spend the budget
            # twice for the same task.
            report = loop_result.to_dict()
            if self_debug.get("lint_errors") or self_debug.get("tests_failed"):
                # The loop said "accepted", the tools disagree.  Say so out loud
                # instead of shipping a green report over a red test.
                report["self_debug"] = self_debug
                report["verdict"] = "rejected by tools"
            # A rejection that came from the *test gate* is different from a broken
            # diff: the code is in place and only the behaviour is wrong.  Repair it
            # against the real failures instead of rolling the work back and starting
            # from stubs again — that loop never converges.
            reason = str(getattr(validation, "reason", "") or "")
            if "test suite failed" in reason and self.router is not None:
                def propose_test(phase: str, prompt_text: str, iteration: int,
                                 hint: str) -> ProposeResult:
                    text = self._ask(phase, prompt_text, ctx)
                    return ProposeResult(text=text, diff=extract_diff(text), phase=phase)

                repaired = self.fixloop.run(
                    ctx.state.goal, propose_fn=propose_test, initial_diff="", ctx=ctx,
                    tests_output=reason, max_iterations=2, run_id=ctx.run_id,
                    approve=ctx.extras.get("approve"),
                )
                report["test_repair"] = repaired.to_dict()
                return {"fix_report": report,
                        "fixed_diff": repaired.diff if repaired.accepted else ""}
            return {"fix_report": report,
                    "fixed_diff": loop_result.diff if loop_result.accepted else ""}

        diff = getattr(inputs.get("proposal"), "diff", "") or ""
        if not diff or self.router is None:
            return {"fix_report": {"skipped": "nothing to repair"}, "fixed_diff": ""}

        def propose(phase: str, prompt_text: str, iteration: int, hint: str) -> ProposeResult:
            text = self._ask(phase, prompt_text, ctx)
            return ProposeResult(text=text, diff=extract_diff(text), phase=phase)

        result = self.fixloop.run(
            ctx.state.goal, propose_fn=propose, initial_diff=diff, ctx=ctx,
            max_iterations=int(ctx.extras.get("max_fix_iterations") or 0) or None,
            dry_run=bool(ctx.extras.get("dry_run")), run_id=ctx.run_id,
        )
        return {"fix_report": result.to_dict(), "fixed_diff": result.diff if result.accepted else ""}

    @staticmethod
    def _winning_diff(inputs: Mapping[str, Any]) -> str:
        """The diff the run is about to accept: the repair loop's if it won, else the proposal's."""
        fixed = str(inputs.get("fixed_diff", "") or "")
        if fixed:
            return fixed
        return getattr(inputs.get("proposal"), "diff", "") or ""

    def _node_review(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        """Adjudicate the winning diff beyond the deterministic gate.

        A cache hit short-circuits: the diff was accepted on a previous run with
        the same task + slice + commit, so re-reviewing would spend tokens to
        re-derive a verdict that is already stored.
        """
        diff = self._winning_diff(inputs)
        strategy_name = self.review_strategy.name
        if not diff or inputs.get("cache_hit"):
            return {"review_verdict": ReviewOutcome(
                verdict="ok", strategy=strategy_name,
                issues=[] if diff else ["nothing to review"],
            ).to_dict()}
        if self.router is None:
            return {"review_verdict": ReviewOutcome(
                verdict="ok", strategy=strategy_name,
                issues=["review skipped: no model client wired"],
            ).to_dict()}

        outcome: ReviewOutcome = self.review_strategy.review(
            diff,
            goal=ctx.state.goal,
            ask=lambda phase, prompt, temperature=None: self._ask(
                phase, prompt, ctx, temperature=temperature
            ),
            slice_obj=inputs.get("slice"),
        )
        if outcome.rejected:
            ctx.state.bump("review_reject")
            for issue in outcome.issues[:5]:
                ctx.state.add_note(f"review: {issue}")
            # An advisory rejection is a *suggestion*, not a verdict.
            #
            # Measured live on a two-file bug fix: the diff was correct — both
            # functions fixed, the repo's own tests passed — and the reviewer
            # rejected it three cycles running for "the comment added in
            # math_utils.py", "the comment '# Updated implementation'", and
            # "the function signature for 'add'".  Every cycle was discarded and
            # the run ended with the original bug.
            #
            # A model asked "is this good?" will always find something; that is
            # what makes it useful as advice and useless as a gate.  The gate is
            # the deterministic one — the validator, the constitution's hard
            # principles, the goal contract — and it already ran.  So a
            # model-only rejection is recorded, fed back to the coder as a note,
            # and does not fail the run on its own.
            if outcome.advisory:
                ctx.state.bump("review_advisory")
                self._event(
                    "pipeline.review_advisory", run_id=ctx.run_id,
                    issues=outcome.issues[:5],
                )
                return {"review_verdict": {**outcome.to_dict(), "verdict": "ok"}}
        else:
            ctx.state.bump("review_pass")
        self._event(
            "pipeline.review",
            run_id=ctx.run_id,
            verdict=outcome.verdict,
            strategy=outcome.strategy,
            issues=len(outcome.issues),
            llm=outcome.llm_used,
        )
        return {"review_verdict": outcome.to_dict()}

    def _owned_files(self, ctx: RunContext) -> Any:
        """The files the harness wrote, with their ownership policy.

        A page the harness **assembled** from a plan is ``AUTHORITATIVE``: its
        bytes are the artifact, and a model edit to it is a regression.

        A page the harness wrote as a **baseline** — an existing page being
        edited, where the harness seeded the file so the model had something to
        change — is ``SEED``: the model's edits are the work, and writing the
        harness's bytes back over them would discard exactly the change the run
        was asked to make.  This is the case the review found: an edit of an
        existing page had its fix silently reverted.
        """
        from gigaforge.harness.ownership import OwnedFiles, Policy

        content = ctx.extras.get("harness_owned_content") or {}
        if not content:
            return OwnedFiles()
        generation = ctx.extras.get("generation") or {}
        policies: dict[str, Policy] = {}
        if generation.get("edit"):
            # The page existed before this run: the harness seeded it.
            for path in content:
                policies[str(path)] = Policy.SEED
        return OwnedFiles.from_mapping(content, policies=policies)

    def _restore_harness_owned(self, ctx: RunContext) -> list[str]:
        """Write the harness's own files back over whatever the model applied.

        The fix loop applies the model's diff to the worktree.  For a path the
        harness built itself, that application is a regression: the assembled
        page is the plan's output, and the model's answer is a guess at the same
        file.  Measured live: 5452 bytes assembled, 1139 bytes after the model's
        diff, and the contract then reported the sections the harness had built
        as missing — the run failed on work it had already done correctly.

        **This used to be the third of three mechanisms solving one problem, and
        it was the one that did the damage.**  It wrote the harness's bytes over
        the file unconditionally, *after* the model's diff, so a model that fixed
        a real bug in that file had its fix silently discarded.  Ownership was
        absolute because nothing in the code could say it was not.

        It now asks the ownership type, which is the one place the invariant
        lives: an ``AUTHORITATIVE`` file is restored, a ``SEED`` file is left
        exactly as the model left it.  The three mechanisms are one call
        (:meth:`OwnedFiles.resolve`); this method is kept as the tree-side half
        of it and no longer decides anything on its own.

        Returns the paths that were actually rewritten, so the trace shows it.
        """
        owned = self._owned_files(ctx)
        if not owned:
            return []
        from gigaforge.harness.ownership import Policy

        root = self._repo_root()
        restored: list[str] = []
        for entry in owned.files.values():
            if entry.policy is not Policy.AUTHORITATIVE:
                # A seeded file is the model's to change.  Writing the harness's
                # baseline back over it would discard exactly the edit the run
                # was asked to make.
                continue
            target = Path(root) / entry.path
            try:
                current = target.read_text(encoding="utf-8") if target.is_file() else ""
            except OSError:
                current = ""
            if current == entry.content:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry.content, encoding="utf-8")
            restored.append(entry.path)
        return restored

    def _node_repair(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        """Repair generated HTML structurally, before anything grades it.

        **Why this is a node and not a step inside the contract.**  The repair
        used to live at the top of ``_check_goal_contract``, after two early
        returns::

            requirements = extract_requirements(ctx.state.goal)
            if not requirements:
                return None          # ← repair never ran
            files = [...]
            if not files:
                return None          # ← nor here
            self._repair_html_artifacts(ctx, files)

        ``extract_requirements`` returns **zero** requirements for "Create a
        single-file index.html", "Build a landing page" and "Make a website for
        my bakery" — three of the four ordinary ways to ask for a page.  So the
        repair was unreachable for exactly the goals that need it, and a broken
        page passed.  A repair that only runs when a *different* check happens
        to have something to say is not a repair, it is a coincidence.

        As its own node it runs for every run that changed an ``.html`` file,
        whether or not the goal produced requirements, and it runs before the
        contract so the contract grades the page a browser will render.

        It also recomputes ``final_diff``: the diff was computed from the
        pre-repair file, so without this the run reports a page that is not the
        page on disk — measured, ``final_diff`` had 4 sections where the tree
        had 1.
        """
        validation = inputs.get("validation")
        changed = list(dict.fromkeys(
            str(p) for p in (getattr(validation, "changed_files", []) or []) if str(p).strip()
        ))
        if not changed:
            return {"repair_report": {"files": [], "actions": []}}
        before = len(ctx.extras.get("html_repairs") or [])
        self._repair_html_artifacts(ctx, changed)
        repairs = list(ctx.extras.get("html_repairs") or [])[before:]
        if not repairs:
            return {"repair_report": {"files": [], "actions": []}}

        # The tree changed under the diff.  Recompute it from the worktree so
        # every consumer of ``final_diff`` — summary, cache, report, persist —
        # sees the file that is actually there.
        repaired_files = [str(item.get("file")) for item in repairs if item.get("file")]
        recomputed = self._episode_diff(ctx, repaired_files)
        if recomputed:
            ctx.extras["repaired_diff"] = recomputed
            self._event("pipeline.repair_diff_recomputed", run_id=ctx.run_id,
                        files=len(repaired_files), chars=len(recomputed))
        return {"repair_report": {
            "files": repaired_files,
            "actions": [a for item in repairs for a in (item.get("actions") or [])],
        }}

    def _node_settle(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        validation = inputs.get("validation")
        final = self._winning_diff(inputs)
        # The repair node rewrote files on disk after the diff was computed.
        # ``final_diff`` must describe the tree that exists, not the tree that
        # existed before the repair — measured, the diff had 4 sections where
        # the file had 1, and every consumer of the diff (summary, cache,
        # report, persist) was reading a page nobody would ever see.
        repaired = str(ctx.extras.get("repaired_diff") or "")
        if repaired:
            final = _merge_repaired_diff(final, repaired)
        validated = bool(validation and validation.accepted)
        # An answer goal settles on its own verdict.  The artifact/code gates
        # below all ask "is the tree right?", which is not a question a reply
        # can answer: running them would reject every answer for having no
        # changed files.
        if _task_kind_for(self, ctx).is_answer:
            answer = str(ctx.extras.get("answer_text") or "")
            self._event(
                "pipeline.settle_answer", run_id=ctx.run_id,
                accepted=validated, chars=len(answer),
            )
            return {
                "accepted": validated,
                "changed_files": [],
                "final_diff": "",
                "answer": answer,
                "answer_citations": list(ctx.extras.get("answer_citations") or []),
                "answer_searched": bool(ctx.extras.get("answer_searched")),
            }
        verdict = inputs.get("review_verdict") or {}
        review_rejected = str((verdict or {}).get("verdict") or "ok") == "reject"
        # The review stage can only ever *narrow* an acceptance: a diff the
        # deterministic gate rejected is never revived by an approving reviewer.
        accepted = validated and not review_rejected
        changed = list(dict.fromkeys(
            str(p) for p in (getattr(validation, "changed_files", []) or []) if str(p).strip()
        ))
        # The goal contract is a GATE, not advice.
        #
        # It used to be advisory: the episode was told what was missing, and if
        # the model finished anyway the run still settled ``accepted: true``.
        # Measured live: a cyberpunk landing page whose body was four
        # "Feature Card 1" divs and a "Footer Content" div settled
        # ``status: succeeded, accepted: true`` — the contract had fired twice
        # (the trace shows the two correction steps) and the run reported
        # success regardless.  A check that cannot change the verdict is
        # decoration.
        #
        # The check runs here, on the files on disk, because this is the only
        # place that knows both the goal and the tree the run wrote to.  It is
        # deterministic and model-free, so it cannot be talked out of a verdict.
        contract = self._check_goal_contract(ctx, changed)
        if contract is not None and not contract.ok:
            accepted = False
            ctx.extras["contract_unmet"] = list(contract.unmet)
            self._event(
                "pipeline.contract_rejected", run_id=ctx.run_id,
                unmet=contract.unmet[:6], files=contract.files[:6],
            )
        structure_unmet = self._check_html_structure(changed)
        if structure_unmet:
            accepted = False
            ctx.extras["contract_unmet"] = list(
                ctx.extras.get("contract_unmet") or []
            ) + structure_unmet
            self._event(
                "pipeline.html_structure_rejected",
                run_id=ctx.run_id,
                unmet=structure_unmet[:6],
                files=changed[:6],
            )
        # A page is a RENDERED artifact.  The contract counts tags; a browser
        # shows what the tags produce, and the two disagreed twice: 1956
        # characters of CSS printed as text in the body, a doubled grid and
        # footer — every one of them invisible to a tag counter and to a size
        # check.  So for an artifact goal the page is rendered and the facts a
        # browser computes are part of the verdict.
        render_unmet = self._check_render(ctx, changed)
        if render_unmet:
            accepted = False
            ctx.extras["contract_unmet"] = list(ctx.extras.get("contract_unmet") or []) + render_unmet
        # A code change is graded by a contract too.  "The tests passed" is both
        # too weak (a diff that touches files the goal never named, or deletes a
        # public function, passes as long as some test is green) and too strong
        # (a correct change the tests do not cover is rejected for a reason that
        # has nothing to do with the change).  The four facts a code change can
        # be held to without guessing are the files it touched, the tests, the
        # linter, and the public symbols it kept.
        code_unmet = self._check_code_contract(ctx, changed)
        if code_unmet:
            accepted = False
            ctx.extras["contract_unmet"] = list(ctx.extras.get("contract_unmet") or []) + code_unmet
        # A partial fan-out cannot settle as accepted.  The accepted children's
        # work is kept — it is already in the tree — but the run is not done,
        # and the goal loop must spend another cycle on what is left.  Measured:
        # two children, one merged, one rejected, and the run reported success
        # with one of the two files fixed.
        partial = ctx.extras.get("subagent_partial")
        if partial:
            accepted = False
            ctx.extras["contract_unmet"] = list(ctx.extras.get("contract_unmet") or []) + [
                "sub-agent work was rejected: "
                + ", ".join(str(r.get("name")) for r in partial.get("rejected") or [])
            ]
            self._event("pipeline.subagents_partial", run_id=ctx.run_id,
                        accepted=partial.get("accepted"),
                        rejected=len(partial.get("rejected") or []))
        focused_creation_partial = ctx.extras.get("focused_creation_partial")
        if focused_creation_partial:
            accepted = False
            ctx.extras["contract_unmet"] = list(ctx.extras.get("contract_unmet") or []) + [
                "focused file creation was partial: "
                + ", ".join(str(path) for path in focused_creation_partial[:4])
            ]
            self._event(
                "pipeline.focused_creation_partial",
                run_id=ctx.run_id,
                missing=list(focused_creation_partial)[:4],
            )
        budget_overrun = ctx.extras.get("subagent_budget_overrun")
        if budget_overrun:
            accepted = False
            ctx.extras["contract_unmet"] = list(ctx.extras.get("contract_unmet") or []) + [
                "sub-agent work exceeded its allocated token budget"
            ]
        ctx.state.artifacts["final_diff"] = final
        ctx.state.artifacts["accepted"] = accepted
        ctx.state.artifacts["review"] = dict(verdict or {})
        if contract is not None:
            ctx.state.artifacts["contract"] = {
                "ok": contract.ok,
                "completeness": round(contract.completeness, 4),
                "checked": contract.checked,
                "met": contract.met,
                "unmet": contract.unmet,
            }
            ctx.extras["completeness"] = round(contract.completeness, 4)
        ctx.state.add_note(
            f"settle: accepted={accepted} diff_chars={len(final)} files={len(changed)} "
            f"review={verdict.get('verdict', 'ok') if isinstance(verdict, Mapping) else 'ok'}"
            + (f" contract_unmet={len(contract.unmet)}" if contract is not None else "")
        )
        self._event("pipeline.settle", run_id=ctx.run_id, accepted=accepted,
                    diff_chars=len(final), files=len(changed),
                    reviewed=bool(verdict), review_rejected=review_rejected)
        return {
            "final_diff": final, "accepted": accepted, "changed_files": changed,
            # Declared in ``provides``, so they must be returned on every path.
            # Empty here: this is a diff-producing run, and an empty answer is
            # the honest value for it.
            "answer": "", "answer_citations": [], "answer_searched": False,
        }

    def _check_code_contract(self, ctx: RunContext, changed: Sequence[str]) -> list[str]:
        """Grade a code change: files, tests, linter, public symbols.

        Only for code goals.  An artifact goal has its own contract (the plan's
        sections, the palette, the render) and holding a page to a Python
        symbol check would be a check of something the run never claimed.

        The "before" content comes from the run's own baseline: the worktree is
        a copy of the repository, so ``git show HEAD:path`` is what the file
        looked like when the run started.  Without it the public-symbol check
        cannot tell a removal from a rename.
        """
        kind = ctx.extras.get("task_kind") or {}
        if kind.get("kind") not in ("code", "mixed"):
            return []
        try:
            from gigaforge.harness.code_contract import CodeContract, check_code_contract
        except Exception as exc:  # noqa: BLE001 - an optional module
            self._degraded("code_contract_import", exc, "settle")
            return []
        allowed = tuple(kind.get("code_paths") or ())
        before: dict[str, str] = {}
        root = self._repo_root()
        for path in changed:
            if not str(path).endswith(".py"):
                continue
            try:
                proc = subprocess.run(
                    ["git", "show", f"HEAD:{path}"], cwd=root,
                    capture_output=True, text=True, timeout=20,
                )
            except Exception as exc:  # noqa: BLE001 - no git, no baseline
                # Without a baseline the public-symbol check cannot tell a
                # removal from a rename, so it is skipped -- and the report must
                # say the check did not run rather than imply it passed.
                self._degraded("code_contract_baseline", exc, "settle")
                continue
            if proc.returncode == 0:
                before[str(path)] = proc.stdout
        verdict = check_code_contract(
            CodeContract(allowed_paths=allowed),
            root,
            changed=changed,
            before=before,
        )
        ctx.extras["code_contract"] = verdict.to_dict()
        # A code goal's completeness is the same arithmetic on its own verdict:
        # the checks that passed over the checks that ran.  Without this a code
        # run reported ``completeness: None`` while an artifact run reported a
        # number, and the report could not compare the two modes.
        if ctx.extras.get("completeness") is None:
            total = len(verdict.unmet) + len(verdict.not_checked)
            if total:
                ctx.extras["completeness"] = round(
                    max(0.0, 1.0 - len(verdict.unmet) / total), 4
                )
        self._event("pipeline.code_contract", run_id=ctx.run_id,
                    ok=verdict.ok, unmet=verdict.unmet[:4],
                    not_checked=verdict.not_checked[:4])
        return [f"code: {item}" for item in verdict.unmet]

    def _check_render(self, ctx: RunContext, changed: Sequence[str]) -> list[str]:
        """Render the page the run wrote and return what a browser disagrees with.

        Only for artifact goals: a code change has no page to render, and
        rendering one would be a check of something the run never claimed.

        A missing browser is a **degradation**, not a pass and not a failure:
        the run records that the page was not rendered, so the report can say
        "not checked" instead of implying "checked and fine".
        """
        kind = ctx.extras.get("task_kind") or {}
        if not kind.get("kind") or kind.get("kind") == "code":
            return []
        pages = [p for p in changed if str(p).lower().endswith((".html", ".htm"))]
        if not pages:
            return []
        root = self._repo_root()
        page = (root / str(pages[0])).resolve()
        try:
            page.relative_to(root)
        except ValueError:
            return []
        if not page.is_file():
            return []
        try:
            from gigaforge.harness.render_check import render_facts, render_verdict
        except Exception as exc:  # noqa: BLE001 - an optional module
            self._degraded("render_check_import", exc, "settle")
            return []
        shot = Path(str(getattr(self.config, "trace_path", ".gigaforge/traces/core.jsonl"))).parent / f"{ctx.run_id}_render.png"
        facts = render_facts(page, screenshot=shot)
        ctx.extras["render"] = facts
        if not facts.get("available"):
            ctx.state.note_degraded(
                what="render_check", why=str(facts.get("reason") or "unavailable"),
                where="settle",
            )
            self._event("pipeline.render_unavailable", run_id=ctx.run_id,
                        reason=str(facts.get("reason"))[:200])
            return []
        plan = ctx.extras.get("generation_plan") or {}
        min_cards = 3 if "features" in (plan.get("sections") or []) else 0
        unmet = render_verdict(facts, min_cards=min_cards)
        self._event("pipeline.render", run_id=ctx.run_id,
                    h1=facts.get("h1"), cards=facts.get("cards"),
                    css_leak=facts.get("css_leak_chars"),
                    unmet=unmet[:4])
        return [f"render: {item}" for item in unmet]

    def _check_html_structure(self, changed: Sequence[str]) -> list[str]:
        """Return structural HTML defects that deterministic repair could not fix."""
        pages = [str(path) for path in changed
                 if Path(str(path)).suffix.lower() in {".html", ".htm"}]
        if not pages:
            return []
        try:
            from gigaforge.harness.html_structure import audit_html_structure
        except Exception as exc:  # noqa: BLE001 - a missing optional verifier
            self._event("pipeline.html_structure_unavailable", error=str(exc)[:200])
            return []

        root = self._repo_root()
        unmet: list[str] = []
        for raw in pages:
            page = (root / raw).resolve()
            try:
                page.relative_to(root)
            except ValueError:
                continue
            if not page.is_file():
                continue
            try:
                report = audit_html_structure(page.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                self._event("pipeline.html_structure_error",
                            file=raw, error=str(exc)[:200])
                continue
            unmet.extend(f"HTML structure: {issue}" for issue in report.issues())
            unmet.extend(_broken_anchors(page.read_text(encoding="utf-8", errors="replace")))
        return unmet

    def _check_goal_contract(self, ctx: RunContext, changed: Sequence[str]) -> Any:
        """Check the artifact on disk against the goal's explicit requirements.

        Returns ``None`` when the goal names nothing checkable, so a goal like
        "fix the failing test" is not held to a design checklist it never set.

        The files checked are the ones the run changed, resolved against the
        repository root.  When the run changed nothing there is nothing to
        check and the verdict is ``None`` — a run that produced no diff is
        already rejected by the validation gate, and reporting a second,
        unrelated reason for it would be noise.
        """
        try:
            from gigaforge.harness.goal_contract import (
                check_artifacts,
                extract_requirements,
                requirements_from_plan,
            )

            files = [str(p) for p in changed if str(p).strip()]
            if not files:
                return None
            has_html_artifact = any(
                Path(path).suffix.lower() in {".html", ".htm"} for path in files
            )
            # An HTML artifact is graded against the plan the harness itself made.
            # ``extract_requirements`` returns nothing for "Create a single-file
            # index.html", "Build a landing page" and "Make a website for my
            # bakery" — so the gate was absent exactly where it was needed, and
            # a page with no hero and four "Feature Card 1" divs passed.  The
            # plan always exists for a page goal, so the contract always exists.
            requirements = []
            generation = ctx.extras.get("generation") or {}
            # The gate is "this run produced an HTML artifact", NOT "this run
            # used sectional generation".  Measured live: `run` (agent mode)
            # produced a page with four cards and **no footer** for a goal that
            # asked for a footer, and reported ``accepted: True`` — because
            # ``generation.sections`` is only populated by the sectional path,
            # so the plan-derived contract was skipped and the run fell back to
            # ``extract_requirements``, which returns almost nothing for a page
            # goal.  One artifact was judged by two different standards and the
            # weaker one was the one people use.
            if has_html_artifact:
                try:
                    from gigaforge.harness.generation import (
                        GenerationPlan,
                        SectionPlan,
                        plan_generation,
                    )

                    # The plan the page was BUILT from, not a fresh derivation
                    # from the goal text.  Re-deriving was a second source of
                    # truth: on an edit the build plan is the union of the
                    # page's existing sections and the goal's, and re-deriving
                    # from the goal alone produced a different plan — so the
                    # contract demanded sections the assembler never planned.
                    stored = ctx.extras.get("generation_plan") or {}
                    if stored.get("sections"):
                        plan = GenerationPlan(
                            path=str(stored.get("path") or "index.html"),
                            sections=[
                                SectionPlan(kind=kind, heading=kind.title(), index=index)
                                for index, kind in enumerate(stored["sections"])
                            ],
                            palette=tuple(stored.get("palette") or ()),
                            title=str(stored.get("title") or ""),
                            goal=_original_goal(ctx.state.goal),
                        )
                    else:
                        # No stored plan (a non-sectional run, or an edit): the
                        # goal is the only source, and ``plan_generation`` is
                        # deterministic, so deriving it here is not a guess.
                        plan = plan_generation(_original_goal(ctx.state.goal))
                    requirements = requirements_from_plan(plan)
                    self._event("pipeline.contract_from_plan", run_id=ctx.run_id,
                                sections=plan.kinds(), palette=list(plan.palette),
                                requirements=len(requirements),
                                from_stored_plan=bool(stored.get("sections")))
                except Exception as exc:  # noqa: BLE001 - fall back to the goal text
                    self._event("pipeline.contract_plan_error", run_id=ctx.run_id,
                                error=str(exc)[:200])
            explicit_requirements = extract_requirements(_original_goal(ctx.state.goal))
            known_labels = {requirement.label for requirement in requirements}
            requirements.extend(
                requirement
                for requirement in explicit_requirements
                if requirement.label not in known_labels
            )
            if not requirements:
                return None
            # The repair is NOT done here.  It used to be, after these two early
            # returns — and ``extract_requirements`` returns nothing for "Create
            # a single-file index.html", so the repair never ran for the goals
            # that need it.  It is its own node now (``_node_repair``), which
            # runs before this check for every run that changed an .html file.
            return check_artifacts(requirements, self._repo_root(), files=files)
        except Exception as exc:  # noqa: BLE001 - a broken check must not fail a run
            self._event("pipeline.contract_error", run_id=ctx.run_id,
                        error=str(exc)[:300])
            return None

    def _repair_html_artifacts(self, ctx: RunContext, files: Sequence[str]) -> None:
        """Fix structural HTML defects that have exactly one correct repair.

        Measured live: a goal run produced a page whose stylesheet appeared twice
        — once in ``<style>`` and once as 1956 characters of raw text in
        ``<body>`` — with a stray ``</style>`` in the body and the feature grid
        and footer each emitted twice.  Every check the harness had passed it.

        The repair is deterministic and conservative (see
        :mod:`gigaforge.harness.html_structure`): it only moves CSS that is
        provably CSS and only deletes blocks that are byte-identical after
        whitespace normalisation.  Anything it cannot prove is left alone and
        reported by the verifier instead.

        This runs *before* the contract check so the contract grades the page the
        browser will actually render, not the broken source.
        """
        try:
            from gigaforge.harness.html_structure import repair_html_structure
        except Exception as exc:  # pragma: no cover - import guard
            self._degraded("html_repair_import", exc, "repair")
            return
        root = self._repo_root()
        for raw in files:
            path = (root / str(raw)).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if path.suffix.lower() not in {".html", ".htm"} or not path.is_file():
                continue
            try:
                original = path.read_text(encoding="utf-8", errors="replace")
                repaired, actions = repair_html_structure(original)
            except Exception as exc:  # noqa: BLE001 - repair is best-effort
                self._event("pipeline.html_repair_error", run_id=ctx.run_id,
                            file=str(raw), error=str(exc)[:300])
                continue
            if not actions or repaired == original:
                continue
            try:
                path.write_text(repaired, encoding="utf-8")
            except OSError as exc:
                self._event("pipeline.html_repair_error", run_id=ctx.run_id,
                            file=str(raw), error=str(exc)[:300])
                continue
            ctx.extras.setdefault("html_repairs", []).append(
                {"file": str(raw), "actions": actions}
            )
            self._event("pipeline.html_repaired", run_id=ctx.run_id,
                        file=str(raw), actions=actions[:6])

    def _node_summarize(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        diff = str(inputs.get("final_diff", "") or "")
        accepted = bool(inputs.get("accepted"))
        # An answer goal's summary IS the answer.  Summarising it through a
        # second model call would paraphrase the deliverable and could drop a
        # citation — and the citations are what the gate just checked.  Read
        # from the context rather than a DAG artifact: the answer path is
        # orthogonal to the diff graph, and wiring it as a dependency would make
        # every graph that does not have an answer node fail to validate.
        answer = str(ctx.extras.get("answer_text") or "")
        if answer:
            return {"summary": answer}
        cached_summary = str((ctx.extras.get("cached_value") or {}).get("summary", "") or "")
        if cached_summary:
            return {"summary": cached_summary}
        if self.router is None or not diff:
            return {"summary": _fallback_summary(accepted, diff)}
        if not accepted:
            # Nothing valid to describe.  Asking the model to summarise a rejected
            # diff buys a paragraph that contradicts the run's own verdict.
            return {"summary": _fallback_summary(accepted, diff)}
        prompt = build_phase_prompt(
            "summarize",
            goal=ctx.state.goal,
            extra_blocks=[("Outcome", f"accepted={accepted}"), ("Diff", diff[:4000])],
        )
        try:
            summary = self._ask("summarize", prompt.render(), ctx).strip()
        except Exception as exc:  # noqa: BLE001 - a summary is never critical
            self._event("pipeline.summary_error", run_id=ctx.run_id, error=str(exc)[:300])
            summary = _fallback_summary(accepted, diff)
        return {"summary": summary or _fallback_summary(accepted, diff)}

    def _node_persist(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        accepted = bool(inputs.get("accepted"))
        diff = str(inputs.get("final_diff", "") or "")
        key = ""
        if accepted and diff and ctx.extras.get("use_cache", True):
            # The same string the lookup used, so a write can only ever land on
            # the key the read asked for.
            key = str(inputs.get("cache_key") or "")
            if key:
                self.cache.put(key, {"diff": diff, "summary": str(inputs.get("summary", "") or "")},
                               model=(self.router.last_route.model
                                      if self.router and self.router.last_route else ""))
        self.metrics.flush()
        memory = getattr(self, "memory", None)
        if memory is not None:
            # Durable lesson for the next run: success/failure, the step that
            # broke, and the summary the model produced.  Capped so memory stays a
            # hint, not a second transcript.
            try:
                from .memory import TaskOutcome

                memory.record(TaskOutcome(
                    task=ctx.state.goal,
                    task_hash=str(inputs.get("cache_key") or "")[:32],
                    success=accepted,
                    tokens=int(ctx.state.total_tokens() if callable(
                        getattr(ctx.state, "total_tokens", None)) else 0),
                    failed_step=str(getattr(inputs.get("validation"), "reason", "") or ""),
                    fixloop_iters=int(len(getattr(
                        ctx.extras.get("fix_result"), "iterations", []) or [])),
                    diff=diff[:2000],
                ))
            except Exception as exc:  # noqa: BLE001 - never fail a run on memory
                self._event("pipeline.memory_error", run_id=ctx.run_id,
                            error=str(exc)[:200])
        self._event("pipeline.persist", run_id=ctx.run_id, cached=bool(key),
                    accepted=accepted)
        return {"persisted": {"cache_key": key, "cached": bool(key)}}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _ask(self, phase: str, prompt: str, ctx: RunContext,
             *, temperature: Optional[float] = None) -> str:
        """Route a phase call through the router and return the answer text.

        ``temperature`` is threaded through for the tree search, whose whole
        point is sampling the same prompt at different temperatures.  A ``None``
        keeps the phase's configured temperature, so the linear path is byte-for-
        byte the call it always was.
        """
        if self.router is None:
            raise PipelineError("no model client is wired into the pipeline")
        messages = ctx.state.messages(extra=[{"role": "user", "content": prompt}])
        max_tokens = None
        budget = int(ctx.extras.get("budget_tokens") or 0)
        if budget:
            remaining = budget - ctx.state.total_tokens()
            prompt_chars = sum(
                len(str(message.get("content") or "")) + 12
                for message in messages
                if isinstance(message, Mapping)
            )
            estimated_input = (prompt_chars + 2) // 3
            output_allowance = remaining - estimated_input
            if output_allowance <= 0:
                raise PipelineError(
                    f"task token budget exhausted before the {phase!r} model call"
                )
            router_config = getattr(self.router, "config", None)
            budget_for = getattr(router_config, "budget_for", None)
            phase_budget = budget_for(phase) if callable(budget_for) else None
            configured_output = int(
                getattr(phase_budget, "max_output_tokens", 0) or 0
            )
            max_tokens = min(
                output_allowance,
                configured_output if configured_output > 0 else output_allowance,
            )
        result = self.router.complete(
            phase,
            messages,
            ctx=ctx,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        for attr in ("content", "text"):
            value = getattr(result, attr, None)
            if isinstance(value, str):
                return value
        return str(result)

    def _collect(
        self, run_id: str, goal: str, dag: DagResult, ctx: RunContext, *, started_ms: float
    ) -> PipelineResult:
        artifacts = dag.artifacts
        final_diff = str(artifacts.get("final_diff", "") or "")
        loop_result: Optional[FixLoopResult] = ctx.extras.get("fix_result")
        state = ctx.state
        nodes = [r.to_dict() for r in dag.records]
        if loop_result is not None:
            nodes.append({"node": "fixloop", **loop_result.to_dict()})
        error = str(dag.error or "")
        # The contract's verdict is the reason the run was refused, and it was
        # being dropped: ``settle`` recorded ``contract_unmet`` and moved on, so
        # ``_collect`` fell through to the fix-loop branch and reported
        # "Produced a change that could not be validated" with ``error: "ok"``.
        # A caller was told the run produced something un-validatable when the
        # truth was "found placeholders: 'feature 1', 'feature 2'".
        if not bool(artifacts.get("accepted")):
            unmet = list(ctx.extras.get("contract_unmet") or [])
            if unmet:
                contract_reason = "the artifact did not meet the goal: " + "; ".join(
                    str(item) for item in unmet[:6]
                )
                error = f"{contract_reason}; {error}" if error and error != "ok" else contract_reason
        if not error and not bool(artifacts.get("accepted")):
            review = artifacts.get("review_verdict") or {}
            if (str(review.get("verdict") or "") == "reject"
                    and not (loop_result is not None and not loop_result.accepted)):
                issues = [str(issue) for issue in review.get("issues", []) if issue]
                error = "review rejected the change"
                if issues:
                    error += ": " + "; ".join(issues[:5])
            elif loop_result is not None and loop_result.iterations:
                last = loop_result.iterations[-1]
                validation = last.validate
                detail = ""
                raw = getattr(validation, "raw", None) if validation is not None else None
                repair = getattr(raw, "last_repair", None) if raw is not None else None
                if repair is not None:
                    details = list(getattr(repair, "apply_errors", []) or [])
                    if not details and getattr(repair, "parse_error", ""):
                        details = [str(repair.parse_error)]
                    if not details:
                        details = [
                            f"{getattr(item, 'code', 'validation')}: "
                            f"{getattr(item, 'message', '')}"
                            for item in (getattr(repair, "diagnostics", []) or [])[:5]
                        ]
                    detail = "; ".join(str(item) for item in details[:5])
                if not detail and validation is not None:
                    detail = str(validation.repair_hint or validation.reason or "")
                error = str(last.reason or loop_result.reason or "change was rejected")
                if detail and detail not in error:
                    error += ": " + detail
            elif not final_diff:
                error = "the model did not produce a diff"
        return PipelineResult(
            run_id=run_id,
            goal=goal,
            # The DAG's own status says the *nodes* ran; it does not know that
            # the settle gate refused the artifact.  Reporting ``succeeded``
            # next to ``accepted: false`` is the contradiction the live run
            # showed: ``status=succeeded accepted=False`` on a page the contract
            # had just rejected.  A caller reading only ``status`` was told the
            # run worked.  The status now follows the verdict.
            status=(
                "rejected"
                if (dag.status == "succeeded" and not artifacts.get("accepted"))
                else dag.status
            ),
            accepted=bool(artifacts.get("accepted")),
            final_diff=final_diff,
            changed_files=list(artifacts.get("changed_files") or []),
            summary=str(artifacts.get("summary", "") or ""),
            answer=str(artifacts.get("answer", "") or ""),
            answer_citations=list(artifacts.get("answer_citations") or []),
            answer_searched=bool(artifacts.get("answer_searched")),
            plan=list(artifacts.get("plan") or []),
            cache_hit=bool(artifacts.get("cache_hit")),
            slice_hash=state.slice_hash,
            git_sha=state.git_sha,
            tokens=state.total_tokens(),
            calls=state.total_calls(),
            phase_usages={k: dict(v) for k, v in state.phase_usages.items()},
            nodes=nodes,
            dlq=list(dag.dlq),
            resumed_from=list(dag.resumed_from),
            duration_ms=(self.clock.timestamp() - started_ms) * 1000.0,
            error=error[:1500],
            dag=dag,
            # The plan's fate, read from the run context where the nodes put it.
            # These were added to the result and to the ledger and then never
            # populated — the live probe reported ``plan_used: false`` on a run
            # whose plan had demonstrably been used, which is the same
            # "field exists, product never reaches it" shape the field was added
            # to expose.
            plan_used=bool(ctx.extras.get("plan_used")),
            plan_steps_executed=int(ctx.extras.get("plan_steps_executed") or 0),
            plan_salvaged=bool(ctx.extras.get("plan_salvaged")),
            cost_micros=int(getattr(state, "cost_micros", 0) or 0),
            cost_usd=float(getattr(state, "cost_micros", 0) or 0) / 1_000_000,
            phase_cost_micros=dict(getattr(state, "phase_cost_micros", {}) or {}),
            cache_hit_rate=float(getattr(state, "cache_hit_rate", 0.0) or 0.0),
            cache_hits=int(getattr(state, "cache_hits", 0) or 0),
            cache_lookups=int(getattr(state, "cache_lookups", 0) or 0),
            degraded=list(getattr(state, "degraded", []) or []),
            contract_unmet=list(ctx.extras.get("contract_unmet") or []),
            completeness=ctx.extras.get("completeness"),
            diff_repaired=list(
                (ctx.extras.get("diff_repaired") or {}).get("repaired") or []
            ),
        )

    def _degraded(self, what: str, exc: BaseException, where: str = "") -> None:
        """Record a swallowed failure on the run's current state.

        A silent ``except`` that swallows a broken check makes a run look clean
        when it was merely unverified.  This is the helper for the places that
        do not have a ``RunContext`` in hand; it finds the active state and
        records ``what``/``why``/``where`` so the report can say "we did not
        check" instead of implying "we checked and it passed".
        """
        state = getattr(self, "_active_state", None)
        if state is None:
            return
        try:
            state.note_degraded(
                what=what, why=f"{type(exc).__name__}: {exc}", where=where
            )
        except Exception as exc:  # noqa: BLE001 - reporting must never fail a run
            # The degradation could not be recorded.  It must still not vanish:
            # a run whose own reporting is broken is exactly the run whose
            # failures need to be visible.
            self._event("pipeline.degraded_unrecorded", run_id="",
                        what=what, why=f"{type(exc).__name__}: {exc}", where=where)

    def _event(self, kind: str, **payload: Any) -> None:
        if self.metrics is not None:
            run_id = str(payload.get("run_id") or "")
            node = str(payload.get("node") or "")
            self.metrics.incr(f"core.{kind}", 1, run_id=run_id, node=node)
            self.metrics.trace.event(kind, **payload)

    def _new_run_id(self) -> str:
        stamp = self.clock.timestamp()
        return f"run-{int(stamp * 1000)}-{hash_text(str(stamp))[:8]}"

    # ── sub-agents ───────────────────────────────────────────────────────────
    def fan_out(
        self,
        specs: Sequence[SubAgentSpec],
        *,
        parent_ctx: Optional[RunContext] = None,
        parallel: bool = True,
    ) -> list[Any]:
        """Run sub-agents in isolated worktrees; results come back in spawn order."""
        from gigaforge.agents.engine import run_subagents

        runner = SubAgentRunner(
            self._subagent_body,
            config=self.config,
            metrics=self.metrics,
            trace=self.metrics.trace,
            clock=self.clock,
        )
        # Routed through the engine dispatcher so ``GIGAFORGE_SUBAGENT_ENGINE``
        # can move the fan-out onto the LangGraph graph without touching this
        # call site.  The classic path is unchanged: the production runner owns
        # the worktrees and takes the specs directly.
        return run_subagents(
            runner,
            specs,
            self._subagent_body,
            parent_ctx=parent_ctx,
            parallel=parallel,
        )

    def _sectional_generation(self, ctx: RunContext) -> str:
        """Build a page by fanning out over its sections, then assembling.

        The harness owns the plan and the skeleton; each child writes one
        section.  Assembly is deterministic, so the page is structurally valid
        by construction — one doctype, one ``<html>``, one ``<style>``, one
        ``<body>``, each section once — instead of by luck.

        A child that fails is not fatal: the section is retried once, and if it
        still fails the page is assembled from the sections that succeeded and
        the miss is reported, so a partial page beats no page.
        """
        from gigaforge.harness.generation import (
            assemble_page,
            existing_sections,
            plan_generation,
            section_prompt,
        )

        # An EDIT of an existing page runs the same sectional workflow as a
        # creation, seeded with the page that is already there.  Routing an
        # existing ``.html`` to the code path was the P0 defect: no section
        # plan, so ``requirements_from_plan`` never ran, so the contract fell
        # back to two bare regex requirements and graded an edit more weakly
        # than a creation.  Editing is the common case — you create a page once
        # and change it many times.
        kind_info = ctx.extras.get("task_kind") or {}
        edit_paths = list(kind_info.get("edit_paths") or [])
        root = self._repo_root()
        existing_html = ""
        existing_path = ""
        if edit_paths:
            candidate = (root / edit_paths[0]).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                candidate = None  # type: ignore[assignment]
            if candidate is not None and candidate.is_file():
                existing_path = edit_paths[0]
                existing_html = candidate.read_text(encoding="utf-8", errors="replace")
                self._event("pipeline.generation_edit_mode", run_id=ctx.run_id,
                            path=existing_path, bytes=len(existing_html))

        plan = plan_generation(_original_goal(ctx.state.goal), existing_html)
        if not plan.ok:
            return ""
        if existing_path:
            # The page being edited is the one that exists, whatever the goal
            # called it.
            plan = replace(plan, path=existing_path)
        self._event("pipeline.generation_plan", run_id=ctx.run_id,
                    path=plan.path, sections=plan.kinds(), palette=list(plan.palette),
                    edit=bool(existing_path))

        # Sections are independent by construction — each has its own kind, its
        # own anchor and its own fragment — so they are generated concurrently.
        # Measured: 3 sections × ~2 s = 6 s sequentially against ~2 s in
        # parallel; on 6 sections it is 12 s against 2 s.  A page is the one
        # goal where the harness knows the work is independent, so it should
        # not serialise it.
        fragments: dict[str, str] = {}
        failures: list[str] = []
        # On an edit, a section the page already has is kept verbatim unless the
        # goal names it.  Regenerating it would discard work the user already
        # had — and the goal "add a footer" does not ask for a new hero.
        present = existing_sections(existing_html) if existing_html else {}
        goal_low = _original_goal(ctx.state.goal).lower()
        to_build = [
            section for section in plan.sections
            if not (present.get(section.kind) and section.kind not in goal_low)
        ]
        if not to_build:
            to_build = list(plan.sections)

        def build_section(section: Any) -> tuple[str, str, list[str]]:
            prompt = section_prompt(plan, section, ctx.state.goal)
            errors: list[str] = []
            for attempt in (1, 2):
                try:
                    answer = self._ask("code", prompt, ctx)
                except Exception as exc:  # noqa: BLE001 - one section must not kill the page
                    errors.append(f"{section.kind} attempt {attempt}: {str(exc)[:200]}")
                    answer = ""
                if answer.strip():
                    return section.kind, answer, errors
            return section.kind, "", errors

        workers = min(len(to_build), int(getattr(self.config, "max_workers", 4) or 4))
        if workers > 1 and len(to_build) > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="section") as pool:
                outcomes = list(pool.map(build_section, to_build))
        else:
            outcomes = [build_section(section) for section in to_build]

        for kind, answer, errors in outcomes:
            for error in errors:
                self._event("pipeline.generation_section_error", run_id=ctx.run_id,
                            error=error)
            if not answer.strip():
                failures.append(kind)
                continue
            fragments[kind] = answer
            self._event("pipeline.generation_section", run_id=ctx.run_id,
                        section=kind, chars=len(answer))

        if not fragments:
            self._event("pipeline.generation_empty", run_id=ctx.run_id,
                        sections=plan.kinds())
            return ""

        page = assemble_page(plan, fragments, existing=present)
        root = self._repo_root()
        target = (root / plan.path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._event("pipeline.generation_path_rejected", run_id=ctx.run_id,
                        path=plan.path)
            return ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")
        ctx.extras["generation"] = {
            "path": plan.path,
            "sections": plan.kinds(),
            "written": sorted(fragments),
            "failed": failures,
            "edit": bool(existing_path),
            "reused": sorted(k for k in present if k not in fragments),
        }
        # The plan itself is kept, not just its section names.  The contract used
        # to re-derive it from the goal text with ``plan_generation(goal)``, which
        # is a second source of truth: on an edit the plan is the union of the
        # page's existing sections and the goal's, and re-deriving it from the
        # goal alone produced a *different* plan than the one the page was built
        # from — so the contract demanded sections the assembler never planned.
        ctx.extras["generation_plan"] = {
            "path": plan.path,
            "sections": plan.kinds(),
            "palette": list(plan.palette),
            "title": plan.title,
        }
        self._event("pipeline.generation_assembled", run_id=ctx.run_id,
                    path=plan.path, bytes=len(page),
                    written=sorted(fragments), failed=failures)
        # ``_episode_diff`` reads the files the run wrote from this list; the
        # assembled page is one of them, so the rest of the pipeline sees it.
        ctx.extras["episode_written_files"] = [plan.path]
        # The harness built this file, so it owns it: a model diff for the same
        # path is dropped rather than applied over the assembly (see ``_node_act``).
        ctx.extras["harness_owned_paths"] = [plan.path]
        # The *content* is kept too, not just the path.  Dropping the model's
        # hunk from the diff is not enough: the fix loop applies the diff it was
        # given to the worktree, and the model's own diff for the same path was
        # applied before the drop — measured live, a 5452-byte assembled page was
        # on disk, the model returned a 1139-byte hero-only page, the fix loop
        # applied it, and the run settled ``accepted: false`` with the contract
        # reporting "section 'features' missing".  The harness had built the page
        # and then let the model overwrite it.  ``_node_act`` restores this.
        ctx.extras["harness_owned_content"] = {plan.path: page}
        return self._episode_diff(ctx)

    def _subagent_body(self, spec: SubAgentSpec, ctx: RunContext) -> Mapping[str, Any]:
        """Default sub-agent: the same task pipeline, scoped to its private copy.

        Nesting the pipeline is what makes a sub-agent real rather than a single
        model call — it plans, slices, authors and validates its own change inside
        its own worktree or copy fallback.
        """
        worktree = Path(str(ctx.extras.get("worktree") or self.config.repo_root)).resolve()
        if not worktree.is_dir():
            return {"ok": False, "error": "sub-agent workspace is unavailable"}
        child_config = replace(self.config, repo_root=str(worktree))
        child_firewall = self.firewall
        if self.firewall is not None:
            try:
                from gigaforge.firewall import ContextFirewall

                firewall_config = replace(self.firewall.config, repo_root=str(worktree))
                # Ownership is enforced, not requested.  ``focus_paths`` was a
                # hint for the slicer and nothing stopped a write: measured, a
                # child told to touch only ``a.html`` created ``index.html`` and
                # ``b.html`` as well.  ``apply_allow_paths`` is the same guard
                # that refuses a path outside the repo, so a write outside the
                # assignment is refused before it reaches the tree.
                if spec.focus_paths:
                    firewall_config = replace(
                        firewall_config,
                        apply_allow_paths=tuple(spec.focus_paths),
                    )
                child_firewall = ContextFirewall(self.firewall.store, config=firewall_config)
            except Exception as exc:  # noqa: BLE001 - fail this child, preserve parent isolation
                return {"ok": False, "error": f"sub-agent firewall unavailable: {exc}"}
        child_test_runner = self.test_runner
        if child_test_runner is not None and hasattr(child_test_runner, "root"):
            try:
                child_test_runner = type(child_test_runner)(worktree)
            except Exception as exc:  # noqa: BLE001 - a custom gate may not expose a cloneable type
                self._degraded("child_test_runner", exc, "subagents")
                child_test_runner = None
        child = TaskPipeline(
            config=child_config,
            client=self.client,
            store=self.store,
            firewall=child_firewall,
            # A child gets its OWN router.  Handing it ``self.router`` made the
            # child's ``run()`` call ``reset()`` on the parent's counters, so the
            # parent forgot what it had spent in ``plan`` the moment a fan-out
            # finished; and because ``BudgetTracker`` keeps its usage on the
            # instance, every child also got the parent's full phase allowance.
            # ``child()`` shares the client (and therefore the circuit breaker)
            # and owns the counters.
            router=self.router.child() if self.router is not None else None,
            cache=self.cache,
            metrics=self.metrics,
            checkpoints=self.checkpoints,
            clock=self.clock,
            indexer=None,
            test_runner=child_test_runner,
        )
        try:
            result = child.run(
                spec.goal,
                run_id=f"{ctx.run_id}:{spec.name}",
                files=spec.focus_paths,
                symbols=spec.symbols,
                budget_tokens=spec.budget_tokens,
                use_cache=spec.use_cache,
                dry_run=spec.dry_run,
                max_fix_iterations=spec.max_fix_iterations,
                cancellation=ctx.cancellation,
            )
        finally:
            child.close()
        return {
            "ok": result.accepted,
            "diff": result.final_diff,
            "summary": result.summary,
            "error": result.error,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for token in self._active_runs.values():
                token.cancel("pipeline is closing")
            if self._active_runs:
                self._idle_callbacks.append(self._close_owned_resources)
                return
        self._close_owned_resources()

    def _close_owned_resources(self) -> None:
        for owned, resource in (
            (self._owns_cache, self.cache),
            (self._owns_checkpoints, self.checkpoints),
            (self._owns_metrics, self.metrics),
        ):
            if owned:
                closer = getattr(resource, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # pragma: no cover - best-effort lifecycle
                        pass

    def __enter__(self) -> "TaskPipeline":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ── module-level helpers ─────────────────────────────────────────────────────
class _NullSlice:
    """An explicitly empty context slice.

    Used when no index is wired in or the slicer found nothing.  It renders to an
    empty string and exposes an empty symbol list, so its hash is deterministic —
    which keeps the cache key well-defined even against a cold index.
    """

    symbols: tuple[Any, ...] = ()

    def render(self) -> str:
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {"symbols": [], "empty": True}


def _allocate_subagent_budget(
    budget_tokens: int, tokens_used: int, child_count: int
) -> int:
    """Split a finite remaining task allowance evenly among active children.

    ``0`` preserves the existing convention that no explicit token cap is set.
    """
    budget_tokens = max(0, int(budget_tokens))
    child_count = max(0, int(child_count))
    if budget_tokens == 0 or child_count == 0:
        return 0
    remaining = max(0, budget_tokens - max(0, int(tokens_used)))
    return remaining // child_count


def _scoped_goal(goal: str, owns_paths: Sequence[str], all_paths: Sequence[str]) -> str:
    """The parent goal with every *other* path removed from the text.

    A child handed the full goal re-derives every file name in it.  Measured:
    a child told "only touch ``a.html``" still created ``index.html`` and
    ``b.html``, because ``_ensure_creations`` reads the paths out of the goal
    text and the goal text named all three.  Three children then rewrote each
    other's files and the run oscillated.

    Removing the other names is the deterministic half of the fix; the other
    half is ``apply_allow_paths`` on the child's firewall, which refuses the
    write even if the model invents the name anyway.
    """
    text = str(goal or "")
    mine = {str(p).replace("\\", "/") for p in owns_paths}
    for other in all_paths:
        rel = str(other).replace("\\", "/")
        if not rel or rel in mine:
            continue
        # Replace the path with a neutral token rather than deleting it: the
        # sentence around it still has to read as a goal.
        text = text.replace(rel, "(another file, not yours)")
        name = rel.rsplit("/", 1)[-1]
        if name and name != rel:
            text = text.replace(name, "(another file, not yours)")
    owned = ", ".join(f"`{p}`" for p in owns_paths)
    return (
        f"{text}\n\n"
        f"Your assignment covers ONLY {owned}. "
        "Do not create, edit or delete any other file: writes outside this set "
        "are refused by the guard and the work will be rejected."
    )


def _added_function_definitions(diff_text: str) -> list[tuple[str, str]]:
    """Extract added top-level function declarations from a unified diff."""
    path = ""
    found: list[tuple[str, str]] = []
    for line in str(diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].replace("\\", "/")
        elif path and line.startswith("+") and not line.startswith("+++"):
            match = re.match(r"\+\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", line)
            if match:
                found.append((path, match.group(1)))
    return found


def _workspace_relative_path(raw: str, root: Path) -> str:
    """Map an absolute or relative path to this workspace, rejecting escapes."""
    value = str(raw or "").strip()
    if not value:
        return ""
    candidate = Path(value)
    if not candidate.is_absolute():
        if re.match(r"^[A-Za-z]:[\\/]", value):
            return ""
        candidate = Path(value.replace("\\", "/"))
    try:
        base = root.resolve()
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
        return resolved.relative_to(base).as_posix()
    except (OSError, ValueError):
        return ""


def _parse_plan(text: str) -> list[str]:
    """Extract numbered / bulleted steps from a plan answer."""
    if not text:
        return []
    steps: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        stripped = line
        for marker in (". ", ") "):
            index = line.find(marker)
            if 0 < index <= 3 and line[:index].isdigit():
                stripped = line[index + len(marker):].strip()
                break
        else:
            for prefix in ("- ", "* ", "\u2022 "):
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix):].strip()
                    break
        if stripped:
            steps.append(stripped)
    return steps[:24]


def _task_kind_for(owner: Any, ctx: RunContext) -> Any:
    """The goal's task kind, tolerating a caller that predates ``_task_kind``.

    Node functions are called directly by tests with a lightweight stand-in for
    the pipeline, and those stand-ins have no ``_task_kind``.  Falling back to a
    fresh classification keeps them working without weakening the product path,
    which uses the cached verdict.
    """
    fn = getattr(owner, "_task_kind", None)
    if callable(fn):
        return fn(ctx)
    from gigaforge.harness.task_kind import classify_task

    root_fn = getattr(owner, "_repo_root", None)
    root = root_fn() if callable(root_fn) else None
    return classify_task(_original_goal(ctx.state.goal), root)


def _citations_from(result: Any) -> list[str]:
    """The URLs a search-backed answer actually saw.

    Read from the episode's own observations, not from the reply text: a model
    can write a plausible URL it never visited, and the acceptance gate must be
    able to tell "cited what it read" from "invented a source".  Only URLs that
    appear in a ``web_search`` observation count.
    """
    urls: list[str] = []
    for step in getattr(result, "steps", None) or []:
        if str(step.get("action") or "") != "web_search":
            continue
        observation = str(step.get("observation") or "")
        for match in re.finditer(r"https?://[^\s<>\"')\]]+", observation):
            url = match.group(0).rstrip(".,;:")
            if url not in urls:
                urls.append(url)
    return urls


def _broken_anchors(html: str) -> list[str]:
    """In-page links whose target id does not exist.

    **Measured live** on the cyberpunk landing page, agent mode: the footer
    linked ``#about``, ``#contact`` and ``#terms`` while the page defined only
    ``id="top"``.  Clicking any of them does nothing — a defect a browser shows
    and no tag-level check sees.  The run was accepted, because ``run`` does not
    attach a verifier at all and the structural audit did not look at anchors.

    ``href="#"`` is not a broken anchor: it is the conventional "top of page"
    link and has no target by design.  A link to another document
    (``page.html#x``) is out of scope here — this checks one file.
    """
    import re as _re

    ids = set(_re.findall(r"""\bid\s*=\s*["']([^"']+)["']""", html, _re.I))
    broken: list[str] = []
    for href in _re.findall(r"""\bhref\s*=\s*["']#([^"']+)["']""", html, _re.I):
        target = href.strip()
        if target and target not in ids and target not in broken:
            broken.append(target)
    if not broken:
        return []
    return ["internal links target missing ids: " + ", ".join(f"#{b}" for b in broken[:8])]


def _drop_hunks_for(diff: str, paths: set[str]) -> str:
    """Remove every file block that touches one of *paths*.

    Used for files the harness wrote itself: the assembled page is authoritative,
    and a model diff for the same path would edit a file the harness just built.

    **Both header forms are recognised**, because the harness produces both: the
    canonical ``diff --git a/X b/X`` and the bare ``--- a/X`` / ``+++ b/X`` pair
    that ``firewall.validator._add_hinted_file_headers`` emits.  The first
    version of this function matched only ``diff --git``, so the normalised diff
    the fix loop actually returns was never dropped: ``final_diff`` described a
    model edit that was not in the tree, and the report said "restored" while
    the diff still carried the edit.  See :mod:`gigaforge.harness.diff_parse`.
    """
    if not diff or not paths:
        return diff
    wanted = {normalise_path(p) for p in paths}
    return "".join(
        block.text for block in split_diff(diff) if block.path not in wanted
    )


def _keep_hunks_for(diff: str, paths: set[str]) -> str:
    """The complement of :func:`_drop_hunks_for`: only the blocks for *paths*."""
    if not diff or not paths:
        return ""
    wanted = {normalise_path(p) for p in paths}
    return "".join(
        block.text for block in split_diff(diff) if block.path in wanted
    )


def _merge_repaired_diff(original: str, repaired: str) -> str:
    """Replace the per-file blocks of *original* with the ones in *repaired*.

    The repair node recomputes a diff for the files it rewrote.  Those files
    must come from the recomputed diff — it is the only one that matches the
    tree — while every other file keeps the block the run already produced.

    A file is identified by its path, parsed from either header form.  When the
    original has no block for a file (the repair created it), the recomputed
    block is appended.
    """
    if not repaired:
        return original
    if not original:
        return repaired

    merged: dict[str, str] = {}
    for block in split_diff(original):
        merged[block.path] = block.text
    for block in split_diff(repaired):
        merged[block.path] = block.text
    return "".join(merged.values())


def _original_goal(goal: str) -> str:
    """The user's goal, with the goal-loop's own feedback block removed.

    ``GoalRunner`` appends a "LONG-LIVED GOAL MODE — CONTINUE THE EXISTING TASK"
    block to every cycle after the first.  Planning the artifact from that text
    made the section plan drift between cycles: measured live, cycle 1 planned
    ``hero, features, footer`` and cycles 2-3 planned ``hero, features, how,
    footer`` — the feedback prose mentioned "workflow", which the catalogue reads
    as a "how it works" section.  The plan must come from what the user asked
    for, not from the harness's own repair notes.
    """
    marker = "\n\nLONG-LIVED GOAL MODE"
    head, sep, _tail = (goal or "").partition(marker)
    return head.strip() if sep else (goal or "").strip()


def _diff_path_hint(ctx: RunContext) -> str:
    """Return a path only when the harness can identify one unambiguously."""
    for key in ("harness_owned_paths", "focus_paths"):
        paths = list(dict.fromkeys(
            str(path).strip() for path in (ctx.extras.get(key) or ()) if str(path).strip()
        ))
        if len(paths) == 1:
            return paths[0]

    task_kind = ctx.extras.get("task_kind") or {}
    if isinstance(task_kind, Mapping):
        paths = [
            str(path).strip()
            for key in ("artifact_paths", "edit_paths", "code_paths")
            for path in (task_kind.get(key) or ())
            if str(path).strip()
        ]
        paths = list(dict.fromkeys(paths))
        if len(paths) == 1:
            return paths[0]
    return ""


def _merge_focused_creation(
    result: FixLoopResult,
    diff: str,
    validation: ValidateResult,
    requested_paths: Sequence[str],
) -> dict[str, Any]:
    previous_accepted = result.accepted
    previous_diff = result.diff
    previous_files = list(result.changed_files)
    created_files = list(validation.changed_files)
    all_files = list(dict.fromkeys([*previous_files, *created_files]))
    requested = set(requested_paths)

    if validation.accepted:
        missing = sorted(requested.difference(all_files))
        result.diff = "\n".join(
            item for item in (previous_diff if previous_accepted else "", diff) if item
        )
        result.changed_files = all_files
        result.accepted = (previous_accepted or bool(created_files)) and not missing
        result.reason = "focused creation accepted" if result.accepted else "focused creation was partial"
        result.iterations.append(FixIteration(
            index=result.proposals + 1,
            kind="repair",
            diff=diff,
            accepted=True,
            reason=validation.reason or "accepted",
            validate=validation,
        ))
        return {"accepted": result.accepted, "files": created_files, "missing": missing}

    if not previous_accepted:
        result.diff = diff
        result.iterations.append(FixIteration(
            index=result.proposals + 1,
            kind="repair",
            diff=diff,
            accepted=False,
            reason=validation.reason or "rejected",
            validate=validation,
        ))
    result.accepted = False
    result.reason = validation.reason or "focused creation was rejected"
    missing = sorted(requested.difference(all_files)) or ["focused file creation was rejected"]
    return {"accepted": False, "files": created_files, "missing": missing}


def _fallback_summary(accepted: bool, diff: str) -> str:
    if not diff:
        return "No change was produced."
    verb = "Proposed a change" if accepted else "Produced a change that could not be validated"
    return f"{verb} ({len(diff)} chars of unified diff)."


#: ``Pipeline`` is the name older code imports.  It used to be a second, async
#: implementation (``core/agent_pipeline.py``) that referenced symbols it never
#: imported, so every call raised ``NameError``.  Two pipelines were also two
#: behaviours to keep in sync; there is one now, and this alias says so.
Pipeline = TaskPipeline

__all__ = [
    "TaskPipeline",
    "Pipeline",
    "PipelineResult",
    "RetryableFailure",
    "PIPELINE_NAME",
    "PIPELINE_VERSION",
    "NODE_ORDER",
]

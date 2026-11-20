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
from .fixloop import FixLoop, FixLoopResult
from .metrics import MetricsCollector
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
    #: Files whose creation hunk was rewritten into a modification because the
    #: file already existed (see ``harness.diff_repair``).
    diff_repaired: list[str] = field(default_factory=list)
    #: Requirements from the goal that the produced artifact does not satisfy.
    #: Non-empty means the run was rejected by the contract gate, not by the
    #: model's own judgement — the distinction a caller needs to tell "the model
    #: gave up" from "the harness refused to call this done".
    contract_unmet: list[str] = field(default_factory=list)

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
            "summary": self.summary,
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
        }

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return self.to_dict()

    def dict(self) -> dict[str, Any]:
        return self.to_dict()


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
                provides=("final_diff", "accepted", "changed_files"),
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
        cancellation: Optional[CancellationToken] = None,
    ) -> PipelineResult:
        """Run the task DAG end to end.

        Pass a :class:`CancellationToken` to let a caller request a cooperative
        stop; ``cancel(run_id)`` is the runtime-facing convenience API.
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
                started_ms=started,
            )
        finally:
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
        cancellation: CancellationToken,
        started_ms: float,
    ) -> PipelineResult:
        state = TaskState(run_id=run_id, goal=goal)
        # ``router.reset()`` existed and was never called, so the per-phase
        # call/token counters accumulated across every run in the process.  A
        # goal run invokes ``pipeline.run()`` once per cycle, so from cycle 5
        # onward the planner was refused with "phase 'plan' used its 4 calls"
        # and spent zero tokens: measured live, 4 of 8 cycles were empty
        # (`tokens 0`) and the goal gave up on work it had already done.
        # A run is the unit a phase budget is specified for.
        self.router.reset()
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
            except Exception:  # noqa: BLE001
                pass
        self._event("pipeline.intake", run_id=ctx.run_id, goal_chars=len(goal))
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
        except Exception:  # noqa: BLE001 - the contract is advisory
            tool_contract = ""
        ctx.state.prefix = build_system_prefix(
            repo_map=repo_map, tool_contract=tool_contract
        )
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
            except Exception:  # noqa: BLE001 - an unbuilt index is not fatal
                pass
        if paths:
            return paths[:limit]
        try:
            files = list(self.indexer.discover()) if self.indexer is not None else []
        except Exception:  # noqa: BLE001
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
        runner.ctx = ReActContext(
            repo_root=root,
            worktree=root,
            index=self.index,
            sandbox=sandbox,
        )
        return runner

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
        if callable(harness_plan):
            outcome, ledger = harness_plan(
                ctx.state.goal,
                slice_text,
                ask=lambda phase, prompt, temperature=None: self._ask(
                    phase, prompt, ctx, temperature=temperature
                ),
                parse=_parse_plan,
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
            getattr(outcome, "structured", None) is None
            and getattr(outcome, "steps", None)
            and outcome.strategy != "linear"
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
        except Exception:  # noqa: BLE001 - extra context must never fail a run
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
        except Exception:  # noqa: BLE001
            files = []
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

    def _subagent_diff(self, ctx: RunContext) -> str:
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
            if kind.is_artifact:
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
        except Exception as exc:  # noqa: BLE001 - fall through to the code path
            self._event("pipeline.generation_plan_error", run_id=ctx.run_id,
                        error=str(exc)[:300])
        try:
            from gigaforge.firewall.slicer_keywords import Extractor

            config = getattr(self.firewall, "config", None)
            paths = Extractor(config).extract(ctx.state.goal).paths
        except Exception:  # noqa: BLE001
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

        specs = [
            SubAgentSpec(
                name=f"work-{index}",
                # Only this item's work.  ``_scoped_goal`` removes the other
                # paths from the text, so the child cannot re-derive them.
                goal=_scoped_goal(ctx.state.goal, item.owns_paths, named),
                focus_paths=item.owns_paths,
                budget_tokens=int(ctx.extras.get("budget_tokens") or 0),
                use_cache=bool(ctx.extras.get("use_cache", True)),
                dry_run=bool(ctx.extras.get("dry_run", False)),
                max_fix_iterations=ctx.extras.get("max_fix_iterations"),
            )
            for index, item in enumerate(items)
        ]
        try:
            results = self.fan_out(specs, parent_ctx=ctx, parallel=True)
        except Exception as exc:  # noqa: BLE001 - fall back to one agent
            self._event("pipeline.subagents_error", run_id=ctx.run_id,
                        error=str(exc)[:300])
            return ""

        # Acceptance gate.  A rejected child is reported with its reason and
        # excluded from the merge; it is never silently dropped and never
        # silently merged.
        entries: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        accepted_paths: list[str] = []
        for item, result in zip(items, results):
            verdict = check_acceptance(
                result, item, accepted_paths=accepted_paths, config=config
            )
            if verdict.accepted:
                accepted_paths.extend(verdict.paths)
                entries.append({
                    "name": item.id,
                    "diff": str(getattr(result, "diff", "") or ""),
                    "owns_paths": item.owns_paths,
                })
            else:
                rejected.append({
                    "name": item.id,
                    "reasons": verdict.reasons,
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

    def _node_act(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        if inputs.get("cache_hit"):
            return {
                "proposal": ProposeResult(diff=str(inputs.get("cached_diff", "")),
                                          phase="code", cached=True, model="cache"),
                "proposal_source": "cache",
            }
        if self.router is None:
            raise PipelineError("no model client is wired into the pipeline")

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
        prefetched = self._subagent_diff(ctx)
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
        if episode_diff:
            prefetched = "\n".join(p for p in (prefetched, episode_diff) if p)

        # A page goal is built section by section in every mode, not only under
        # ``--subagents``.  Measured live: ``run`` on the site goal produced a
        # 2549-byte skeleton — "Feature 1 / Feature 2 / Feature 3" — because one
        # model call was asked to write a whole page in one shot.  The harness
        # owns the plan and the skeleton; that is true whether or not the user
        # asked for sub-agents.
        if not prefetched:
            try:
                from gigaforge.harness.task_kind import classify_task

                kind = classify_task(_original_goal(ctx.state.goal), self._repo_root())
                ctx.extras["task_kind"] = kind.to_dict()
                self._event("pipeline.task_kind", run_id=ctx.run_id,
                            task_kind=kind.kind, evidence=kind.evidence[:300])
                if kind.is_artifact:
                    prefetched = self._sectional_generation(ctx)
            except Exception as exc:  # noqa: BLE001 - fall back to the coder
                self._event("pipeline.generation_plan_error", run_id=ctx.run_id,
                            error=str(exc)[:300])

        def propose(phase: str, prompt_text: str, iteration: int, hint: str) -> ProposeResult:
            text = self._ask(phase, prompt_text, ctx)
            route = self.router.last_route if self.router else None
            diff = extract_diff(text)
            blocks = self._file_blocks(text)
            if blocks:
                from .fileblocks import files_to_diff

                diff = "\n".join(part for part in (diff, files_to_diff(blocks)) if part)
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
            path_hint=(sorted(ctx.extras.get("harness_owned_paths") or []) or [""])[0],
        )
        ctx.extras["fix_result"] = result
        result.diff = self._ensure_creations(ctx, result.diff, slice_text, ctx)
        result.diff = self._repair_creation_hunks(ctx, result.diff)
        # A file the harness wrote itself is authoritative.  ``_sectional_generation``
        # writes the assembled page to disk and hands its diff to the fix loop as
        # ``initial_diff``; if the model then also returns a diff for that path,
        # applying it would edit a file the harness just built — the page on disk
        # and the page in the diff would diverge, and the run would report the
        # model's version while the tree held the harness's.  Measured: the
        # assembled page had 1 <style> and the applied diff reintroduced a second.
        #
        # Only the *model's* contribution is dropped.  The first version dropped
        # every hunk for an owned path, which also removed the harness's own
        # ``initial_diff`` — measured live: the fix loop accepted the page, then
        # settle reported ``diff_chars: 0, files: 0`` and the goal loop rejected
        # three cycles running with the error "ok".  The harness's own hunks are
        # put back, so the diff describes the page that exists.
        owned = set(ctx.extras.get("harness_owned_paths") or [])
        if owned and result.diff:
            before = len(result.diff)
            result.diff = _drop_hunks_for(result.diff, owned)
            if prefetched:
                result.diff = _merge_repaired_diff(
                    result.diff, _keep_hunks_for(prefetched, owned)
                )
            if len(result.diff) != before:
                self._event("pipeline.harness_owned_hunks_dropped", run_id=ctx.run_id,
                            paths=sorted(owned))

        # Dropping the hunk from the diff does not undo what the fix loop already
        # applied to the tree.  The harness's own content is authoritative, so it
        # is written back — measured live, a 5452-byte assembled page was replaced
        # by the model's 1139-byte hero-only answer and the contract then reported
        # the sections the harness had built as missing.
        restored = self._restore_harness_owned(ctx)
        if restored:
            self._event("pipeline.harness_owned_restored", run_id=ctx.run_id,
                        paths=restored)

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
                          ctx_any: Any = None) -> str:
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
        except Exception:  # noqa: BLE001
            return diff
        root = self._repo_root()
        paths = [
            rel for raw in dict.fromkeys(paths)
            if (rel := _workspace_relative_path(raw, root))
        ]
        created_markers = [p for p in paths if f"+++ b/{p}" in diff or f"+++ {p}" in diff]
        missing = [
            p for p in paths
            if not (root / p).exists() and p not in created_markers
        ][:2]
        if not missing:
            return diff
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
            except Exception:  # noqa: BLE001 - a missing creation is reported, not fatal
                continue
            proposal = extract_diff(text)
            if proposal and ("/dev/null" in proposal or f"+++ b/{rel}" in proposal):
                extra_blocks.append(proposal.strip())
                ctx.state.add_note(f"create: {rel} (focused pass)")
        if not extra_blocks:
            return diff
        return (diff.rstrip() + "\n" + "\n".join(extra_blocks)).strip()

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
        except Exception:  # noqa: BLE001 - a parse failure just means "no blocks"
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
        except Exception:  # noqa: BLE001 - no test suite is not an error
            pass
        if findings.get("lint_errors") or findings.get("tests_failed"):
            ctx.extras["self_debug"] = findings
            self._event("pipeline.self_debug", run_id=ctx.run_id,
                        lint=len(findings.get("lint_errors") or []),
                        tests=bool(findings.get("tests_failed")))
        return findings

    def _node_validate(self, ctx: RunContext, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        proposal = inputs.get("proposal")
        diff = getattr(proposal, "diff", "") or ""
        if not diff:
            return {"validation": ValidateResult(accepted=False, reason="no diff to validate")}

        # No validator wired in means there is nothing that can reject a diff.
        # Reporting that as "rejected" would fail every run in a deployment that
        # simply has no lint/parse gate, so the verdict is accepted and the
        # absence is stated in the reason rather than hidden.
        if self.fixloop.validator is None:
            return {"validation": ValidateResult(
                accepted=True, applied=not bool(ctx.extras.get("dry_run")),
                changed_files=list(getattr(proposal, "changed_files", []) or []),
                reason="accepted without validation (no validator configured)",
            )}

        # When the fix loop already validated a diff, reuse its verdict instead of
        # validating a second time: a second pass could disagree with the loop's
        # last iteration and the run would then report two different truths.
        loop_result: Optional[FixLoopResult] = ctx.extras.get("fix_result")
        if loop_result is not None and loop_result.iterations:
            last = loop_result.iterations[-1]
            if last.validate is not None:
                return {"validation": last.validate}

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

    def _restore_harness_owned(self, ctx: RunContext) -> list[str]:
        """Write the harness's own files back over whatever the model applied.

        The fix loop applies the model's diff to the worktree.  For a path the
        harness built itself, that application is a regression: the assembled
        page is the plan's output, and the model's answer is a guess at the same
        file.  Measured live: 5452 bytes assembled, 1139 bytes after the model's
        diff, and the contract then reported the sections the harness had built
        as missing — the run failed on work it had already done correctly.

        Returns the paths that were actually rewritten, so the trace shows it.
        """
        content = ctx.extras.get("harness_owned_content") or {}
        if not content:
            return []
        root = self._repo_root()
        restored: list[str] = []
        for rel, text in content.items():
            target = Path(root) / rel
            try:
                current = target.read_text(encoding="utf-8") if target.is_file() else ""
            except OSError:
                current = ""
            if current == text:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            restored.append(rel)
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
        ctx.state.artifacts["final_diff"] = final
        ctx.state.artifacts["accepted"] = accepted
        ctx.state.artifacts["review"] = dict(verdict or {})
        if contract is not None:
            ctx.state.artifacts["contract"] = {
                "ok": contract.ok,
                "checked": contract.checked,
                "met": contract.met,
                "unmet": contract.unmet,
            }
        ctx.state.add_note(
            f"settle: accepted={accepted} diff_chars={len(final)} files={len(changed)} "
            f"review={verdict.get('verdict', 'ok') if isinstance(verdict, Mapping) else 'ok'}"
            + (f" contract_unmet={len(contract.unmet)}" if contract is not None else "")
        )
        self._event("pipeline.settle", run_id=ctx.run_id, accepted=accepted,
                    diff_chars=len(final), files=len(changed),
                    reviewed=bool(verdict), review_rejected=review_rejected)
        return {"final_diff": final, "accepted": accepted, "changed_files": changed}

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
            # A page goal is graded against the plan the harness itself made.
            # ``extract_requirements`` returns nothing for "Create a single-file
            # index.html", "Build a landing page" and "Make a website for my
            # bakery" — so the gate was absent exactly where it was needed, and
            # a page with no hero and four "Feature Card 1" divs passed.  The
            # plan always exists for a page goal, so the contract always exists.
            requirements = []
            generation = ctx.extras.get("generation") or {}
            if generation.get("sections"):
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
                        )
                    else:
                        plan = plan_generation(_original_goal(ctx.state.goal))
                    requirements = requirements_from_plan(plan)
                    self._event("pipeline.contract_from_plan", run_id=ctx.run_id,
                                sections=plan.kinds(), palette=list(plan.palette),
                                requirements=len(requirements))
                except Exception as exc:  # noqa: BLE001 - fall back to the goal text
                    self._event("pipeline.contract_plan_error", run_id=ctx.run_id,
                                error=str(exc)[:200])
            if not requirements:
                requirements = extract_requirements(ctx.state.goal)
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
        except Exception:  # pragma: no cover - import guard
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
        result = self.router.complete(phase, messages, ctx=ctx, temperature=temperature)
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
            diff_repaired=list(
                (ctx.extras.get("diff_repaired") or {}).get("repaired") or []
            ),
        )

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
            except Exception:  # noqa: BLE001 - a custom gate may not expose a cloneable type
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


def _drop_hunks_for(diff: str, paths: set[str]) -> str:
    """Remove every ``diff --git`` hunk that touches one of *paths*.

    Used for files the harness wrote itself: the assembled page is authoritative,
    and a model diff for the same path would edit a file the harness just built.
    A path matches when the hunk header names it as ``a/<path>`` or ``b/<path>``,
    so ``index.html`` does not match ``sub/index.html``.
    """
    if not diff or not paths:
        return diff
    wanted = {p.replace("\\", "/").lstrip("./") for p in paths}
    kept: list[str] = []
    current: list[str] = []
    drop = False

    def flush() -> None:
        if current and not drop:
            kept.extend(current)

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            current = [line]
            drop = False
            for token in line.split():
                if token.startswith(("a/", "b/")):
                    if token[2:].lstrip("./") in wanted:
                        drop = True
                        break
        else:
            current.append(line)
    flush()
    return "".join(kept)


def _keep_hunks_for(diff: str, paths: set[str]) -> str:
    """The complement of :func:`_drop_hunks_for`: only the hunks for *paths*."""
    if not diff or not paths:
        return ""
    wanted = {p.replace("\\", "/").lstrip("./") for p in paths}
    kept: list[str] = []
    current: list[str] = []
    keep = False

    def flush() -> None:
        if current and keep:
            kept.extend(current)

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            current = [line]
            keep = False
            for token in line.split():
                if token.startswith(("a/", "b/")):
                    if token[2:].lstrip("./") in wanted:
                        keep = True
                        break
        else:
            current.append(line)
    flush()
    return "".join(kept)


def _merge_repaired_diff(original: str, repaired: str) -> str:
    """Replace the per-file hunks of *original* with the ones in *repaired*.

    The repair node recomputes a diff for the files it rewrote.  Those files
    must come from the recomputed diff — it is the only one that matches the
    tree — while every other file keeps the hunk the run already produced.

    A file is identified by its ``diff --git a/X b/X`` header.  When the
    original has no hunk for a file (the repair created it), the recomputed
    hunk is appended.
    """
    if not repaired:
        return original
    if not original:
        return repaired

    def split(diff: str) -> dict[str, str]:
        chunks: dict[str, str] = {}
        current: list[str] = []
        key = ""
        for line in diff.splitlines(keepends=True):
            if line.startswith("diff --git "):
                if key:
                    chunks[key] = "".join(current)
                key = line.strip()
                current = [line]
            elif key:
                current.append(line)
        if key:
            chunks[key] = "".join(current)
        return chunks

    merged = split(original)
    merged.update(split(repaired))
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

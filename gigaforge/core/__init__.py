"""IGAFORGE core — orchestration layer (M4–M5).

The core layer turns the lower three layers into a working agent:

* **routing** — :class:`ModelRouter` picks a model per phase and enforces a
  token/call budget, so a runaway repair loop is a spend incident that cannot
  happen silently (:mod:`gigaforge.core.router`);
* **caching** — :class:`CacheManager` keys results by
  ``SHA-256(normalized_task + slice_hash + git_sha)`` so a cached diff can never
  be replayed onto code that has moved (:mod:`gigaforge.core.cache`);
* **execution** — :class:`DagExecutor` runs the task graph with per-node retries,
  timeouts, SQLite checkpoints, resume and a DLQ (:mod:`gigaforge.core.dag`);
* **repair** — :class:`FixLoop` drives propose→validate with a hard iteration
  bound and a circuit breaker in front of the model
  (:mod:`gigaforge.core.fixloop`);
* **delegation** — :class:`SubAgentRunner` fans work out into git worktrees with
  isolated context, aggregating strictly in spawn order
  (:mod:`gigaforge.core.agents`);
* **observability** — :class:`MetricsCollector` writes ``metrics_points`` and an
  append-only JSONL trace (:mod:`gigaforge.core.metrics`).

Typical use::

    from gigaforge.core import CoreConfig, TaskPipeline, GigaClientAdapter

    cfg = CoreConfig.from_env()
    with TaskPipeline(config=cfg, client=my_client, firewall=fw) as pipeline:
        result = pipeline.run("fix division by zero in div")
        print(result.accepted, result.changed_files)
"""
from __future__ import annotations

from typing import Any

from .agents import SubAgentHandle, SubAgentRunner, Worktree, WorktreeManager
from .breaker import BreakerRegistry, BreakerState, BreakerStats, CircuitBreaker
from .cache import CacheEntry, CacheManager, normalize_task, slice_hash, task_hash
from .cancellation import CancellationToken
from .config import (
    ENV_PREFIX,
    PHASES,
    BreakerConfig,
    CacheConfig,
    CoreConfig,
    FixLoopConfig,
    PhaseBudget,
    PhaseRoute,
    RetryConfig,
    SubAgentConfig,
    default_budgets,
    default_routes,
    override_budget,
)
from .contracts import (
    ChatClient,
    Clock,
    LLMRouter,
    NodeSpec,
    ProposeResult,
    RunContext,
    SubAgentResult,
    SubAgentSpec,
    SystemClock,
    ValidateResult,
)
from .cost import CostAccumulator, ModelPrice, PriceTable, cost_micros
from .dag import CheckpointStore, DagExecutor, DagResult, NodeRecord
from .db import Database, get_database, reset_registry
from .errors import (
    BreakerOpenError,
    CacheError,
    CacheMissError,
    CoreConfigError,
    CoreError,
    CycleError,
    DagError,
    DuplicateArtifactError,
    DuplicateNodeError,
    FixLoopExhausted,
    MissingInputError,
    NodeFailedError,
    NodeOutputError,
    NodeTimeoutError,
    RunCancelledError,
    NoModelForPhaseError,
    PhaseBudgetExceeded,
    PipelineError,
    RoutingError,
    SubAgentError,
    UnknownDependencyError,
    UnknownPhaseError,
    WorktreeError,
)
from .fixloop import FixIteration, FixLoop, FixLoopResult
from .goal import (
    GoalCycle,
    GoalResult,
    GoalRunner,
    GoalStore,
    VerificationCheck,
    VerificationReport,
    WorkspaceVerifier,
)
from .metrics import MetricsCollector, SqliteMetricsSink, TraceWriter, read_jsonl
from .metrics_compat import CompositeSink, InMemorySink, MetricsSink, NullSink, percentile
from .optimize import ContextOptimizer, TrimResult, dedupe_blocks, optimize_context
from .parallel import WaveResult, WaveScheduler, compute_levels
from .pipeline import (
    NODE_ORDER,
    PIPELINE_NAME,
    PIPELINE_VERSION,
    Pipeline,
    PipelineResult,
    TaskPipeline,
)
from .prompt import (
    CODE_INSTRUCTION,
    FIX_INSTRUCTION,
    PHASE_INSTRUCTIONS,
    PhasePrompt,
    build_phase_prompt,
    extract_diff,
    instruction_for,
)
from .router import ModelRouter, PhaseUsage, Route, RouterStats, estimate_tokens, usage_dict
from .state import (
    DEFAULT_PERSONA,
    DEFAULT_RULES,
    PlanStep,
    SystemPrefix,
    TaskState,
    build_system_prefix,
    normalize_whitespace,
)
from .strategies import (
    PLAN_STRATEGIES,
    REVIEW_STRATEGIES,
    ConstitutionalReview,
    LinearPlan,
    LLMReview,
    NoReview,
    PlanOutcome,
    PlanStrategy,
    ReviewOutcome,
    ReviewStrategy,
    TreeOfThoughtPlan,
    build_plan_strategy,
    build_review_strategy,
    score_steps,
)
from .sync_adapter import SyncChatClient, await_blocking, shutdown_shared_loop
from .telemetry import RunTelemetry, StepView, TelemetryBuilder, build_telemetry

__version__ = "0.5.0"


class GigaClientAdapter:
    """Make ``gigaforge.client.GigaClient`` satisfy :class:`ChatClient`.

    The old adapter forwarded ``complete()`` to ``GigaClient.complete`` — a method
    that does not exist — so every routed call raised ``AttributeError``.  The
    real bridge lives in :mod:`gigaforge.core.sync_adapter`; this name stays so
    existing imports keep working.
    """

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        return SyncChatClient(*args, **kwargs)


__all__ = [
    "__version__",
    # facade / pipeline
    "TaskPipeline",
    "Pipeline",
    "PipelineResult",
    "PIPELINE_NAME",
    "PIPELINE_VERSION",
    "NODE_ORDER",
    "GigaClientAdapter",
    # config
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
    # contracts
    "Clock",
    "SystemClock",
    "CancellationToken",
    "ChatClient",
    "LLMRouter",
    "NodeSpec",
    "RunContext",
    "ProposeResult",
    "ValidateResult",
    "SubAgentSpec",
    "SubAgentResult",
    # router
    "ModelRouter",
    "Route",
    "PhaseUsage",
    "RouterStats",
    "estimate_tokens",
    "usage_dict",
    # cache
    "CacheManager",
    "CacheEntry",
    "task_hash",
    "slice_hash",
    "normalize_task",
    # state / prompt
    "TaskState",
    "PlanStep",
    "SystemPrefix",
    "build_system_prefix",
    "normalize_whitespace",
    "DEFAULT_PERSONA",
    "DEFAULT_RULES",
    "PhasePrompt",
    "PHASE_INSTRUCTIONS",
    "PLAN_INSTRUCTION",
    "CODE_INSTRUCTION",
    "FIX_INSTRUCTION",
    "build_phase_prompt",
    "instruction_for",
    "extract_diff",
    # dag
    "DagExecutor",
    "DagResult",
    "NodeRecord",
    "CheckpointStore",
    # breaker
    "CircuitBreaker",
    "BreakerRegistry",
    "BreakerState",
    "BreakerStats",
    # fix loop
    "FixLoop",
    "FixIteration",
    "FixLoopResult",
    # long-lived execution goals
    "GoalCycle",
    "GoalResult",
    "GoalRunner",
    "GoalStore",
    "VerificationCheck",
    "VerificationReport",
    "WorkspaceVerifier",
    # agents
    "SubAgentRunner",
    "SubAgentHandle",
    "WorktreeManager",
    "Worktree",
    # metrics
    "MetricsCollector",
    "SqliteMetricsSink",
    "TraceWriter",
    "read_jsonl",
    "MetricsSink",
    "InMemorySink",
    "NullSink",
    "CompositeSink",
    "percentile",
    # V2: shared db / wave scheduling / cost / telemetry / optimisation
    "Database",
    "get_database",
    "reset_registry",
    "WaveScheduler",
    "WaveResult",
    "compute_levels",
    "ModelPrice",
    "PriceTable",
    "CostAccumulator",
    "cost_micros",
    "RunTelemetry",
    "StepView",
    "TelemetryBuilder",
    "build_telemetry",
    "ContextOptimizer",
    "TrimResult",
    "dedupe_blocks",
    "optimize_context",
    # errors
    "CoreError",
    "CoreConfigError",
    "RoutingError",
    "UnknownPhaseError",
    "NoModelForPhaseError",
    "PhaseBudgetExceeded",
    "DagError",
    "DuplicateNodeError",
    "DuplicateArtifactError",
    "CycleError",
    "UnknownDependencyError",
    "MissingInputError",
    "NodeOutputError",
    "NodeTimeoutError",
    "RunCancelledError",
    "NodeFailedError",
    "BreakerOpenError",
    "CacheError",
    "CacheMissError",
    "FixLoopExhausted",
    "SubAgentError",
    "WorktreeError",
    "PipelineError",
    # strategies (pluggable plan / review stages)
    "PlanStrategy",
    "ReviewStrategy",
    "PlanOutcome",
    "ReviewOutcome",
    "LinearPlan",
    "TreeOfThoughtPlan",
    "NoReview",
    "ConstitutionalReview",
    "LLMReview",
    "PLAN_STRATEGIES",
    "REVIEW_STRATEGIES",
    "build_plan_strategy",
    "build_review_strategy",
    "score_steps",
    # blocking bridge between the sync DAG and the async transports
    "SyncChatClient",
    "await_blocking",
    "shutdown_shared_loop",
]

# Late import to avoid a cycle: ``PLAN_INSTRUCTION`` is defined in prompt.py, which
# nothing else in this module needs before the re-exports above are built.
from .prompt import PLAN_INSTRUCTION  # noqa: E402,F401  (re-exported above)

__all__.append("PLAN_INSTRUCTION")

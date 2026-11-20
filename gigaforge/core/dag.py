"""DAG executor with checkpoints and a dead-letter queue (M4–M5).

The pipeline is a directed acyclic graph of :class:`~gigaforge.core.contracts.NodeSpec`
units.  This module provides the executor: validation, topological ordering,
per-node retries/timeouts, SQLite checkpoints, resume, and a DLQ for work that
could not be completed.

Two design decisions worth stating, because both are easy to get wrong:

**Checkpoints record the completed nodes, not the running one.**  A checkpoint
written *before* a node runs is a promise the executor cannot keep — the process
could die mid-node and the next run would skip work that never happened.  Only a
node whose outputs are in hand is written as ``succeeded``, so a resume either
re-runs a node (cheap, and nodes are idempotent by contract) or skips one that is
genuinely done.

**A timeout is a deadline, not a kill.** CPython cannot stop a thread inside a
blocking call. The executor signals the node's cancellation token, stops waiting,
and does not retry the timed-out node; cooperative code can stop at its next
boundary. The executor never claims an uncooperative worker has stopped running.
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

from .config import CoreConfig
from .contracts import Clock, NodeSpec, RunContext, SystemClock
from .errors import (
    CycleError,
    DagError,
    DuplicateArtifactError,
    DuplicateNodeError,
    MissingInputError,
    NodeOutputError,
    NodeFailedError,
    NodeTimeoutError,
    RunCancelledError,
    UnknownDependencyError,
)

CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  tenant         TEXT NOT NULL DEFAULT 'default',
  pipeline       TEXT NOT NULL,
  pipeline_version TEXT,
  status         TEXT NOT NULL,
  correlation_id TEXT,
  input          JSON,
  output         JSON,
  error          JSON,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  duration_ms    INTEGER,
  total_tokens   INTEGER DEFAULT 0,
  cost_micros    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status, started_at);

CREATE TABLE IF NOT EXISTS node_runs (
  node_run_id TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL,
  node        TEXT NOT NULL,
  attempt     INTEGER NOT NULL DEFAULT 1,
  status      TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER,
  tokens_in   INTEGER DEFAULT 0,
  tokens_out  INTEGER DEFAULT 0,
  error_code  TEXT,
  error_msg   TEXT,
  attrs       JSON
);
CREATE INDEX IF NOT EXISTS ix_node_runs_run ON node_runs(run_id);

CREATE TABLE IF NOT EXISTS node_checkpoints (
  run_id     TEXT NOT NULL,
  node       TEXT NOT NULL,
  status     TEXT NOT NULL,
  attempt    INTEGER NOT NULL DEFAULT 1,
  payload    JSON,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, node)
);

CREATE TABLE IF NOT EXISTS dlq (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  run_id   TEXT,
  node     TEXT,
  code     TEXT NOT NULL,
  message  TEXT NOT NULL,
  payload  JSON,
  resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_dlq_run ON dlq(run_id, resolved);
"""


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@dataclass
class NodeRecord:
    """Per-node outcome, persisted to ``node_runs``."""

    node: str
    status: str               # succeeded | failed | timeout | cancelled | skipped
    attempts: int = 1
    duration_ms: float = 0.0
    error_code: str = ""
    error_msg: str = ""
    outputs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "status": self.status,
            "attempts": self.attempts,
            "duration_ms": round(self.duration_ms, 3),
            "error_code": self.error_code,
            "error_msg": self.error_msg,
            "outputs": list(self.outputs),
        }


@dataclass
class DagResult:
    """The whole run's outcome."""

    run_id: str
    status: str               # succeeded | failed | cancelled
    artifacts: dict[str, Any] = field(default_factory=dict)
    records: list[NodeRecord] = field(default_factory=list)
    dlq: list[dict[str, Any]] = field(default_factory=list)
    resumed_from: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def node(self, name: str) -> Optional[NodeRecord]:
        for record in self.records:
            if record.node == name:
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "records": [r.to_dict() for r in self.records],
            "dlq": list(self.dlq),
            "resumed_from": list(self.resumed_from),
            "duration_ms": round(self.duration_ms, 3),
            "error": self.error,
            "artifact_keys": sorted(self.artifacts),
        }


class CheckpointStore:
    """SQLite persistence for run state, node checkpoints and the DLQ."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        connection: Optional[sqlite3.Connection] = None,
        clock: Any = None,
    ) -> None:
        self._clock = clock or time.time
        if connection is not None:
            self.conn = connection
            self._owns = False
        else:
            path = db_path or ":memory:"
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self._owns = True
        self.conn.executescript(CHECKPOINT_SCHEMA)
        self.conn.commit()
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ── runs ─────────────────────────────────────────────────────────────────
    def start_run(
        self, run_id: str, pipeline: str, *, tenant: str = "default",
        input_payload: Any = None, correlation_id: str = "",
        version: str = "",
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO runs (run_id, tenant, pipeline, pipeline_version, status,
                                  correlation_id, input, started_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = 'running', finished_at = NULL
                """,
                (run_id, tenant, pipeline, version, correlation_id,
                 json.dumps(input_payload, default=str) if input_payload is not None else None,
                 _iso(self._clock())),
            )
            self.conn.commit()

    def finish_run(
        self, run_id: str, status: str, *, output: Any = None, error: Any = None,
        duration_ms: float = 0.0, total_tokens: int = 0,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE runs SET status = ?, output = ?, error = ?, finished_at = ?,
                                duration_ms = ?, total_tokens = ?
                WHERE run_id = ?
                """,
                (status,
                 json.dumps(output, default=str) if output is not None else None,
                 json.dumps(error, default=str) if error is not None else None,
                 _iso(self._clock()), int(duration_ms), int(total_tokens), run_id),
            )
            self.conn.commit()

    def run_row(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    # ── node runs ────────────────────────────────────────────────────────────
    def record_node(self, run_id: str, record: NodeRecord, *, attrs: Any = None) -> None:
        node_run_id = f"{run_id}:{record.node}:{record.attempts}"
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO node_runs (node_run_id, run_id, node, attempt, status,
                                       started_at, finished_at, duration_ms,
                                       error_code, error_msg, attrs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_run_id) DO UPDATE SET
                    status = excluded.status, finished_at = excluded.finished_at,
                    duration_ms = excluded.duration_ms, error_code = excluded.error_code,
                    error_msg = excluded.error_msg, attrs = excluded.attrs
                """,
                (node_run_id, run_id, record.node, record.attempts, record.status,
                 _iso(self._clock()), _iso(self._clock()), int(record.duration_ms),
                 record.error_code, record.error_msg[:2000],
                 json.dumps(attrs, default=str) if attrs is not None else None),
            )
            self.conn.commit()

    def node_runs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM node_runs WHERE run_id = ? ORDER BY started_at, node", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── checkpoints ──────────────────────────────────────────────────────────
    def checkpoint(self, run_id: str, node: str, *, status: str, payload: Any = None,
                   attempt: int = 1) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO node_checkpoints (run_id, node, status, attempt, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, node) DO UPDATE SET
                    status = excluded.status, attempt = excluded.attempt,
                    payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (run_id, node, status, int(attempt),
                 json.dumps(payload, default=str) if payload is not None else None,
                 _iso(self._clock())),
            )
            self.conn.commit()

    def completed_nodes(self, run_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT node FROM node_checkpoints WHERE run_id = ? AND status = 'succeeded' "
            "ORDER BY updated_at",
            (run_id,),
        ).fetchall()
        return [r["node"] for r in rows]

    def checkpoint_payload(self, run_id: str, node: str) -> Optional[Any]:
        row = self.conn.execute(
            "SELECT payload FROM node_checkpoints WHERE run_id = ? AND node = ?",
            (run_id, node),
        ).fetchone()
        if row is None or row["payload"] is None:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM node_checkpoints WHERE run_id = ? ORDER BY updated_at", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── DLQ ──────────────────────────────────────────────────────────────────
    def push_dlq(self, run_id: str, node: str, code: str, message: str, payload: Any = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO dlq (ts, run_id, node, code, message, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (_iso(self._clock()), run_id, node, code, message[:4000],
                 json.dumps(payload, default=str) if payload is not None else None),
            )
            self.conn.commit()

    def dlq_entries(self, run_id: str = "", *, unresolved_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dlq WHERE 1=1"
        params: list[Any] = []
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        if unresolved_only:
            sql += " AND resolved = 0"
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def resolve_dlq(self, entry_id: int) -> bool:
        with self._lock:
            cursor = self.conn.execute("UPDATE dlq SET resolved = 1 WHERE id = ?", (entry_id,))
            self.conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        if self._owns:
            try:
                self.conn.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def __enter__(self) -> "CheckpointStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class DagExecutor:
    """Runs a validated DAG of nodes, with retries, timeouts and checkpoints."""

    def __init__(
        self,
        nodes: Sequence[NodeSpec],
        *,
        config: Optional[CoreConfig] = None,
        checkpoints: Optional[CheckpointStore] = None,
        metrics: Any = None,
        trace: Any = None,
        clock: Optional[Clock] = None,
        pipeline_name: str = "pipeline",
        sleeper: Optional[Callable[[float], None]] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.config = config or CoreConfig()
        self.nodes: dict[str, NodeSpec] = {}
        for spec in nodes:
            if spec.name in self.nodes:
                raise DuplicateNodeError(f"duplicate node {spec.name!r}")
            self.nodes[spec.name] = spec
        self.checkpoints = checkpoints or CheckpointStore(self.config.db_path)
        self.metrics = metrics
        self.trace = trace
        self.clock: Clock = clock or SystemClock()
        self.pipeline_name = pipeline_name
        self._sleep = sleeper or time.sleep
        self._rng = rng or random.Random(0)
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._node_pool: Optional[futures.ThreadPoolExecutor] = None
        self.order: list[str] = []
        self.validate()

    # ── validation ───────────────────────────────────────────────────────────
    def validate(self) -> list[str]:
        """Verify the graph and compute a topological order.

        Raises :class:`CycleError` / :class:`UnknownDependencyError`.  Doing this
        in the constructor means a malformed graph fails before any node runs,
        rather than halfway through a run that has already spent tokens.
        """
        providers: dict[str, str] = {}
        for spec in self.nodes.values():
            for artifact in spec.provides:
                previous = providers.get(artifact)
                if previous is not None:
                    raise DuplicateArtifactError(
                        f"artifact {artifact!r} is provided by both {previous!r} and {spec.name!r}",
                        detail={"artifact": artifact,
                                "providers": [previous, spec.name]},
                    )
                providers[artifact] = spec.name
            for dep in spec.requires:
                if self._producer(dep) is None:
                    raise UnknownDependencyError(
                        f"node {spec.name!r} requires {dep!r} but nothing provides it",
                        detail={"node": spec.name, "missing": dep},
                    )
        order = self._topological_order()
        self.order = order
        return order

    def _producer(self, artifact: str) -> Optional[str]:
        for spec in self.nodes.values():
            if artifact in spec.provides:
                return spec.name
        return None

    def _topological_order(self) -> list[str]:
        """Kahn's algorithm over artifact dependencies.

        Ties are broken by declared order so that a graph with no constraints
        between two nodes runs them in the order the author wrote them — the same
        input always produces the same execution, which makes traces comparable.
        """
        declared = list(self.nodes)
        index = {name: i for i, name in enumerate(declared)}
        deps: dict[str, set[str]] = {}
        for name, spec in self.nodes.items():
            needed: set[str] = set()
            for artifact in spec.requires:
                producer = self._producer(artifact)
                if producer and producer != name:
                    needed.add(producer)
            deps[name] = needed

        order: list[str] = []
        ready = sorted([n for n, d in deps.items() if not d], key=lambda n: index[n])
        remaining = {n: set(d) for n, d in deps.items()}
        while ready:
            node = ready.pop(0)
            order.append(node)
            for candidate, needed in remaining.items():
                if node in needed:
                    needed.discard(node)
                    if not needed and candidate not in order and candidate not in ready:
                        ready.append(candidate)
            ready.sort(key=lambda n: index[n])

        if len(order) != len(self.nodes):
            unresolved = [n for n in declared if n not in order]
            raise CycleError(
                "the graph is not acyclic",
                detail={"unresolved": unresolved},
            )
        return order

    # ── execution ────────────────────────────────────────────────────────────
    def run(
        self,
        initial: Optional[Mapping[str, Any]] = None,
        *,
        run_id: Optional[str] = None,
        ctx: Optional[RunContext] = None,
        resume: bool = False,
        parallel: bool = False,
    ) -> DagResult:
        """Execute one run with a bounded, reusable node worker pool."""
        if not self._run_lock.acquire(blocking=False):
            raise DagError("this DagExecutor already has an active run")
        pool: Optional[futures.ThreadPoolExecutor] = None
        try:
            workers = max(1, int(getattr(self.config, "max_concurrency", 4)))
            pool = futures.ThreadPoolExecutor(max_workers=workers,
                                              thread_name_prefix="dagnode")
            self._node_pool = pool
            return self._run_impl(initial, run_id=run_id, ctx=ctx,
                                  resume=resume, parallel=parallel)
        finally:
            self._node_pool = None
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            self._run_lock.release()

    def _run_impl(
        self,
        initial: Optional[Mapping[str, Any]] = None,
        *,
        run_id: Optional[str] = None,
        ctx: Optional[RunContext] = None,
        resume: bool = False,
        parallel: bool = False,
    ) -> DagResult:
        """Execute the DAG.

        ``resume=True`` skips nodes already checkpointed as succeeded for this
        ``run_id`` and restores their outputs, so a crashed run continues instead
        of repeating work it already paid for.
        """
        started = self.clock.now()
        run_id = run_id or (ctx.run_id if ctx else f"run-{int(self.clock.timestamp())}")
        artifacts: dict[str, Any] = dict(initial or {})
        records: list[NodeRecord] = []
        resumed: list[str] = []

        base = ctx or RunContext(run_id=run_id, clock=self.clock, metrics=self.metrics,
                                 trace=self.trace)
        base.run_id = run_id
        base.metrics = base.metrics or self.metrics
        base.trace = base.trace or self.trace

        # Record what the run was asked to do.  ``runs.input`` stayed NULL for every
        # run because nothing passed it, which left the audit table — and every
        # view that reads it — with an anonymous row: a status without a subject.
        self.checkpoints.start_run(
            run_id,
            self.pipeline_name,
            tenant=self.config.tenant,
            input_payload=initial,
        )
        self._emit("dag.start", run_id=run_id, order=list(self.order))

        done: set[str] = set()
        if resume:
            for name in self.checkpoints.completed_nodes(run_id):
                payload = self.checkpoints.checkpoint_payload(run_id, name)
                if isinstance(payload, Mapping):
                    artifacts.update(payload)
                done.add(name)
                resumed.append(name)
            if resumed:
                self._emit("dag.resume", run_id=run_id, nodes=resumed)

        failed = False
        error_text = ""

        try:
            for level in (self._levels() if parallel else [[name] for name in self.order]):
                if base.cancellation.cancelled:
                    failed = True
                    error_text = base.cancellation.reason or "run cancelled"
                    break
                runnable = [name for name in level if name not in done]
                for name in level:
                    if name not in runnable:
                        records.append(NodeRecord(node=name, status="skipped",
                                                  attempts=0, outputs=()))
                if not runnable:
                    continue

                # Parallel execution is opt-in at the node boundary. Independent
                # graph edges alone do not prove that a callable's hidden state,
                # external writes or run-scoped metadata are safe to race.
                if (len(runnable) == 1 or not parallel
                        or not all(self.nodes[name].parallel_safe for name in runnable)):
                    for name in runnable:
                        spec = self.nodes[name]
                        if not self._all_inputs_ready(spec, artifacts):
                            missing = [a for a in spec.requires if a not in artifacts]
                            if spec.critical:
                                raise MissingInputError(
                                    f"node {name!r} is missing inputs {missing}",
                                    detail={"node": name, "missing": missing},
                                )
                            record = NodeRecord(node=name, status="skipped",
                                                attempts=0, error_code="GF-CORE-DAG-024")
                            records.append(record)
                            self.checkpoints.record_node(run_id, record)
                            continue
                        record = self._run_node(spec, base, artifacts,
                                                missing_ok=not spec.critical)
                        records.append(record)
                        self._emit_node(name, record, run_id)
                        if not self._after_node(name, record, artifacts, run_id):
                            failed = True
                            error_text = f"node {name!r} failed: {record.error_msg}"
                            break
                    if failed:
                        break
                    continue

                # ── one wave, several independent nodes ──────────────────────
                # Every worker gets its own copy of the artifact map, so no node
                # can observe another's half-written output; the merge below is
                # ordered by declaration, not by completion.
                snapshot = dict(artifacts)
                extras_snapshot = dict(base.extras)
                records_by_name: dict[str, NodeRecord] = {}
                locals_by_name: dict[str, dict[str, Any]] = {}
                contexts_by_name: dict[str, RunContext] = {}
                configured_workers = min(
                    int(getattr(self.config, "wave_workers", 4)),
                    int(getattr(self.config, "max_concurrency", 4)),
                )
                with futures.ThreadPoolExecutor(
                    max_workers=max(1, min(len(runnable), configured_workers)),
                    thread_name_prefix="dagwave",
                ) as pool:
                    pending: dict[Any, str] = {}
                    for name in runnable:
                        spec = self.nodes[name]
                        if not self._all_inputs_ready(spec, snapshot):
                            records_by_name[name] = NodeRecord(
                                node=name, status="skipped", attempts=0,
                                error_code="GF-CORE-DAG-024")
                            continue
                        local = dict(snapshot)
                        locals_by_name[name] = local
                        worker_ctx = base.child(extras=dict(extras_snapshot))
                        contexts_by_name[name] = worker_ctx
                        pending[pool.submit(self._run_node, spec, worker_ctx, local,
                                            missing_ok=not spec.critical)] = name
                    for future in futures.as_completed(pending):
                        name = pending[future]
                        try:
                            records_by_name[name] = future.result()
                        except BaseException as exc:  # noqa: BLE001 - per node
                            records_by_name[name] = NodeRecord(
                                node=name, status="failed", attempts=1,
                                error_msg=f"{type(exc).__name__}: {exc}")
                for name in runnable:  # deterministic merge order
                    record = records_by_name.get(name) or NodeRecord(
                        node=name, status="failed", attempts=0)
                    if record.status == "succeeded":
                        worker_ctx = contexts_by_name.get(name)
                        if worker_ctx is not None:
                            # A parallel-safe node may publish new run metadata,
                            # but it cannot rewrite shared inputs. Merge only after
                            # the wave finishes, in declaration order.
                            changed = [
                                key for key, value in extras_snapshot.items()
                                if key not in worker_ctx.extras
                                or worker_ctx.extras[key] is not value
                            ]
                            if changed:
                                raise DagError(
                                    f"parallel-safe node {name!r} mutated shared extras",
                                    detail={"node": name, "keys": changed},
                                )
                            for key, value in worker_ctx.extras.items():
                                if key in extras_snapshot:
                                    continue
                                if key in base.extras:
                                    raise DagError(
                                        f"parallel-safe nodes wrote the same extra {key!r}",
                                        detail={"node": name, "key": key},
                                    )
                                base.extras[key] = value
                        local = locals_by_name.get(name, {})
                        for artifact in self.nodes[name].provides:
                            if artifact in local:
                                artifacts[artifact] = local[artifact]
                    records.append(record)
                    self._emit_node(name, record, run_id)
                wave_failed = False
                for name in runnable:
                    record = records_by_name.get(name)
                    if record is None or not self._after_node(name, record, artifacts,
                                                              run_id):
                        wave_failed = True
                        if not error_text:
                            error_text = (f"node {name!r} failed: "
                                          f"{getattr(record, 'error_msg', 'unknown')}")
                if wave_failed:
                    failed = True
                    break
        except DagError as exc:
            failed = True
            error_text = str(exc)
            if self.config.dlq_enabled:
                self.checkpoints.push_dlq(run_id, base.node or "dag", exc.code, exc.message,
                                          payload=exc.detail)
        finally:
            duration = (self.clock.now() - started) * 1000.0

        status = ("cancelled" if base.cancellation.cancelled else
                  "failed" if failed else "succeeded")
        dlq = self.checkpoints.dlq_entries(run_id)
        total_tokens = int(getattr(base.state, "total_tokens", lambda: 0)()
                           if base.state is not None else 0)
        self.checkpoints.finish_run(run_id, status, output=_artifact_summary(artifacts),
                                    error=error_text or None, duration_ms=duration,
                                    total_tokens=total_tokens)
        self._emit("dag.finish", run_id=run_id, status=status,
                   duration_ms=round(duration, 3), dlq=len(dlq))

        return DagResult(
            run_id=run_id,
            status=status,
            artifacts=artifacts,
            records=records,
            dlq=dlq,
            resumed_from=resumed,
            duration_ms=duration,
            error=error_text,
        )

    # ── node execution ───────────────────────────────────────────────────────
    def _levels(self) -> list[list[str]]:
        """Topological levels of the DAG, in declaration order inside a level.

        A node joins the earliest level after all of its producers, so two nodes in
        one level never depend on each other — the property that makes a wave safe
        to execute concurrently.  The last level collects whatever the first pass
        could not place (a cycle): it still runs, in order, instead of vanishing.
        """
        produced_at: dict[str, int] = {}
        levels: list[list[str]] = []
        placed: set[str] = set()
        remaining = list(self.order)
        while remaining:
            level: list[str] = []
            rest: list[str] = []
            for name in remaining:
                spec = self.nodes[name]
                ready = all(
                    artifact in produced_at for artifact in spec.requires
                    if artifact not in {"task"}
                )
                (level if ready else rest).append(name)
            if not level:  # cycle or unsatisfiable input: keep the original order
                levels.append(rest)
                break
            for name in level:
                placed.add(name)
                for artifact in self.nodes[name].provides:
                    produced_at.setdefault(artifact, len(levels))
            levels.append(level)
            remaining = rest
        return levels

    def _emit_node(self, name: str, record: NodeRecord, run_id: str) -> None:
        """Persist one node record and stream its outcome."""
        spec = self.nodes[name]
        try:
            self.checkpoints.record_node(
                run_id, record,
                attrs={"requires": list(spec.requires), "provides": list(spec.provides)})
        except Exception:  # noqa: BLE001 - telemetry must not fail a run
            pass
        if record.status == "cancelled":
            self._emit("dag.node_cancelled", run_id=run_id, node=name,
                       attempts=record.attempts)
        elif record.status != "succeeded":
            self._emit("dag.node_failed", run_id=run_id, node=name,
                       code=record.error_code or "GF-CORE-DAG-026",
                       attempts=record.attempts)

    def _after_node(self, name: str, record: NodeRecord, artifacts: Mapping[str, Any],
                    run_id: str) -> bool:
        """Checkpoint a node and decide whether the run may continue."""
        if record.status == "cancelled":
            self.checkpoints.checkpoint(run_id, name, status="cancelled", payload=None,
                                        attempt=record.attempts)
            return False
        if record.status == "succeeded":
            provided = {a: artifacts.get(a) for a in self.nodes[name].provides}
            self.checkpoints.checkpoint(run_id, name, status="succeeded",
                                        payload=provided, attempt=record.attempts)
            return True
        code = record.error_code or "GF-CORE-DAG-026"
        self.checkpoints.checkpoint(run_id, name, status="failed", payload=None,
                                    attempt=record.attempts)
        if self.config.dlq_enabled:
            self.checkpoints.push_dlq(
                run_id, name, code, record.error_msg,
                payload={"requires": list(self.nodes[name].requires),
                         "non_critical": not self.nodes[name].critical},
            )
        return not self.nodes[name].critical

    @staticmethod
    def _all_inputs_ready(spec: NodeSpec, artifacts: Mapping[str, Any]) -> bool:
        if not spec.requires:
            return True
        return all(a in artifacts and artifacts[a] is not None for a in spec.requires)

    def _run_node(
        self,
        spec: NodeSpec,
        base: RunContext,
        artifacts: dict[str, Any],
        *,
        missing_ok: bool = False,
    ) -> NodeRecord:
        attempts = max(1, int(spec.retries) + 1)
        timeout = spec.timeout if spec.timeout is not None else self.config.node_timeout
        last_error: Optional[BaseException] = None
        started = self.clock.now()

        for attempt in range(1, attempts + 1):
            if base.cancellation.cancelled:
                last_error = RunCancelledError(
                    base.cancellation.reason or "run cancelled",
                    detail={"node": spec.name, "attempt": attempt},
                )
                break
            effective_timeout = timeout
            base_remaining = base.remaining()
            if base_remaining is not None:
                if base_remaining <= 0:
                    last_error = NodeTimeoutError(
                        f"run deadline expired before node {spec.name!r} started",
                        detail={"node": spec.name, "attempt": attempt},
                    )
                    break
                effective_timeout = (
                    base_remaining if not effective_timeout or effective_timeout <= 0
                    else min(effective_timeout, base_remaining)
                )
            node_token = base.cancellation.child()
            deadline = base.deadline
            if effective_timeout and effective_timeout > 0:
                node_deadline = base.clock.now() + effective_timeout
                deadline = min(deadline, node_deadline) if deadline is not None else node_deadline
            node_ctx = base.child(phase=spec.name, node=spec.name, attempt=attempt,
                                  artifacts=dict(artifacts), extras=base.extras,
                                  cancellation=node_token,
                                  deadline=deadline)
            inputs = MappingProxyType({a: artifacts.get(a) for a in spec.requires})
            self._emit("dag.node_start", run_id=base.run_id, node=spec.name, attempt=attempt)
            try:
                outputs = self._call_with_timeout(spec, node_ctx, inputs, effective_timeout)
            except BaseException as exc:  # noqa: BLE001 - classified below
                last_error = exc
                retryable = (
                    spec.idempotent
                    and not isinstance(exc, (NodeTimeoutError, RunCancelledError))
                    and bool(getattr(exc, "retryable", True))
                )
                event = "dag.node_cancelled" if isinstance(exc, RunCancelledError) else "dag.node_error"
                payload = {"run_id": base.run_id, "node": spec.name,
                           "attempt": attempt, "code": getattr(exc, "code", "ERR"),
                           "error": str(exc)[:300], "retryable": retryable}
                self._emit(event, **payload)
                if attempt < attempts and retryable:
                    delay = self._backoff(attempt)
                    if self._sleep is time.sleep:
                        if node_token.wait(delay):
                            last_error = RunCancelledError(
                                node_token.reason or "run cancelled during retry backoff",
                                detail={"node": spec.name, "attempt": attempt},
                            )
                            break
                    else:
                        self._sleep(delay)
                        if node_token.cancelled:
                            last_error = RunCancelledError(
                                node_token.reason or "run cancelled during retry backoff",
                                detail={"node": spec.name, "attempt": attempt},
                            )
                            break
                    continue
                break

            if not isinstance(outputs, Mapping):
                last_error = DagError(
                    f"node {spec.name!r} returned {type(outputs).__name__}, expected a mapping"
                )
                break
            expected = set(spec.provides)
            actual = set(outputs)
            if actual != expected:
                last_error = NodeOutputError(
                    f"node {spec.name!r} returned artifacts outside its contract",
                    detail={"node": spec.name,
                            "missing": sorted(map(str, expected - actual)),
                            "undeclared": sorted(map(str, actual - expected))},
                )
                break
            with self._lock:
                artifacts.update(outputs)
            return NodeRecord(
                node=spec.name, status="succeeded", attempts=attempt,
                duration_ms=(self.clock.now() - started) * 1000.0,
                outputs=tuple(spec.provides),
            )

        error = last_error or DagError(f"node {spec.name!r} failed with no error")
        status = (
            "cancelled" if isinstance(error, RunCancelledError)
            else "timeout" if isinstance(error, NodeTimeoutError)
            else "failed"
        )
        return NodeRecord(
            node=spec.name, status=status, attempts=attempt,
            duration_ms=(self.clock.now() - started) * 1000.0,
            error_code=getattr(error, "code", "GF-CORE-DAG-026"),
            error_msg=str(error),
        )

    def _call_with_timeout(
        self, spec: NodeSpec, ctx: RunContext, inputs: Mapping[str, Any], timeout: float
    ) -> Any:
        # A single bounded pool is reused for every node in this DAG run.
        # Worker threads cannot be force-killed; polling reacts promptly and
        # signals cooperative cancellation before returning on timeout.
        pool = self._node_pool
        if pool is None:
            raise DagError("node execution requires an active DagExecutor.run()")

        def invoke() -> Any:
            ctx.raise_if_cancelled()
            result = spec.fn(ctx, inputs)
            ctx.raise_if_cancelled()
            return result

        future = pool.submit(invoke)
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        try:
            while True:
                if ctx.cancellation.cancelled:
                    future.cancel()
                    raise RunCancelledError(
                        ctx.cancellation.reason or "node cancelled",
                        detail={"node": spec.name, "attempt": ctx.attempt},
                    )
                wait_for = 0.05
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        ctx.cancellation.cancel(
                            f"node {spec.name!r} exceeded its {timeout:.3f}s budget"
                        )
                        raise NodeTimeoutError(
                            f"node {spec.name!r} exceeded its {timeout:.3f}s budget",
                            detail={"node": spec.name, "timeout": timeout,
                                    "attempt": ctx.attempt},
                        )
                    wait_for = min(wait_for, remaining)
                done, _ = futures.wait((future,), timeout=wait_for)
                if future in done:
                    ctx.raise_if_cancelled()
                    return future.result()
        finally:
            if not future.done():
                future.cancel()

    def _backoff(self, attempt: int) -> float:
        cfg = self.config.retry
        raw = cfg.backoff_base * (2 ** max(0, attempt - 1))
        capped = min(cfg.backoff_max, raw)
        if cfg.jitter and capped > 0:
            return float(capped * (0.5 + self._rng.random() * 0.5))
        return float(capped)

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.metrics is not None:
            labels = {"pipeline": self.pipeline_name}
            node = payload.get("node")
            if node:
                labels["node"] = node
            # Scope the point to its run so a per-run view can filter it; a global
            # counter is only useful for lifetime totals.
            self.metrics.incr(
                f"core.{kind}", 1, labels,
                run_id=str(payload.get("run_id") or ""),
            )
        if self.trace is not None:
            self.trace.event(kind, pipeline=self.pipeline_name, **payload)

    # ── introspection ────────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline_name,
            "order": list(self.order),
            "nodes": [self.nodes[n].to_dict() for n in self.order],
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"DagExecutor(pipeline={self.pipeline_name!r}, nodes={len(self.nodes)})"


def _artifact_summary(artifacts: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in artifacts.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > 2000:
                out[key] = value[:2000] + "...[truncated]"
            else:
                out[key] = value
        elif hasattr(value, "to_dict"):
            try:
                out[key] = value.to_dict()
            except Exception:  # pragma: no cover - defensive
                out[key] = f"<{type(value).__name__}>"
        elif isinstance(value, (list, tuple)):
            out[key] = f"<{type(value).__name__}[{len(value)}]>"
        elif isinstance(value, Mapping):
            out[key] = f"<dict[{len(value)}]>"
        else:
            out[key] = f"<{type(value).__name__}>"
    return out


__all__ = [
    "DagExecutor",
    "DagResult",
    "NodeRecord",
    "CheckpointStore",
    "CHECKPOINT_SCHEMA",
]

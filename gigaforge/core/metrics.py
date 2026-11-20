"""Metrics collection and JSONL tracing (M4–M5).

Two complementary recorders:

* :class:`MetricsCollector` — counters, gauges and observations, fanned out to
  ``metrics_points`` in SQLite and to any extra sink (including the client
  layer's ``MetricsSink``).  This is what a dashboard reads.
* :class:`TraceWriter` — an append-only JSONL stream of *events*.  Metrics tell
  you *that* something happened; the trace tells you *what*, in order, per run.

The trace is deliberately JSONL rather than a database table: it is written on
the hot path, it must never block a task, and ``grep``/``jq`` over a JSONL file
is the fastest way to debug a single run.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Mapping, Optional, Sequence

from gigaforge.config import Settings, get_settings
from .config import CoreConfig
from .metrics_compat import CompositeSink, InMemorySink, NullSink, percentile  # noqa: F401

_RECORD_KEYS: tuple[str, ...] = (
    "ts",
    "task_id",
    "phase",
    "model",
    "tokens_in",
    "tokens_out",
    "estimated",
    "latency_ms",
    "retries",
    "ok",
    "cache_hit",
)


# ── SQLite side ──────────────────────────────────────────────────────────────
METRICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics_points (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     TEXT NOT NULL,
  metric TEXT NOT NULL,
  value  REAL NOT NULL,
  labels JSON,
  run_id TEXT,
  node   TEXT
);
CREATE INDEX IF NOT EXISTS ix_metrics_metric_ts ON metrics_points(metric, ts);

CREATE TABLE IF NOT EXISTS llm_calls (
    ts          REAL,
    task_id     TEXT,
    phase       TEXT,
    model       TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    estimated   INTEGER,
    latency_ms  INTEGER,
    retries     INTEGER,
    ok          INTEGER,
    cache_hit   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_task_id ON llm_calls(task_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_phase   ON llm_calls(phase);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts       ON llm_calls(ts);
"""


def _coerce(record: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _RECORD_KEYS:
        v = record.get(k)
        if v is None:
            if k in {"ts"}:
                v = time.time()
            elif k in {"task_id", "phase", "model"}:
                v = ""
            else:
                v = 0
        if k in {"estimated", "ok", "cache_hit"} and isinstance(v, bool):
            v = int(v)
        out[k] = v
    return out


def _accumulate(
    bucket: dict[str, dict[str, int | float]],
    key: str,
    tin: int,
    tout: int,
    lat: int,
) -> None:
    d = bucket.setdefault(
        key, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "_latency_sum": 0}
    )
    d["calls"] = int(d["calls"]) + 1
    d["tokens_in"] = int(d["tokens_in"]) + tin
    d["tokens_out"] = int(d["tokens_out"]) + tout
    d["_latency_sum"] = int(d["_latency_sum"]) + lat


def _finalize_avg_latency(bucket: dict[str, dict[str, int | float]]) -> None:
    for d in bucket.values():
        calls = int(d["calls"])
        d["avg_latency_ms"] = int(d["_latency_sum"] / calls) if calls else 0
        d.pop("_latency_sum", None)


def _since_to_epoch(since: str) -> float:
    s = since.strip()
    fmts = (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    )
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except ValueError:
            continue
    try:
        return float(s)
    except ValueError as e:
        raise ValueError(f"unparseable `since` value: {since!r}") from e


def _now_iso(clock: Any = None) -> str:
    epoch = clock() if clock is not None else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class SqliteMetricsSink:
    """Writes points into ``metrics_points``.

    Writes are batched and flushed on demand: a per-point commit on the hot path
    would serialise the whole pipeline on the SQLite write lock.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        connection: Optional[sqlite3.Connection] = None,
        clock: Any = None,
        batch_size: int = 64,
        namespace: str = "core",
    ) -> None:
        self._clock = clock or time.time
        self.namespace = namespace
        self.batch_size = max(1, int(batch_size))
        self._buffer: list[tuple[str, str, float, str, str, str]] = []
        self._lock = threading.Lock()
        if connection is not None:
            self.conn = connection
            self._owns = False
        else:
            self.db_path = db_path or ":memory:"
            if self.db_path != ":memory:":
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self._owns = True
        self.conn.executescript(METRICS_SCHEMA)
        self.conn.commit()

    def _push(self, kind: str, name: str, value: float, labels: Mapping[str, Any] | None, run_id: str, node: str) -> None:
        point = (
            _now_iso(self._clock),
            f"{kind}.{name}" if kind else name,
            float(value),
            json.dumps(dict(labels or {}), ensure_ascii=False, default=str),
            run_id or "",
            node or "",
        )
        with self._lock:
            self._buffer.append(point)
            if len(self._buffer) >= self.batch_size:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        rows = self._buffer
        self._buffer = []
        try:
            self.conn.executemany(
                "INSERT INTO metrics_points (ts, metric, value, labels, run_id, node) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.conn.commit()
        except Exception:  # pragma: no cover - metrics must never break a run
            pass

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def incr(self, name: str, value: float = 1, labels: Mapping[str, Any] | None = None,
             *, run_id: str = "", node: str = "") -> None:
        self._push("counter", name, value, labels, run_id, node)

    def gauge(self, name: str, value: float, labels: Mapping[str, Any] | None = None,
              *, run_id: str = "", node: str = "") -> None:
        self._push("gauge", name, value, labels, run_id, node)

    def observe(self, name: str, value: float, labels: Mapping[str, Any] | None = None,
                *, run_id: str = "", node: str = "") -> None:
        self._push("observation", name, value, labels, run_id, node)

    def points(self, *, metric: str = "", run_id: str = "") -> list[dict[str, Any]]:
        self.flush()
        sql = "SELECT * FROM metrics_points WHERE 1=1"
        params: list[Any] = []
        if metric:
            sql += " AND metric = ?"
            params.append(metric)
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        self.flush()
        if self._owns:
            try:
                self.conn.close()
            except Exception:  # pragma: no cover - defensive
                pass


# ── JSONL trace ──────────────────────────────────────────────────────────────
class TraceWriter:
    """Append-only JSONL event log, safe for concurrent writers in-process."""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        enabled: bool = True,
        clock: Any = None,
        max_value_chars: int = 2000,
        echo: bool = False,
    ) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self._clock = clock or time.time
        self.max_value_chars = int(max_value_chars)
        self.echo = echo
        self._lock = threading.Lock()
        self._handle: Optional[Any] = None
        self._events = 0
        if self.enabled and self.path:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    # ── writing ──────────────────────────────────────────────────────────────
    def event(self, kind: str, **payload: Any) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None
        record = {"ts": _now_iso(self._clock), "event": kind}
        for key, value in payload.items():
            if value is None:
                continue
            record[key] = self._clip(value)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._events += 1
            if self.path:
                if self._handle is None:
                    self._handle = open(self.path, "a", encoding="utf-8")
                self._handle.write(line + "\n")
                self._handle.flush()
            if self.echo:  # pragma: no cover - diagnostics only
                print(line)
        return record

    def _clip(self, value: Any) -> Any:
        if isinstance(value, str):
            if len(value) <= self.max_value_chars:
                return value
            return value[: self.max_value_chars] + "...[truncated]"
        if isinstance(value, Mapping):
            return {str(k): self._clip(v) for k, v in list(value.items())[:40]}
        if isinstance(value, (list, tuple)):
            return [self._clip(v) for v in list(value)[:40]]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if hasattr(value, "to_dict"):
            try:
                return self._clip(value.to_dict())
            except Exception:  # pragma: no cover - defensive
                return str(value)
        return str(value)[: self.max_value_chars]

    # ── reading ──────────────────────────────────────────────────────────────
    def events(self) -> list[dict[str, Any]]:
        if not self.path or not os.path.exists(self.path):
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        return self.events()[-max(1, int(n)):]

    @property
    def count(self) -> int:
        return self._events

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                finally:
                    self._handle = None


# ── collector ────────────────────────────────────────────────────────────────
class MetricsCollector:
    """Fan-out metrics facade: in-memory + SQLite + anything else.

    Also carries a :class:`TraceWriter`, so a single object can be handed to
    every component as the observability port.
    """

    def __init__(
        self,
        config: Optional[CoreConfig] = None,
        *,
        sinks: Sequence[Any] = (),
        trace: Optional[TraceWriter] = None,
        db_path: Optional[str | Path] = None,
        connection: Optional[sqlite3.Connection] = None,
        settings: Any = None,
        clock: Any = None,
    ) -> None:
        self.config = config or CoreConfig()
        self._clock = clock or time.time
        self.memory = InMemorySink()
        self.sqlite: Optional[SqliteMetricsSink] = None
        self._lock = threading.Lock()

        # Resolve DB path
        db_p: Optional[str] = None
        if db_path is not None:
            db_p = str(db_path)
        elif settings is not None and getattr(settings, "db_path", None):
            db_p = str(settings.db_path)
        elif self.config.metrics_enabled and self.config.db_path:
            db_p = str(self.config.db_path)

        self._db_path: Path = Path(db_p) if db_p else Path(":memory:")
        self.db_path: str = str(self._db_path)

        if db_p or connection is not None:
            self.sqlite = SqliteMetricsSink(
                db_p,
                connection=connection,
                clock=self._clock,
                namespace="core",
            )
            self._conn = self.sqlite.conn
            self._owns_conn = False
        else:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(METRICS_SCHEMA)
            self._conn.commit()
            self._owns_conn = True

        extra = [s for s in sinks if s is not None]
        self._sink = CompositeSink(*([self.memory] + ([self.sqlite] if self.sqlite else []) + extra))
        self.trace = trace if trace is not None else TraceWriter(
            self.config.trace_path, enabled=self.config.trace_enabled,
            clock=self._clock, max_value_chars=self.config.trace_max_value_chars,
        )

    # ── sink interface (GigaClient) ──────────────────────────────────────────
    def __call__(self, record: dict[str, Any]) -> None:
        """Sink entrypoint: GigaClient passes its record dict here."""
        self.record(**_coerce(record))

    def record(
        self,
        *,
        ts: float | None = None,
        task_id: str = "",
        phase: str = "",
        model: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        estimated: bool | int = 0,
        latency_ms: int = 0,
        retries: int = 0,
        ok: bool | int = 1,
        cache_hit: bool | int = 0,
    ) -> None:
        """Write one row to llm_calls and mirror to counters."""
        row = (
            float(ts) if ts is not None else time.time(),
            str(task_id),
            str(phase),
            str(model),
            int(tokens_in),
            int(tokens_out),
            int(bool(estimated)),
            int(latency_ms),
            int(retries),
            int(bool(ok)),
            int(bool(cache_hit)),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO llm_calls "
                "(ts, task_id, phase, model, tokens_in, tokens_out, estimated, "
                " latency_ms, retries, ok, cache_hit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            self._conn.commit()

        labels = {"phase": phase, "model": model}
        if task_id:
            labels["task_id"] = task_id
        self.incr("llm.calls", 1, labels)
        self.incr("llm.tokens.in", int(tokens_in), labels)
        self.incr("llm.tokens.out", int(tokens_out), labels)
        if latency_ms:
            self.observe("llm.latency_ms", float(latency_ms), labels)

    def _fetch(
        self, *, since: str | None = None, task_id: str | None = None, until: str | None = None
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(_since_to_epoch(since))
        if until is not None:
            clauses.append("ts < ?")
            params.append(_since_to_epoch(until))
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, task_id, phase, model, tokens_in, tokens_out, "
                "       estimated, latency_ms, retries, ok, cache_hit "
                "FROM llm_calls" + where + " ORDER BY ts ASC",
                params,
            )
            return cur.fetchall()

    def aggregate(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate metrics."""
        rows = self._fetch(since=since, until=until, task_id=task_id)
        total_calls = len(rows)
        total_in = 0
        total_out = 0
        estimated_calls = 0
        cache_hits = 0
        rejected = 0
        by_phase: dict[str, dict[str, int | float]] = {}
        by_model: dict[str, dict[str, int | float]] = {}
        for r in rows:
            (_ts, _tid, phase, model, tin, tout, est, lat, _retr, ok, chit) = r
            total_in += tin
            total_out += tout
            if est:
                estimated_calls += 1
            if chit:
                cache_hits += 1
            if not ok:
                rejected += 1
            _accumulate(by_phase, phase, tin, tout, lat)
            _accumulate(by_model, model, tin, tout, lat)
        _finalize_avg_latency(by_phase)
        _finalize_avg_latency(by_model)
        cost_per_phase = {
            p: int(d["tokens_in"]) + int(d["tokens_out"]) for p, d in by_phase.items()
        }
        reject_rate = (rejected / total_calls) if total_calls else 0.0
        return {
            "total_calls": total_calls,
            "total_tokens_in": total_in,
            "total_tokens_out": total_out,
            "estimated_calls": estimated_calls,
            "cache_hits": cache_hits,
            "reject_rate": reject_rate,
            "by_phase": by_phase,
            "by_model": by_model,
            "cost_per_phase": cost_per_phase,
        }

    def daily_report(
        self, date: str | None = None, task_id: str | None = None
    ) -> str:
        """Human-readable text report for ONE day (00:00–24:00 UTC)."""
        if date is None:
            date = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        until_date = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        agg = self.aggregate(since=date, until=until_date, task_id=task_id)
        lines: list[str] = []
        lines.append(f"GigaForge daily report — {date}")
        lines.append("=" * 60)
        lines.append(f"Total calls       : {agg['total_calls']}")
        lines.append(f"Tokens in  (sum)  : {agg['total_tokens_in']}")
        lines.append(f"Tokens out (sum)  : {agg['total_tokens_out']}")
        lines.append(f"Total tokens      : "
                     f"{int(agg['total_tokens_in']) + int(agg['total_tokens_out'])}")
        lines.append(f"Estimated calls   : {agg['estimated_calls']}")
        lines.append(f"Cache hits        : {agg['cache_hits']}")
        total_calls = int(agg["total_calls"]) + int(agg["estimated_calls"])
        hit_rate = int(agg["cache_hits"]) / total_calls if total_calls > 0 else 0.0
        lines.append(f"Cache hit rate    : {hit_rate:.2%}")
        lines.append(f"Reject rate       : {agg['reject_rate']:.2%}")
        lines.append("")
        lines.append("By phase:")
        if agg["by_phase"]:
            lines.append(
                f"  {'phase':<14}{'calls':>8}{'tok_in':>10}{'tok_out':>10}{'avg_ms':>10}"
            )
            for phase, d in sorted(agg["by_phase"].items()):
                lines.append(
                    f"  {phase:<14}{int(d['calls']):>8}"
                    f"{int(d['tokens_in']):>10}{int(d['tokens_out']):>10}"
                    f"{int(d['avg_latency_ms']):>10}"
                )
        else:
            lines.append("  (no calls)")
        lines.append("")
        lines.append("By model:")
        if agg["by_model"]:
            lines.append(
                f"  {'model':<22}{'calls':>8}{'tok_in':>10}{'tok_out':>10}{'avg_ms':>10}"
            )
            for model, d in sorted(agg["by_model"].items()):
                lines.append(
                    f"  {model:<22}{int(d['calls']):>8}"
                    f"{int(d['tokens_in']):>10}{int(d['tokens_out']):>10}"
                    f"{int(d['avg_latency_ms']):>10}"
                )
        else:
            lines.append("  (no calls)")
        lines.append("")
        lines.append("Cost per phase (in+out tokens):")
        if agg["cost_per_phase"]:
            for p, c in sorted(agg["cost_per_phase"].items()):
                lines.append(f"  {p:<14}{c}")
        else:
            lines.append("  (no calls)")
        return "\n".join(lines)

    def dump_json(self, *, since: str | None = None, task_id: str | None = None) -> str:
        rows = self._fetch(since=since, task_id=task_id)
        out = [dict(zip(_RECORD_KEYS, r, strict=True)) for r in rows]
        return json.dumps(out, default=float)

    # ── MetricsSink protocol ─────────────────────────────────────────────────
    def incr(
        self,
        name: str,
        value: float = 1,
        labels: Optional[Mapping[str, Any]] = None,
        *,
        run_id: str = "",
        node: str = "",
    ) -> None:
        """Increment a counter, optionally scoped to a run and node.

        The scope is optional so every existing two/three-argument call keeps
        working; passing it lets a per-run view filter points that would otherwise
        be indistinguishable from lifetime totals.
        """
        self._emit("incr", name, value, labels, run_id, node)

    def gauge(
        self,
        name: str,
        value: float,
        labels: Optional[Mapping[str, Any]] = None,
        *,
        run_id: str = "",
        node: str = "",
    ) -> None:
        self._emit("gauge", name, value, labels, run_id, node)

    def observe(
        self,
        name: str,
        value: float,
        labels: Optional[Mapping[str, Any]] = None,
        *,
        run_id: str = "",
        node: str = "",
    ) -> None:
        self._emit("observe", name, value, labels, run_id, node)

    def _emit(
        self,
        kind: str,
        name: str,
        value: float,
        labels: Optional[Mapping[str, Any]],
        run_id: str,
        node: str,
    ) -> None:
        """Forward to the sinks, using the scoped API where a sink offers it."""
        sink = self._sink
        scoped = (run_id or node) and kind in {"incr", "gauge", "observe"}
        if scoped:
            method = getattr(sink, kind, None)
            try:
                method(name, value, labels, run_id=run_id, node=node)  # type: ignore[union-attr]
                return
            except TypeError:
                # A sink that predates the scoped signature: fall back to the plain
                # call rather than dropping the measurement.
                pass
        getattr(sink, kind)(name, value, labels)

    def snapshot(self) -> dict[str, Any]:
        return self._sink.snapshot()

    def reset(self) -> None:
        self._sink.reset()

    # ── convenience ──────────────────────────────────────────────────────────
    def counter(self, name: str) -> float:
        return self.memory.counter(name)

    def values(self, name: str) -> Sequence[float]:
        return self.memory.values(name)

    def p95(self, name: str) -> Optional[float]:
        return percentile(self.values(name), 95)

    def flush(self) -> None:
        if self.sqlite is not None:
            self.sqlite.flush()

    def close(self) -> None:
        self.flush()
        if getattr(self, "_owns_conn", False):
            try:
                self._conn.close()
            except Exception:
                pass
        if self.sqlite is not None:
            self.sqlite.close()
        self.trace.close()

    def __enter__(self) -> "MetricsCollector":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"MetricsCollector(trace={self.trace.count} events, sqlite={self.sqlite is not None})"


def read_jsonl(path: str) -> list[dict[str, Any]]:
    """Read a JSONL file, skipping malformed lines."""
    out: list[dict[str, Any]] = []
    if not path or not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


__all__ = [
    "MetricsCollector",
    "SqliteMetricsSink",
    "TraceWriter",
    "METRICS_SCHEMA",
    "read_jsonl",
]

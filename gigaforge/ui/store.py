"""Read-only data access for the GigaForge V2 UI layer.

The UI must never need a live pipeline object.  Everything the TUI, the web
panel and the agentic chat render is read back from the sinks the pipeline
already writes:

* ``metrics_points``  — counters / gauges / observations (SQLite)
* ``runs`` / ``node_runs`` / ``node_checkpoints`` / ``dlq`` — the DAG journal
* ``cache_entries``   — cache contents and hit counters
* the JSONL **trace** — the ordered event stream of a run

Two rules, each learned the hard way:

1. **Stored metric names carry a kind prefix.**  ``SqliteMetricsSink`` writes a
   point as ``f"{kind}.{name}"``, so the metric a component calls
   ``core.router.calls`` lands in the table as ``counter.core.router.calls``.
   A reader matching only the canonical spelling silently returns *zero* — worse
   than an error, because the dashboard simply looks empty.  Every lookup goes
   through :func:`stored_variants` and a ``metric IN (...)`` predicate.
2. **The read path never mutates.**  Connections are opened read-only
   (``mode=ro``) so an open dashboard can never block the writer, and nothing in
   this module writes.  Cache mutation lives elsewhere by design.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

#: Kind prefixes the SQLite sink prepends to every metric name.
KIND_PREFIXES: tuple[str, ...] = ("counter", "gauge", "observation")

#: Canonical metric names the UI reads, grouped by the component that emits them.
#: Kept as data (not code) so the web panel can advertise what it will show.
UI_METRICS: dict[str, str] = {
    # model router
    "core.router.calls": "counter",
    "core.router.tokens": "counter",
    "core.router.errors": "counter",
    "core.router.fallback": "counter",
    "core.router.latency_ms": "observation",
    # cache
    "core.cache.hit": "counter",
    "core.cache.miss": "counter",
    "core.cache.write": "counter",
    "core.cache.invalidate": "counter",
    # repair / resilience
    "core.fixloop.runs": "counter",
    "core.fixloop.iterations": "observation",
    "core.breaker.rejected": "counter",
    # delegation
    "core.subagent.runs": "counter",
    "core.subagent.duration_ms": "observation",
    # dag + pipeline lifecycle (emitted as ``core.<event>``)
    "core.dag.start": "counter",
    "core.dag.node_start": "counter",
    "core.dag.node_error": "counter",
    "core.dag.node_failed": "counter",
    "core.dag.finish": "counter",
    "core.dag.resume": "counter",
    "core.pipeline.intake": "counter",
    "core.pipeline.plan": "counter",
    "core.pipeline.plan_cached": "counter",
    "core.pipeline.cache_hit": "counter",
    "core.pipeline.settle": "counter",
    "core.pipeline.persist": "counter",
    "core.pipeline.index_error": "counter",
    "core.pipeline.slice_empty": "counter",
    "core.pipeline.summary_error": "counter",
    # client layer
    "client.usage.total_tokens": "counter",
    "client.usage.prompt_tokens": "counter",
    "client.request": "counter",
    "client.response": "counter",
    "client.latency_ms": "observation",
    "client.ratelimit.429": "counter",
}


def stored_variants(name: str) -> list[str]:
    """Every spelling of ``name`` the SQLite sink may have used.

    ``core.router.calls`` is stored as ``counter.core.router.calls``; a name that
    already carries a prefix is used verbatim.
    """
    if name.startswith(tuple(f"{kind}." for kind in KIND_PREFIXES)):
        return [name]
    return [f"{kind}.{name}" for kind in KIND_PREFIXES] + [name]


def canonical(name: str) -> str:
    """Strip the kind prefix a stored metric name carries."""
    for kind in KIND_PREFIXES:
        prefix = f"{kind}."
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _normalise_run_row(row: dict[str, Any]) -> None:
    """Decode a run row's JSON columns and derive a displayable ``goal``.

    The JSON columns hold whatever the pipeline passed in, so the goal hides in
    different places depending on the version: ``input.task`` for a V2 run, the
    legacy ``output`` payload for anything older, and occasionally a plain string.
    Deriving it once, here, keeps every renderer from re-implementing the fallback
    chain — and is why the runs table no longer shows blank goals.
    """
    for key in ("input", "output", "error"):
        if key in row:
            row[key] = _json_or_raw(row[key])

    goal = ""
    for key in ("input", "output"):
        value = row.get(key)
        if isinstance(value, Mapping):
            goal = str(value.get("task") or value.get("goal") or value.get("input") or "")
        elif isinstance(value, str) and value.strip():
            goal = value
        if goal.strip():
            break
    row["goal"] = " ".join(goal.split())[:500]

    # ``accepted`` is the run's actual verdict, stored in the finished output.  The
    # runs table has no column for it, and defaulting to False made every
    # successful run render as "not accepted" — the one badge an operator trusts.
    output = row.get("output")
    if isinstance(output, Mapping):
        accepted = output.get("accepted")
        if isinstance(accepted, bool):
            row["accepted"] = accepted
        summary = output.get("summary")
        if isinstance(summary, str) and summary.strip():
            row.setdefault("summary", summary)
        review = output.get("review_verdict")
        if isinstance(review, Mapping):
            row.setdefault("review_verdict", dict(review))


def _json_or_raw(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL file, skipping blank and malformed lines."""
    p = Path(path)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return out


class UiStore:
    """Read-only facade over the GigaForge sinks.

    Every accessor is defensive: a missing database, a missing table or a
    database that does not exist yet (a fresh checkout) yields empty results
    rather than an exception, so a dashboard opened before the first run shows
    an honest empty state instead of a traceback.
    """

    def __init__(
        self,
        *,
        db_path: str | Path = ".gigaforge/core.sqlite3",
        trace_path: str | Path = ".gigaforge/traces/core.jsonl",
        l0_db_path: str | Path | None = ".gigaforge/l0.sqlite3",
        report_dir: str | Path | None = None,
        timeout: float = 2.0,
    ) -> None:
        self.db_path = str(db_path)
        self.trace_path = str(trace_path)
        self.l0_db_path = str(l0_db_path) if l0_db_path else None
        self.report_dir = str(report_dir) if report_dir else None
        self.timeout = float(timeout)
        self._conn: Optional[sqlite3.Connection] = None
        self._l0_conn: Optional[sqlite3.Connection] = None

    # ── construction ──────────────────────────────────────────────────────────
    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        report_dir: str | Path | None = None,
    ) -> "UiStore":
        """Build a store from a :class:`gigaforge.core.CoreConfig`."""
        db_path = getattr(config, "db_path", ".gigaforge/core.sqlite3")
        trace_path = getattr(config, "trace_path", ".gigaforge/traces/core.jsonl")
        return cls(db_path=db_path, trace_path=trace_path, report_dir=report_dir)

    # ── connections (read-only) ───────────────────────────────────────────────
    @staticmethod
    def _connect_readonly(path: str, timeout: float) -> Optional[sqlite3.Connection]:
        p = Path(path)
        if not p.is_file():
            return None
        try:
            uri = f"file:{p.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        except sqlite3.Error:
            return None
        conn.row_factory = sqlite3.Row
        return conn

    @property
    def conn(self) -> Optional[sqlite3.Connection]:
        if self._conn is None:
            self._conn = self._connect_readonly(self.db_path, self.timeout)
        return self._conn

    @property
    def l0_conn(self) -> Optional[sqlite3.Connection]:
        if self._l0_conn is None and self.l0_db_path:
            self._l0_conn = self._connect_readonly(self.l0_db_path, self.timeout)
        return self._l0_conn

    def close(self) -> None:
        for conn in (self._conn, self._l0_conn):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - defensive
                    pass
        self._conn = None
        self._l0_conn = None

    def __enter__(self) -> "UiStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── generic query helpers ─────────────────────────────────────────────────
    @property
    def db_present(self) -> bool:
        return Path(self.db_path).is_file()

    @property
    def trace_present(self) -> bool:
        return Path(self.trace_path).is_file()

    def tables(self, *, l0: bool = False) -> list[str]:
        conn = self.l0_conn if l0 else self.conn
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [str(r["name"]) for r in rows]

    def _has_table(self, table: str, *, l0: bool = False) -> bool:
        return table in self.tables(l0=l0)

    def query(self, sql: str, params: Sequence[Any] = (), *, l0: bool = False) -> list[dict[str, Any]]:
        """Run a read-only query, returning ``[]`` on any failure."""
        conn = self.l0_conn if l0 else self.conn
        if conn is None:
            return []
        try:
            cur = conn.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]
        except sqlite3.Error:
            return []

    # ── metrics ───────────────────────────────────────────────────────────────
    def metric_points(
        self,
        *,
        metric: str | None = None,
        run_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._has_table("metrics_points"):
            return []
        sql = "SELECT id, ts, metric, value, labels, run_id, node FROM metrics_points WHERE 1=1"
        params: list[Any] = []
        if metric:
            variants = stored_variants(metric)
            sql += f" AND metric IN ({','.join('?' * len(variants))})"
            params.extend(variants)
        if run_id:
            sql += " AND (run_id = ? OR run_id = '' OR run_id IS NULL)"
            params.append(run_id)
        sql += " ORDER BY id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.query(sql, params)
        for row in rows:
            row["labels"] = _json_or_raw(row.get("labels")) or {}
            row["canonical"] = canonical(str(row.get("metric", "")))
        return rows

    def metric_values(self, metric: str, *, run_id: str | None = None) -> list[float]:
        """Every recorded value of ``metric``, in insertion order."""
        return [
            float(row["value"])
            for row in self.metric_points(metric=metric, run_id=run_id)
        ]

    def metric_sum(self, metric: str, *, run_id: str | None = None) -> float:
        """Counter semantics: sum of every recorded point."""
        return float(sum(self.metric_values(metric, run_id=run_id)))

    def metric_last(self, metric: str, *, run_id: str | None = None) -> Optional[float]:
        """Gauge semantics: the most recently recorded point."""
        values = self.metric_values(metric, run_id=run_id)
        return values[-1] if values else None

    def metric_names(self) -> list[str]:
        if not self._has_table("metrics_points"):
            return []
        rows = self.query("SELECT DISTINCT metric FROM metrics_points ORDER BY metric")
        return [str(r["metric"]) for r in rows]

    # ── runs / DAG journal ────────────────────────────────────────────────────
    def runs(self, *, limit: int = 25, status: str | None = None) -> list[dict[str, Any]]:
        if not self._has_table("runs"):
            return []
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.query(sql, params)
        for row in rows:
            _normalise_run_row(row)
        return rows

    def run(self, run_id: str) -> Optional[dict[str, Any]]:
        if not self._has_table("runs"):
            return None
        rows = self.query("SELECT * FROM runs WHERE run_id = ? LIMIT 1", (run_id,))
        if not rows:
            return None
        row = rows[0]
        _normalise_run_row(row)
        return row

    def latest_run_id(self) -> Optional[str]:
        runs = self.runs(limit=1)
        return str(runs[0]["run_id"]) if runs else None

    def node_runs(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Per-node outcomes, in the pipeline's declared order when known."""
        if not self._has_table("node_runs"):
            return []
        if run_id:
            # ``rowid`` breaks the tie: rows written in the same server second
            # share ``started_at``, and without it the timeline order would flip
            # between reads depending on how SQLite happens to scan the table.
            rows = self.query(
                "SELECT * FROM node_runs WHERE run_id = ? ORDER BY started_at, rowid, node",
                (run_id,),
            )
        else:
            rows = self.query("SELECT * FROM node_runs ORDER BY rowid DESC LIMIT 200")
        for row in rows:
            row["attrs"] = _json_or_raw(row.get("attrs"))
        return rows

    def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        if not self._has_table("node_checkpoints"):
            return []
        return self.query(
            "SELECT * FROM node_checkpoints WHERE run_id = ? ORDER BY node", (run_id,)
        )

    def dlq(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if not self._has_table("dlq"):
            return []
        if run_id:
            return self.query("SELECT * FROM dlq WHERE run_id = ? ORDER BY rowid", (run_id,))
        return self.query("SELECT * FROM dlq ORDER BY rowid DESC LIMIT 100")

    # ── cache ─────────────────────────────────────────────────────────────────
    def cache_entries(self, *, namespace: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Cache metadata (never the blob payload — a viewer must stay cheap)."""
        if not self._has_table("cache_entries"):
            return []
        sql = (
            "SELECT cache_key, namespace, value_type, model, created_at, expires_at, "
            "hits, bytes FROM cache_entries"
        )
        params: list[Any] = []
        if namespace:
            sql += " WHERE namespace = ?"
            params.append(namespace)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return self.query(sql, params)

    def cache_summary(self) -> dict[str, Any]:
        """Contents of the cache table plus the emitted hit/miss counters."""
        entries = self.cache_entries(limit=100000)
        namespaces: dict[str, int] = {}
        total_bytes = 0
        for entry in entries:
            ns = str(entry.get("namespace") or "-")
            namespaces[ns] = namespaces.get(ns, 0) + 1
            total_bytes += int(entry.get("bytes") or 0)
        hits = int(self.metric_sum("core.cache.hit"))
        misses = int(self.metric_sum("core.cache.miss"))
        writes = int(self.metric_sum("core.cache.write"))
        total = hits + misses
        return {
            "entries": len(entries),
            "bytes": total_bytes,
            "namespaces": namespaces,
            "hits": hits,
            "misses": misses,
            "writes": writes,
            "hit_rate": round(hits / total, 4) if total else 0.0,
        }

    # ── trace ─────────────────────────────────────────────────────────────────
    def trace_events(
        self, *, run_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        events = read_jsonl(self.trace_path)
        if run_id:
            filtered = [e for e in events if str(e.get("run_id") or "") == run_id]
            # Events without a run_id (e.g. cache.invalidate) are still relevant
            # to a single-run view, so keep them appended in order at the tail.
            if filtered:
                events = filtered
        if limit:
            events = events[-int(limit) :]
        return events

    def trace_tail(self, n: int = 50) -> list[dict[str, Any]]:
        return self.trace_events(limit=n)

    def trace_counts(self, run_id: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.trace_events(run_id=run_id):
            kind = str(event.get("event") or "?")
            counts[kind] = counts.get(kind, 0) + 1
        return dict(sorted(counts.items()))

    # ── diff / run report ─────────────────────────────────────────────────────
    def _report_candidates(self, run_id: str) -> Iterable[Path]:
        dirs: list[Path] = []
        if self.report_dir:
            dirs.append(Path(self.report_dir))
        # A conventional sidecar next to the trace: .gigaforge/reports/
        dirs.append(Path(self.trace_path).parent.parent / "reports")
        dirs.append(Path(self.db_path).parent / "reports")
        for base in dirs:
            yield base / f"{run_id}.json"
            yield base / f"{run_id}.report.json"

    def run_report(self, run_id: str) -> Optional[dict[str, Any]]:
        """Full run report: the ``runs.output`` JSON, or a sidecar file."""
        row = self.run(run_id)
        if row is not None and isinstance(row.get("output"), Mapping):
            return dict(row["output"])
        for candidate in self._report_candidates(run_id):
            if candidate.is_file():
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
        return None

    def diff_text(self, run_id: str) -> str:
        """The run's final diff, from the report if it recorded one."""
        report = self.run_report(run_id) or {}
        for key in ("final_diff", "diff", "patch", "unified_diff"):
            value = report.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, Mapping):
                inner = value.get("text") or value.get("diff")
                if isinstance(inner, str) and inner.strip():
                    return inner
        return ""

    def changed_files(self, run_id: str) -> list[str]:
        report = self.run_report(run_id) or {}
        value = report.get("changed_files")
        if isinstance(value, list):
            return [str(v) for v in value]
        row = self.run(run_id) or {}
        output = row.get("output") if isinstance(row.get("output"), Mapping) else {}
        nodes = output.get("nodes") if isinstance(output, Mapping) else None
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, Mapping) and node.get("changed_files"):
                    return [str(v) for v in node["changed_files"]]
        return []

    # ── L0 index (optional) ───────────────────────────────────────────────────
    def index_summary(self) -> dict[str, Any]:
        """Symbol-index counts, when an L0 database is present."""
        if not self._has_table("l0_files", l0=True):
            return {"present": False, "files": 0, "symbols": 0, "languages": {}}
        files = self.query(
            "SELECT COUNT(*) AS n FROM l0_files WHERE deleted_at IS NULL", l0=True
        )
        symbols = self.query("SELECT COUNT(*) AS n FROM l0_symbols", l0=True)
        langs = self.query(
            "SELECT language, COUNT(*) AS n FROM l0_files WHERE deleted_at IS NULL "
            "GROUP BY language ORDER BY n DESC",
            l0=True,
        )
        return {
            "present": True,
            "files": int((files[0]["n"] if files else 0) or 0),
            "symbols": int((symbols[0]["n"] if symbols else 0) or 0),
            "languages": {str(r["language"]): int(r["n"] or 0) for r in langs},
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"UiStore(db={self.db_path!r}, trace={self.trace_path!r}, present={self.db_present})"


__all__ = [
    "UiStore",
    "KIND_PREFIXES",
    "UI_METRICS",
    "canonical",
    "stored_variants",
    "read_jsonl",
]

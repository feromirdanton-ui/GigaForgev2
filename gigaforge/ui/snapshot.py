"""One UI-ready snapshot, assembled from the persisted sinks.

The TUI, the web panel and the chat interface all render **this dict** and
nothing else.  That is the whole point: a single snapshot builder means one
definition of every KPI and no chance of the terminal showing a different cache
hit rate than the browser.

Order of preference for the numbers:

1. ``runs.output`` / a sidecar run report — the richest source, because the
   pipeline recorded phase usages and node records directly;
2. the SQLite journal (``runs`` + ``node_runs``);
3. the JSONL trace (``dag.*`` / ``router.call`` / ``cache.*`` events).

Each level degrades gracefully into the next, and a completely empty data
directory produces a valid snapshot with zeros and an honest ``missing`` note
instead of an exception — a dashboard opened before the first run must render.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from . import analytics as A
from .store import UiStore

#: The pipeline's declared node order (``core.pipeline.NODE_ORDER``), duplicated
#: as a constant so a progress bar can show "3/11" before the first run.
NODE_ORDER: tuple[str, ...] = (
    "intake",
    "index",
    "slice",
    "cache",
    "plan",
    "act",
    "validate",
    "fix",
    "settle",
    "summarize",
    "persist",
)

SNAPSHOT_VERSION = 2

#: Trace events worth showing in a viewer (lifecycle noise is filtered out).
STEP_EVENTS: tuple[str, ...] = (
    "dag.start",
    "dag.node_start",
    "dag.node_error",
    "dag.node_failed",
    "dag.resume",
    "dag.finish",
    "router.call",
    "router.error",
    "cache.hit",
    "cache.miss",
    "cache.write",
    "cache.invalidate",
    "fixloop.finish",
    "subagents.spawn",
    "subagents.finish",
    "subagents.no_worktree",
    "subagent.start",
    "subagent.finish",
    "pipeline.intake",
    "pipeline.plan",
    "pipeline.plan_cached",
    "pipeline.cache_hit",
    "pipeline.settle",
    "pipeline.persist",
    "pipeline.index_error",
    "pipeline.slice_empty",
    "pipeline.summary_error",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _phase_breakdown(
    store: UiStore,
    prices: Any,
    run_id: Optional[str],
    *,
    model_by_phase: Mapping[str, str],
    default_model: str,
) -> list[dict[str, Any]]:
    """Per-phase calls / tokens / cost / p95 latency, from the router metrics."""
    tokens_by_phase = A.group_points(
        store.metric_points(metric="core.router.tokens", run_id=run_id), by="phase"
    )
    calls_by_phase = A.group_points(
        store.metric_points(metric="core.router.calls", run_id=run_id), by="phase"
    )
    errors_by_phase = A.group_points(
        store.metric_points(metric="core.router.errors", run_id=run_id), by="phase"
    )
    latency_by_phase: dict[str, list[float]] = {}
    for point in store.metric_points(metric="core.router.latency_ms", run_id=run_id):
        labels = point.get("labels") or {}
        phase = str(labels.get("phase") or "-")
        latency_by_phase.setdefault(phase, []).append(A.as_float(point.get("value")))

    phases = sorted(set(tokens_by_phase) | set(calls_by_phase) | set(latency_by_phase))
    out: list[dict[str, Any]] = []
    for phase in phases:
        tokens = A.as_int(tokens_by_phase.get(phase, {}).get("value"))
        prices_model = model_by_phase.get(phase) or default_model
        # ``core.router.tokens`` records the total; the split between prompt and
        # completion is not carried on the label, so price the whole total at the
        # completion rate's midpoint — deterministic and documented, rather than
        # inventing a 50/50 split silently.
        micros = A.cost_micros(prices, prices_model, 0, tokens)
        out.append(
            {
                "phase": phase,
                "calls": A.as_int(calls_by_phase.get(phase, {}).get("value")),
                "tokens": tokens,
                "cost_micros": micros,
                "cost_usd": round(micros / A.MICROS_PER_USD, 6),
                "errors": A.as_int(errors_by_phase.get(phase, {}).get("value")),
                "latency_p95_ms": A.percentile(latency_by_phase.get(phase, []), 95),
                "model": prices_model,
            }
        )
    return out


def _model_breakdown(store: UiStore, run_id: Optional[str]) -> list[dict[str, Any]]:
    calls = A.group_points(
        store.metric_points(metric="core.router.calls", run_id=run_id), by="model"
    )
    fallbacks = A.group_points(
        store.metric_points(metric="core.router.fallback", run_id=run_id), by="phase"
    )
    out = [
        {
            "model": model,
            "calls": A.as_int(bucket.get("value")),
        }
        for model, bucket in calls.items()
    ]
    if fallbacks:
        out.append(
            {
                "model": "<fallback>",
                "calls": A.as_int(sum(b["value"] for b in fallbacks.values())),
            }
        )
    return sorted(out, key=lambda row: (-row["calls"], row["model"]))


def build_snapshot(
    store: UiStore,
    *,
    run_id: Optional[str] = None,
    prices: Any = None,
    default_model: str = "GigaChat",
    diff_limit: int = 24000,
    trace_limit: int = 400,
) -> dict[str, Any]:
    """Assemble the single dict every renderer consumes.

    ``run_id=None`` means "the newest run we know about", falling back to the
    whole log when no run row exists.
    """
    prices = prices or A.price_table()
    run_row = store.run(run_id) if run_id else None
    if run_row is None:
        run_row = (store.runs(limit=1) or [None])[0]
    effective_run = str((run_row or {}).get("run_id") or run_id or "")

    report = store.run_report(effective_run) if effective_run else None
    node_rows = store.node_runs(effective_run or None)
    steps = A.timeline(node_rows) if node_rows else []
    events = store.trace_events(run_id=effective_run or None, limit=trace_limit)
    if not steps:
        steps = A.trace_timeline(events)

    # ── tokens & cost ─────────────────────────────────────────────────────────
    router_tokens = A.as_int(store.metric_sum("core.router.tokens", run_id=effective_run or None))
    client_tokens = A.as_int(
        store.metric_sum("client.usage.total_tokens", run_id=effective_run or None)
    )
    total_tokens = A.as_int((report or {}).get("tokens")) or A.as_int(
        (run_row or {}).get("total_tokens")
    ) or router_tokens or client_tokens
    prompt_tokens = A.as_int((report or {}).get("prompt_tokens")) or A.as_int(
        store.metric_sum("client.usage.prompt_tokens", run_id=effective_run or None)
    )
    calls = A.as_int((report or {}).get("calls")) or A.as_int(
        store.metric_sum("core.router.calls", run_id=effective_run or None)
    )
    errors = A.as_int(store.metric_sum("core.router.errors", run_id=effective_run or None))

    model_by_phase: dict[str, str] = {}
    for point in store.metric_points(metric="core.router.calls", run_id=effective_run or None):
        labels = point.get("labels") or {}
        phase = str(labels.get("phase") or "")
        model = str(labels.get("model") or "")
        if phase and model:
            model_by_phase.setdefault(phase, model)

    phases = _phase_breakdown(
        store, prices, effective_run or None,
        model_by_phase=model_by_phase, default_model=default_model,
    )
    models = _model_breakdown(store, effective_run or None)

    report_phase_cost = (report or {}).get("cost", {}).get("by_phase_micros") or {}
    if report_phase_cost:
        cost_by_phase_micros = {str(k): A.as_int(v) for k, v in report_phase_cost.items()}
    else:
        cost_by_phase_micros = {row["phase"]: row["cost_micros"] for row in phases}

    report_cost_micros = A.as_int(
        (report or {}).get("cost", {}).get("micros")
        if isinstance((report or {}).get("cost"), Mapping)
        else 0
    )
    total_cost_micros = report_cost_micros or A.as_int((run_row or {}).get("cost_micros")) or sum(
        cost_by_phase_micros.values()
    )
    if total_cost_micros == 0 and total_tokens:
        total_cost_micros = A.cost_micros(prices, default_model, 0, total_tokens)

    # ── cache / repair / health ──────────────────────────────────────────────
    cache = store.cache_summary()
    proposals = A.as_int(store.metric_sum("core.fixloop.runs", run_id=effective_run or None))
    iterations = store.metric_values("core.fixloop.iterations", run_id=effective_run or None)
    rejections = sum(1 for event in events if str(event.get("event")) == "fixloop.rejected")
    accepted_events = sum(1 for event in events if str(event.get("event")) == "fixloop.accepted")

    latency_samples = store.metric_values("core.router.latency_ms", run_id=effective_run or None)
    model_latency = store.metric_values("client.latency_ms", run_id=effective_run or None)

    diff_text = store.diff_text(effective_run) if effective_run else ""
    diff = A.diff_stats(diff_text)
    changed_files = store.changed_files(effective_run) if effective_run else []
    if not changed_files:
        changed_files = list(diff["files"])

    errors_list = list((report or {}).get("errors") or [])
    if not errors_list:
        errors_list = [
            f"{event.get('node') or event.get('phase') or event.get('event')}: "
            f"{event.get('error') or event.get('code') or 'error'}"
            for event in events
            if str(event.get("event")) in {"dag.node_error", "dag.node_failed", "router.error"}
        ]

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "generated_at": _iso_now(),
        "source": {
            "db_path": store.db_path,
            "trace_path": store.trace_path,
            "db_present": store.db_present,
            "trace_present": store.trace_present,
            "missing": _missing_note(store),
        },
        "run": {
            "run_id": effective_run,
            "goal": str(
                (report or {}).get("goal")
                or (run_row or {}).get("goal")
                or (run_row or {}).get("input")
                or ""
            ),
            "status": str(
                (report or {}).get("status") or (run_row or {}).get("status") or "unknown"
            ),
            "accepted": bool(
                (report or {}).get("accepted", False)
            ),
            "started_at": str((run_row or {}).get("started_at") or ""),
            "finished_at": str((run_row or {}).get("finished_at") or ""),
            "duration_ms": A.as_float(
                (report or {}).get("duration_ms") or (run_row or {}).get("duration_ms")
            ),
        },
        "kpis": {
            "tokens": {
                "total": total_tokens,
                "prompt": prompt_tokens,
                "completion": A.as_int(
                    (report or {}).get("completion_tokens")
                ) or max(0, total_tokens - prompt_tokens),
                "calls": calls,
            },
            "cost": {
                "micros": total_cost_micros,
                "usd": round(total_cost_micros / A.MICROS_PER_USD, 6),
                "display": A.human_usd(total_cost_micros),
                "by_phase_usd": {
                    phase: round(micros / A.MICROS_PER_USD, 6)
                    for phase, micros in sorted(cost_by_phase_micros.items())
                },
                "prices": {
                    name: price for name, price in _price_rows(prices)
                },
            },
            "cache": cache,
            "repair": {
                "proposals": max(proposals, accepted_events),
                "rejections": rejections,
                "reject_rate": A.rate(rejections, max(proposals, accepted_events, rejections)),
                "fixloop_runs": proposals,
                "max_iterations": A.as_int(max(iterations)) if iterations else 0,
                "avg_iterations": round(sum(iterations) / len(iterations), 2) if iterations else 0.0,
            },
            "latency": {
                "router_p50_ms": A.percentile(latency_samples, 50),
                "router_p95_ms": A.percentile(latency_samples, 95),
                "client_p95_ms": A.percentile(model_latency, 95),
                "samples": len(latency_samples),
            },
            "health": {
                "router_errors": errors,
                "breaker_rejected": A.as_int(store.metric_sum("core.breaker.rejected")),
                "ratelimit_429": A.as_int(store.metric_sum("client.ratelimit.429")),
                "subagent_runs": A.as_int(store.metric_sum("core.subagent.runs")),
            },
        },
        "phases": phases,
        "models": models,
        "steps": steps,
        "progress": A.progress(steps, expected=NODE_ORDER),
        "node_order": list(NODE_ORDER),
        "step_status_counts": A.status_counts(steps),
        "trace": [
            {k: v for k, v in event.items() if k in _TRACE_KEYS}
            for event in events
            if str(event.get("event")) in STEP_EVENTS
        ],
        "trace_counts": store.trace_counts(effective_run or None),
        "diff": {
            "chars": diff["chars"],
            "files": changed_files,
            "hunks": diff["hunks"],
            "added": diff["added"],
            "removed": diff["removed"],
            "text": diff_text[:diff_limit],
            "truncated": len(diff_text) > diff_limit,
        },
        "errors": errors_list[:50],
        "index": store.index_summary(),
        "totals": {
            "runs_recorded": len(store.runs(limit=1000)),
            "cache_entries": cache["entries"],
            "trace_events": len(store.trace_events()),
            "metrics_points": len(store.metric_points(limit=100000)),
        },
    }


_TRACE_KEYS = (
    "ts",
    "event",
    "node",
    "phase",
    "model",
    "tokens",
    "duration_ms",
    "status",
    "code",
    "error",
    "attempt",
    "run_id",
    "count",
    "name",
    "spawn_index",
    "parallel",
    "ok",
    "failed",
    "key",
)


def _price_rows(prices: Any) -> list[tuple[str, dict[str, float]]]:
    try:
        table = prices.to_dict()
        return [
            (str(name), {k: A.as_float(v) for k, v in row.items()})
            for name, row in table.items()
        ]
    except Exception:
        return [("GigaChat", {"prompt_per_million": 0.5, "completion_per_million": 1.5})]


def _missing_note(store: UiStore) -> list[str]:
    """Human-readable reasons a snapshot looks empty."""
    notes: list[str] = []
    if not store.db_present:
        notes.append(f"metrics database not found at {store.db_path}")
    if not store.trace_present:
        notes.append(f"trace file not found at {store.trace_path}")
    return notes


def build_series(store: UiStore, *, limit: int = 500) -> dict[str, list[dict[str, Any]]]:
    """Sparkline data: per-run token totals and cache hit/miss over time."""
    metrics = {
        "tokens": "core.router.tokens",
        "cache_hit": "core.cache.hit",
        "cache_miss": "core.cache.miss",
        "latency_ms": "core.router.latency_ms",
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for key, metric in metrics.items():
        points = store.metric_points(metric=metric, limit=limit)
        out[key] = [{"ts": p.get("ts"), "value": A.as_float(p.get("value"))} for p in points]
    return out


__all__ = [
    "NODE_ORDER",
    "SNAPSHOT_VERSION",
    "STEP_EVENTS",
    "build_series",
    "build_snapshot",
]

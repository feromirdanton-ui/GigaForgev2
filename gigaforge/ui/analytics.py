"""Pure analytics helpers for the GigaForge V2 UI.

No I/O lives here: every function takes facts (metric points, trace events,
node rows) and returns numbers.  That keeps the KPI maths unit-testable without
a database and lets the TUI, the web panel and the chat interface share exactly
one definition of "cache hit rate" instead of three slightly different ones.

Conventions that matter:

* **Rate denominators are explicit.**  ``reject_rate`` is rejections over
  *proposals* (a proposal is accepted or rejected — adding rejections to the
  denominator would double-count).  ``cache_hit_rate`` is hits over
  hits+misses. A rate with a zero denominator is ``0.0``, never ``None``, so a
  renderer never prints ``NaN``.
* **Money stays integral until the end.**  Costs are accumulated as integer
  micro-USD and only divided for display, exactly like ``core.cost``, so a long
  run cannot drift.
* **Percentiles interpolate.**  ``latency_p95`` over a handful of samples would
  otherwise be pure noise.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

MICROS_PER_USD = 1_000_000

#: Fallback price table used when ``gigaforge.core.cost`` cannot be imported
#: (e.g. a UI-only deployment).  Mirrors ``core.cost.DEFAULT_PRICES``.
_FALLBACK_PRICES: dict[str, tuple[float, float]] = {
    "GigaChat": (0.50, 1.50),
    "GigaChat-Pro": (2.50, 10.00),
    "GigaChat-Max": (5.00, 20.00),
}


# ── generic numeric helpers ────────────────────────────────────────────────
def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def rate(numerator: float, denominator: float) -> float:
    """``numerator / denominator`` as a 4-decimal ratio, 0.0 when undefined."""
    denom = as_float(denominator)
    if denom <= 0:
        return 0.0
    return round(as_float(numerator) / denom, 4)


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile; ``None`` for an empty sample."""
    data = sorted(as_float(v) for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    q = min(max(as_float(q), 0.0), 100.0) / 100.0
    position = q * (len(data) - 1)
    lower = int(position)
    upper = min(lower + 1, len(data) - 1)
    weight = position - lower
    return round(data[lower] * (1 - weight) + data[upper] * weight, 3)


def human_bytes(count: float) -> str:
    size = as_float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def human_usd(micros: float) -> str:
    """Micro-USD to a compact dollar string — never a bare ``0.0`` for tiny costs."""
    value = as_float(micros) / MICROS_PER_USD
    if value == 0:
        return "$0.000000"
    if value < 0.01:
        return f"${value:.6f}"
    return f"${value:,.4f}"


def human_duration(ms: float) -> str:
    value = as_float(ms)
    if value < 1000:
        return f"{value:.0f} ms"
    if value < 60_000:
        return f"{value / 1000:.2f} s"
    minutes, seconds = divmod(value / 1000.0, 60.0)
    return f"{int(minutes)}m {seconds:.1f}s"


# ── pricing ────────────────────────────────────────────────────────────────
def price_table() -> Any:
    """The core price table when importable, else a compatible stand-in."""
    try:  # pragma: no cover - import depends on the deployment
        from gigaforge.core.cost import PriceTable

        return PriceTable()
    except Exception:
        return _FallbackPrices()


class _FallbackPrices:
    """Minimal ``PriceTable``-shaped object used when core is unavailable."""

    def price_for(self, model: str) -> Any:
        prompt, completion = _FALLBACK_PRICES.get(
            model, _FALLBACK_PRICES.get("GigaChat", (0.0, 0.0))
        )
        return _FallbackPrice(model or "<unknown>", prompt, completion)

    def cost_micros(self, model: str, prompt_tokens: int, completion_tokens: int) -> int:
        return self.price_for(model).cost_micros(prompt_tokens, completion_tokens)


class _FallbackPrice:
    def __init__(self, model: str, prompt_per_million: float, completion_per_million: float) -> None:
        self.model = model
        self.prompt_per_million = prompt_per_million
        self.completion_per_million = completion_per_million

    def cost_micros(self, prompt_tokens: int, completion_tokens: int) -> int:
        prompt = as_int(prompt_tokens) * self.prompt_per_million
        completion = as_int(completion_tokens) * self.completion_per_million
        return int(round(prompt + completion))


def cost_micros(
    prices: Any,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> int:
    try:
        return int(prices.cost_micros(model, as_int(prompt_tokens), as_int(completion_tokens)))
    except Exception:
        return int(_FallbackPrices().cost_micros(model, prompt_tokens, completion_tokens))


# ── metric-point aggregation ───────────────────────────────────────────────
def group_points(
    points: Iterable[Mapping[str, Any]],
    *,
    by: str = "phase",
) -> dict[str, dict[str, float]]:
    """Sum metric points by a label (``phase`` / ``model`` / ``source``).

    Returns ``{label_value: {"value": sum, "count": n}}`` so a caller can show
    both the magnitude and how many samples produced it.
    """
    out: dict[str, dict[str, float]] = {}
    for point in points:
        labels = point.get("labels") or {}
        if isinstance(labels, str):
            continue
        key = str(labels.get(by) or "-")
        bucket = out.setdefault(key, {"value": 0.0, "count": 0.0})
        bucket["value"] += as_float(point.get("value"))
        bucket["count"] += 1.0
    return out


def series_by_node(points: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Sum of a metric's points per ``node`` column."""
    out: dict[str, float] = {}
    for point in points:
        node = str(point.get("node") or "-")
        out[node] = out.get(node, 0.0) + as_float(point.get("value"))
    return out


def timeline(node_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Turn ``node_runs`` rows into an ordered, display-ready step timeline.

    A node that recorded several attempts keeps the last outcome but sums its
    durations, so the timeline shows the cost of the retries rather than hiding
    them.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in node_rows:
        node = str(row.get("node") or "?")
        if node not in merged:
            order.append(node)
            merged[node] = {
                "node": node,
                "status": str(row.get("status") or "unknown"),
                "attempts": 0,
                "duration_ms": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
                "error_code": "",
                "error_msg": "",
                "started_at": str(row.get("started_at") or ""),
            }
        entry = merged[node]
        entry["attempts"] += max(1, as_int(row.get("attempt"), 1))
        entry["duration_ms"] += as_float(row.get("duration_ms"))
        entry["tokens_in"] += as_int(row.get("tokens_in"))
        entry["tokens_out"] += as_int(row.get("tokens_out"))
        status = str(row.get("status") or "")
        if status:
            entry["status"] = status
        error_code = str(row.get("error_code") or "")
        error_msg = str(row.get("error_msg") or "")
        if error_code or error_msg:
            entry["error_code"] = error_code
            entry["error_msg"] = error_msg
    steps = [merged[name] for name in order]
    for index, step in enumerate(steps):
        step["index"] = index
        step["duration_ms"] = round(step["duration_ms"], 3)
    return steps


def trace_timeline(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild the step timeline from ``dag.*`` trace events alone.

    Used when the DAG journal tables are absent (metrics-only deployments) — the
    trace is the same information in a stream instead of a table.
    """
    steps: list[dict[str, Any]] = []
    open_steps: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = str(event.get("event") or "")
        node = str(event.get("node") or "")
        if not node:
            continue
        if kind == "dag.node_start":
            open_steps[node] = {
                "node": node,
                "status": "running",
                "attempts": 1,
                "duration_ms": 0.0,
                "tokens_in": as_int(event.get("tokens_in")),
                "tokens_out": as_int(event.get("tokens_out")),
                "error_code": "",
                "error_msg": "",
                "started_at": str(event.get("ts") or ""),
            }
        elif kind in {"dag.node_finish", "dag.node_error", "dag.node_failed"}:
            step = open_steps.pop(node, None)
            if step is None:
                step = {
                    "node": node,
                    "status": "unknown",
                    "attempts": 1,
                    "duration_ms": 0.0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "error_code": "",
                    "error_msg": "",
                    "started_at": str(event.get("ts") or ""),
                }
            step["status"] = {
                "dag.node_finish": "succeeded",
                "dag.node_error": "failed",
                "dag.node_failed": "failed",
            }.get(kind, step["status"])
            step["duration_ms"] = round(as_float(event.get("duration_ms"), step["duration_ms"]), 3)
            if kind in {"dag.node_error", "dag.node_failed"}:
                step["error_code"] = str(event.get("code") or "")
                step["error_msg"] = str(event.get("error") or "")
            steps.append(step)
    steps.extend(open_steps.values())
    for index, step in enumerate(steps):
        step["index"] = index
    return steps


def status_counts(steps: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for step in steps:
        status = str(step.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def progress(steps: Sequence[Mapping[str, Any]], *, expected: Sequence[str] = ()) -> dict[str, Any]:
    """Progress as a fraction of *expected* nodes — the pipeline's own order.

    Progress measured against the nodes seen so far is always 100% and therefore
    useless; the denominator has to be the pipeline's declared node order.
    """
    total = len(expected) if expected else max(len(steps), 1)
    done = sum(1 for s in steps if str(s.get("status")) in {"succeeded", "skipped"})
    failed = sum(1 for s in steps if str(s.get("status")) in {"failed", "timeout"})
    running = sum(1 for s in steps if str(s.get("status")) == "running")
    return {
        "done": done,
        "failed": failed,
        "running": running,
        "total": total,
        "fraction": round(min(done, total) / total, 4) if total else 0.0,
        "percent": round(100.0 * min(done, total) / total, 1) if total else 0.0,
    }


def diff_stats(diff_text: str) -> dict[str, Any]:
    """Counts a diff viewer shows: files, hunks, added/removed lines.

    A diff string ends with a newline, so splitting yields a trailing empty
    element that must be dropped — otherwise every diff reports one phantom
    line and the +/- totals never reconcile.
    """
    text = diff_text or ""
    if not text.strip():
        return {"chars": 0, "files": [], "hunks": 0, "added": 0, "removed": 0, "lines": 0}
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[str] = []
    added = removed = hunks = 0
    for line in lines:
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.append(parts[3][2:] if parts[3].startswith("b/") else parts[3])
        elif line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path and path != "/dev/null" and path not in files:
                files.append(path)
        elif line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {
        "chars": len(text),
        "files": files,
        "hunks": hunks,
        "added": added,
        "removed": removed,
        "lines": len(lines),
    }


__all__ = [
    "MICROS_PER_USD",
    "as_float",
    "as_int",
    "cost_micros",
    "diff_stats",
    "group_points",
    "human_bytes",
    "human_duration",
    "human_usd",
    "percentile",
    "price_table",
    "progress",
    "rate",
    "series_by_node",
    "status_counts",
    "timeline",
    "trace_timeline",
]

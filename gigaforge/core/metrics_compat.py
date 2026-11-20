"""Thin adapter over the client layer's metrics primitives.

The client package already ships a dependency-free ``MetricsSink`` with
counters, bounded observation windows, gauges and percentiles.  Reproducing it
here would be a second implementation to keep in sync, so ``core`` re-exports it
under the names the orchestration layer uses.

Keeping the import in one small module (instead of importing
``gigaforge.client.metrics`` all over ``core``) also means the dependency on the
client layer is declared in exactly one place.
"""
from __future__ import annotations

try:  # pragma: no cover - import shape is trivial
    from gigaforge.client.metrics import (  # type: ignore
        CompositeMetrics as CompositeSink,
        InMemoryMetrics as InMemorySink,
        MetricsSink,
        NullMetrics as NullSink,
        percentile,
    )
except Exception:  # pragma: no cover - standalone fallback
    import threading
    import time
    from collections import defaultdict, deque
    from typing import Any, Mapping, Optional, Sequence

    class MetricsSink:  # type: ignore[no-redef]
        def incr(self, name: str, value: float = 1, labels=None) -> None: ...
        def observe(self, name: str, value: float, labels=None) -> None: ...
        def gauge(self, name: str, value: float, labels=None) -> None: ...
        def snapshot(self) -> dict: return {"counters": {}, "observations": {}, "gauges": {}}
        def reset(self) -> None: ...

    class NullSink(MetricsSink):  # type: ignore[no-redef]
        pass

    class InMemorySink(MetricsSink):  # type: ignore[no-redef]
        def __init__(self, *, window: int = 512) -> None:
            self._lock = threading.Lock()
            self._counters: dict = defaultdict(float)
            self._observations: dict = defaultdict(lambda: deque(maxlen=window))
            self._gauges: dict = {}

        def incr(self, name, value=1, labels=None):
            with self._lock:
                self._counters[(name, _key(labels))] += float(value)

        def observe(self, name, value, labels=None):
            with self._lock:
                self._observations[(name, _key(labels))].append(float(value))

        def gauge(self, name, value, labels=None):
            with self._lock:
                self._gauges[(name, _key(labels))] = float(value)

        def counter(self, name, labels=None):
            with self._lock:
                return self._counters.get((name, _key(labels)), 0.0)

        def values(self, name, labels=None):
            with self._lock:
                return tuple(self._observations.get((name, _key(labels)), ()))

        def snapshot(self):
            with self._lock:
                return {
                    "counters": {_label(k): v for k, v in self._counters.items()},
                    "observations": {_label(k): list(v) for k, v in self._observations.items()},
                    "gauges": {_label(k): v for k, v in self._gauges.items()},
                }

        def reset(self):
            with self._lock:
                self._counters.clear(); self._observations.clear(); self._gauges.clear()

    def _key(labels) -> str:
        if not labels:
            return ""
        return ",".join(f"{k}={labels[k]}" for k in sorted(labels))

    def _label(key) -> str:
        name, suffix = key
        return f"{name}{{{suffix}}}" if suffix else name

    class CompositeSink(MetricsSink):  # type: ignore[no-redef]
        def __init__(self, *sinks) -> None:
            self._sinks = [s for s in sinks if s is not None]

        def incr(self, name, value=1, labels=None):
            for s in self._sinks:
                s.incr(name, value, labels)

        def observe(self, name, value, labels=None):
            for s in self._sinks:
                s.observe(name, value, labels)

        def gauge(self, name, value, labels=None):
            for s in self._sinks:
                s.gauge(name, value, labels)

        def snapshot(self):
            merged = {"counters": {}, "observations": {}, "gauges": {}}
            for s in self._sinks:
                snap = s.snapshot() or {}
                for section in merged:
                    merged[section].update(snap.get(section) or {})
            return merged

        def reset(self):
            for s in self._sinks:
                s.reset()

    def percentile(values: Sequence[float], p: float) -> Optional[float]:
        data = sorted(values)
        if not data:
            return None
        if len(data) == 1:
            return float(data[0])
        rank = max(0, min(len(data) - 1, int(round((p / 100.0) * (len(data) - 1)))))
        return float(data[rank])


class SqliteAwareSink:
    """Marker mixin documenting that a sink accepts ``run_id`` / ``node`` kwargs.

    :class:`gigaforge.core.metrics.SqliteMetricsSink` uses it so callers can
    dispatch on the capability with ``isinstance`` instead of ``hasattr`` probing
    for keyword-only parameters.
    """


__all__ = ["CompositeSink", "InMemorySink", "NullSink", "MetricsSink", "percentile", "SqliteAwareSink"]

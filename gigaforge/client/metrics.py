"""Dependency-free metrics primitives for the client layer.

The architecture puts counters and histograms in the SQLite ``metrics_points``
table, so the client only needs a tiny sink interface: something the client can
call, plus a snapshot for tests and the ``doctor`` command. No exporter, no
agent, no globals.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterator, Mapping, Optional, Sequence

Labels = Optional[Mapping[str, Any]]


def label_key(name: str, labels: Labels) -> str:
    if not labels:
        return name
    parts = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{parts}}}"


class MetricsSink:
    """No-op interface. Anything with these four methods is a valid sink."""

    def incr(self, name: str, value: float = 1, labels: Labels = None) -> None:
        return None

    def observe(self, name: str, value: float, labels: Labels = None) -> None:
        return None

    def gauge(self, name: str, value: float, labels: Labels = None) -> None:
        return None

    def snapshot(self) -> Dict[str, Any]:
        return {"counters": {}, "observations": {}, "gauges": {}}

    def reset(self) -> None:
        return None


class NullMetrics(MetricsSink):
    """Explicitly discards everything (used when metrics are disabled)."""


class InMemoryMetrics(MetricsSink):
    """Thread-safe counters, bounded observation windows and gauges."""

    def __init__(self, *, window: int = 512) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = defaultdict(float)
        self._observations: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self._gauges: Dict[str, float] = {}

    def incr(self, name: str, value: float = 1, labels: Labels = None) -> None:
        with self._lock:
            self._counters[label_key(name, labels)] += float(value)

    def observe(self, name: str, value: float, labels: Labels = None) -> None:
        with self._lock:
            self._observations[label_key(name, labels)].append(float(value))

    def gauge(self, name: str, value: float, labels: Labels = None) -> None:
        with self._lock:
            self._gauges[label_key(name, labels)] = float(value)

    @contextmanager
    def timer(self, name: str, labels: Labels = None) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - start) * 1000.0, labels)

    def counter(self, name: str, labels: Labels = None) -> float:
        with self._lock:
            return self._counters.get(label_key(name, labels), 0.0)

    def values(self, name: str, labels: Labels = None) -> Sequence[float]:
        with self._lock:
            return tuple(self._observations.get(label_key(name, labels), ()))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "observations": {
                    k: {
                        "count": len(v),
                        "min": min(v) if v else None,
                        "max": max(v) if v else None,
                        "mean": (sum(v) / len(v)) if v else None,
                        "p95": percentile(v, 95) if v else None,
                        "last": v[-1] if v else None,
                    }
                    for k, v in self._observations.items()
                },
                "gauges": dict(self._gauges),
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._observations.clear()
            self._gauges.clear()


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """Nearest-rank percentile - good enough for latency dashboards."""
    data = sorted(values)
    if not data:
        return None
    if len(data) == 1:
        return float(data[0])
    rank = max(0, min(len(data) - 1, int(round((p / 100.0) * (len(data) - 1)))))
    return float(data[rank])


class CompositeMetrics(MetricsSink):
    """Fan-out sink: e.g. in-memory for tests plus a SQLite writer in production."""

    def __init__(self, *sinks: MetricsSink) -> None:
        self._sinks = [s for s in sinks if s is not None]

    def incr(self, name: str, value: float = 1, labels: Labels = None,
             **scope: Any) -> None:
        self._fan("incr", name, value, labels, scope)

    def observe(self, name: str, value: float, labels: Labels = None,
                **scope: Any) -> None:
        self._fan("observe", name, value, labels, scope)

    def gauge(self, name: str, value: float, labels: Labels = None,
              **scope: Any) -> None:
        self._fan("gauge", name, value, labels, scope)

    def _fan(self, kind: str, name: str, value: float, labels: Labels,
             scope: Dict[str, Any]) -> None:
        """Fan out, forwarding the run scope only to sinks that accept it.

        A composite mixes sinks from different layers — the in-memory one has no
        notion of a run, the SQLite writer stores the scope on the row — so the
        forward is capability-detected instead of assumed.
        """
        for sink in self._sinks:
            method = getattr(sink, kind)
            if scope:
                try:
                    method(name, value, labels, **scope)
                    continue
                except TypeError:
                    pass
            method(name, value, labels)

    def snapshot(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {"counters": {}, "observations": {}, "gauges": {}}
        for sink in self._sinks:
            snap = sink.snapshot() or {}
            for section in ("counters", "observations", "gauges"):
                merged[section].update(snap.get(section) or {})
        return merged

    def reset(self) -> None:
        for sink in self._sinks:
            sink.reset()


__all__ = [
    "CompositeMetrics",
    "InMemoryMetrics",
    "MetricsSink",
    "NullMetrics",
    "percentile",
]

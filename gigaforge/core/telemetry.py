"""Run telemetry aggregation for the V2 UI (V2).

V1 wrote metrics points and a JSONL trace, but nothing turned them into the
single object a dashboard needs.  V2 adds :class:`RunTelemetry`: one pass over a
run's trace + metrics that produces a UI-ready payload with the numbers the
front-end actually renders:

* token totals and **cost** (via :mod:`gigaforge.core.cost`);
* **cache hit rate** and **reject rate**;
* per-phase and per-node breakdowns;
* the ordered **step timeline** (for a progress view);
* the **diff** and its size (for a diff viewer).

Everything is derived from data the pipeline already emits, so the UI never has
to reach into pipeline internals — it consumes this one JSON shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .cost import MICROS_PER_USD, PriceTable


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class StepView:
    """One row of the pipeline progress timeline."""

    index: int
    node: str
    status: str
    duration_ms: float = 0.0
    attempts: int = 1
    error_code: str = ""
    error_msg: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "node": self.node,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 3),
            "attempts": self.attempts,
            "error_code": self.error_code,
            "error_msg": self.error_msg,
        }


@dataclass
class RunTelemetry:
    """Aggregated, UI-ready view of a single run."""

    run_id: str = ""
    goal: str = ""
    status: str = "unknown"
    accepted: bool = False
    duration_ms: float = 0.0

    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    cost_micros: int = 0

    cache_hits: int = 0
    cache_misses: int = 0
    cache_writes: int = 0
    proposals: int = 0
    rejections: int = 0

    diff_chars: int = 0
    changed_files: list[str] = field(default_factory=list)
    steps: list[StepView] = field(default_factory=list)
    phase_usages: dict[str, dict[str, Any]] = field(default_factory=dict)
    cost_by_phase_micros: dict[str, int] = field(default_factory=dict)
    cost_by_model_micros: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    # ── derived rates ─────────────────────────────────────────────────────────
    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return (self.cache_hits / total) if total else 0.0

    @property
    def reject_rate(self) -> float:
        # Rejections are a subset of proposals: a proposal is either accepted or
        # rejected, so the denominator is the number of proposals, not
        # proposals + rejections (which would double-count).
        total = max(self.proposals, self.rejections)
        return (self.rejections / total) if total else 0.0

    @property
    def cost_usd(self) -> float:
        return self.cost_micros / MICROS_PER_USD

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status,
            "accepted": self.accepted,
            "duration_ms": round(self.duration_ms, 3),
            "tokens": {
                "total": self.tokens,
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "calls": self.calls,
            },
            "cost": {
                "micros": self.cost_micros,
                "usd": round(self.cost_usd, 6),
                "by_phase_micros": dict(sorted(self.cost_by_phase_micros.items())),
                "by_model_micros": dict(sorted(self.cost_by_model_micros.items())),
            },
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "writes": self.cache_writes,
                "hit_rate": round(self.cache_hit_rate, 4),
            },
            "repair": {
                "proposals": self.proposals,
                "rejections": self.rejections,
                "reject_rate": round(self.reject_rate, 4),
            },
            "diff": {
                "chars": self.diff_chars,
                "changed_files": list(self.changed_files),
            },
            "phase_usages": {k: dict(v) for k, v in self.phase_usages.items()},
            "steps": [s.to_dict() for s in self.steps],
            "errors": list(self.errors),
        }


class TelemetryBuilder:
    """Builds a :class:`RunTelemetry` from a pipeline result and/or a trace."""

    def __init__(self, *, prices: Optional[PriceTable] = None) -> None:
        self.prices = prices or PriceTable()

    def from_result(self, result: Any) -> RunTelemetry:
        """Aggregate a ``PipelineResult`` (or any object with the same shape)."""
        telemetry = RunTelemetry(
            run_id=str(getattr(result, "run_id", "") or ""),
            goal=str(getattr(result, "goal", "") or ""),
            status=str(getattr(result, "status", "unknown") or "unknown"),
            accepted=bool(getattr(result, "accepted", False)),
            duration_ms=_as_float(getattr(result, "duration_ms", 0.0)),
            tokens=_as_int(getattr(result, "tokens", 0)),
            calls=_as_int(getattr(result, "calls", 0)),
            diff_chars=len(str(getattr(result, "final_diff", "") or "")),
            changed_files=list(getattr(result, "changed_files", []) or []),
        )
        telemetry.phase_usages = {
            str(k): dict(v) for k, v in (getattr(result, "phase_usages", {}) or {}).items()
        }
        for phase, usage in telemetry.phase_usages.items():
            prompt = _as_int(usage.get("prompt_tokens", 0))
            completion = _as_int(usage.get("completion_tokens", 0))
            telemetry.prompt_tokens += prompt
            telemetry.completion_tokens += completion
            model = str(usage.get("model", "") or "")
            micros = self.prices.cost_micros(model, prompt, completion)
            telemetry.cost_micros += micros
            telemetry.cost_by_phase_micros[phase] = (
                telemetry.cost_by_phase_micros.get(phase, 0) + micros
            )
            key = model or "<unknown>"
            telemetry.cost_by_model_micros[key] = (
                telemetry.cost_by_model_micros.get(key, 0) + micros
            )

        for index, node in enumerate(getattr(result, "nodes", []) or []):
            if not isinstance(node, Mapping):
                continue
            name = str(node.get("node", "") or "")
            if name == "fixloop":
                telemetry.proposals += _as_int(node.get("proposals", 0))
                telemetry.rejections += max(
                    0, _as_int(node.get("proposals", 0)) - (1 if node.get("accepted") else 0)
                )
                continue
            telemetry.steps.append(
                StepView(
                    index=index,
                    node=name,
                    status=str(node.get("status", "") or ""),
                    duration_ms=_as_float(node.get("duration_ms", 0.0)),
                    attempts=_as_int(node.get("attempts", 1), 1),
                    error_code=str(node.get("error_code", "") or ""),
                    error_msg=str(node.get("error_msg", "") or ""),
                )
            )
            if node.get("error_msg"):
                telemetry.errors.append(f"{name}: {node['error_msg']}")

        if getattr(result, "cache_hit", False):
            telemetry.cache_hits += 1
        else:
            telemetry.cache_misses += 1
        if getattr(result, "error", ""):
            telemetry.errors.append(str(result.error))
        return telemetry

    def from_trace(self, events: Sequence[Mapping[str, Any]]) -> RunTelemetry:
        """Aggregate a JSONL trace (list of event dicts)."""
        telemetry = RunTelemetry()
        for event in events:
            if not isinstance(event, Mapping):
                continue
            kind = str(event.get("event", "") or "")
            if kind == "dag.start":
                telemetry.run_id = str(event.get("run_id", "") or telemetry.run_id)
            elif kind == "dag.finish":
                telemetry.status = str(event.get("status", telemetry.status) or telemetry.status)
                telemetry.duration_ms = _as_float(event.get("duration_ms", telemetry.duration_ms))
            elif kind == "router.call":
                tokens = _as_int(event.get("tokens", 0))
                telemetry.tokens += tokens
                telemetry.calls += 1
                model = str(event.get("model", "") or "")
                phase = str(event.get("phase", "") or "")
                micros = self.prices.cost_micros(model, 0, tokens)
                telemetry.cost_micros += micros
                if phase:
                    telemetry.cost_by_phase_micros[phase] = (
                        telemetry.cost_by_phase_micros.get(phase, 0) + micros
                    )
                key = model or "<unknown>"
                telemetry.cost_by_model_micros[key] = (
                    telemetry.cost_by_model_micros.get(key, 0) + micros
                )
            elif kind == "cache.hit":
                telemetry.cache_hits += 1
            elif kind == "cache.miss":
                telemetry.cache_misses += 1
            elif kind == "cache.write":
                telemetry.cache_writes += 1
            elif kind == "fixloop.rejected":
                telemetry.rejections += 1
            elif kind == "fixloop.accepted":
                telemetry.proposals += 1
            elif kind == "pipeline.settle":
                telemetry.diff_chars = _as_int(event.get("diff_chars", telemetry.diff_chars))
                telemetry.accepted = bool(event.get("accepted", telemetry.accepted))
            elif kind in {"dag.node_failed", "router.error"}:
                telemetry.errors.append(str(event.get("error", kind))[:300])
        return telemetry


def build_telemetry(
    result: Any = None,
    *,
    events: Optional[Sequence[Mapping[str, Any]]] = None,
    prices: Optional[PriceTable] = None,
) -> RunTelemetry:
    """Convenience: prefer the result, fall back to the trace."""
    builder = TelemetryBuilder(prices=prices)
    if result is not None:
        return builder.from_result(result)
    return builder.from_trace(events or [])


__all__ = ["RunTelemetry", "StepView", "TelemetryBuilder", "build_telemetry"]

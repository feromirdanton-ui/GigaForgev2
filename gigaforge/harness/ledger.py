"""Run ledger — one structured record of what a harness run actually did.

The tracer answers "what happened, in order".  The ledger answers "what did it
cost, and did it work" — the questions a benchmark, a CI gate and a human
reviewer all ask, and which today require grepping three JSONL files and a
SQLite table.

A ledger is deliberately a plain dataclass with a ``to_dict``: it is written to
disk as JSON, compared in tests, and printed in reports, so it must not depend
on the tracer's file layout or on a database being present.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["RunLedger", "StepRecord"]


@dataclass
class StepRecord:
    """One step of a run: a model call, a tool call, or a decision."""

    index: int
    kind: str  # "llm" | "tool" | "decision" | "compaction"
    name: str = ""
    ok: bool = True
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "ok": self.ok,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
            "detail": dict(self.detail),
        }


@dataclass
class RunLedger:
    """The accounting record of one harness run."""

    run_id: str
    goal: str = ""
    engine: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    finish_reason: str = ""
    steps: list[StepRecord] = field(default_factory=list)
    compactions: int = 0
    retries: int = 0
    rate_limit_waits: float = 0.0
    error: str = ""
    #: Authoritative token totals from the shared budget tracker.  ``None``
    #: means "sum the steps"; a number means the tracker knows better, because
    #: the runner commits real usage there after every call.
    tokens_in_override: int | None = None
    tokens_out_override: int | None = None
    #: Did the plan the episode produced actually reach the executor?  This is
    #: the field that makes N1 visible in a report instead of only in a review:
    #: a run that pays for exploration and discards it looks identical to a
    #: successful one in every other column.
    plan_used: bool = False
    #: How many of the plan's steps the executor actually ran.
    plan_steps_executed: int = 0
    #: True when the plan had to be rebuilt from the episode's observations
    #: because the model's own finish payload was unusable.
    salvaged: bool = False
    #: Cost of the run in integer micro-USD, accumulated from the price table.
    #: Tokens alone do not answer the question a budget owner asks; a run that
    #: is cheap in tokens on an expensive model is not a cheap run.
    cost_micros: int = 0
    #: Cache accounting.  ``cache_hit_rate`` is the fraction of lookups that
    #: were served from cache — the number that says whether the cache is
    #: earning its keep, which a bare ``cache_hit: bool`` cannot.
    cache_hits: int = 0
    cache_lookups: int = 0
    #: Per-phase cost, so a report can say WHERE the money went.
    phase_cost_micros: dict[str, int] = field(default_factory=dict)

    def note_cost(self, *, model: str, phase: str, tokens_in: int, tokens_out: int,
                  prices: Any = None) -> int:
        """Add one call's cost; returns the micro-USD it added.

        ``prices`` is a :class:`gigaforge.core.cost.PriceTable`; when it is
        omitted the default table is used.  An unknown model costs zero — the
        tokens are still counted, but a price is never invented.
        """
        if prices is None:
            from gigaforge.core.cost import PriceTable

            prices = PriceTable()
        micros = int(prices.cost_micros(model, tokens_in, tokens_out))
        self.cost_micros += micros
        self.phase_cost_micros[phase] = self.phase_cost_micros.get(phase, 0) + micros
        return micros

    def note_cache(self, *, hit: bool) -> None:
        """Record one cache lookup and whether it was served from cache."""
        self.cache_lookups += 1
        if hit:
            self.cache_hits += 1

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of cache lookups served from cache (0.0 when none)."""
        if self.cache_lookups <= 0:
            return 0.0
        return self.cache_hits / self.cache_lookups

    @property
    def cost_usd(self) -> float:
        return self.cost_micros / 1_000_000

    @property
    def cost_per_task_usd(self) -> float:
        """Cost of this run, which IS one task.  Named for the report column."""
        return self.cost_usd

    def note_plan(self, *, used: bool, steps: int = 0, salvaged: bool = False) -> None:
        """Record what happened to the plan, for the report and the benchmark."""
        self.plan_used = bool(used)
        self.plan_steps_executed = int(steps)
        self.salvaged = bool(salvaged)

    # -- recording -----------------------------------------------------------
    def add(self, record: StepRecord) -> StepRecord:
        record.index = len(self.steps)
        self.steps.append(record)
        return record

    def llm(
        self,
        *,
        name: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int = 0,
        ok: bool = True,
        **detail: Any,
    ) -> StepRecord:
        return self.add(
            StepRecord(
                index=0,
                kind="llm",
                name=name,
                ok=ok,
                tokens_in=int(tokens_in),
                tokens_out=int(tokens_out),
                latency_ms=int(latency_ms),
                detail=detail,
            )
        )

    def tool(self, *, name: str, ok: bool = True, **detail: Any) -> StepRecord:
        return self.add(StepRecord(index=0, kind="tool", name=name, ok=ok, detail=detail))

    def decision(self, *, name: str, **detail: Any) -> StepRecord:
        return self.add(StepRecord(index=0, kind="decision", name=name, detail=detail))

    def compaction(self, *, before_tokens: int, after_tokens: int, dropped: int) -> StepRecord:
        self.compactions += 1
        return self.add(
            StepRecord(
                index=0,
                kind="compaction",
                name="compact_trajectory",
                detail={
                    "before_tokens": int(before_tokens),
                    "after_tokens": int(after_tokens),
                    "dropped_messages": int(dropped),
                },
            )
        )

    def finish(self, reason: str) -> None:
        self.finish_reason = reason
        self.finished_at = time.time()

    # -- derived -------------------------------------------------------------
    @property
    def duration_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def tokens_in(self) -> int:
        if self.tokens_in_override is not None:
            return int(self.tokens_in_override)
        return sum(step.tokens_in for step in self.steps)

    @property
    def tokens_out(self) -> int:
        if self.tokens_out_override is not None:
            return int(self.tokens_out_override)
        return sum(step.tokens_out for step in self.steps)

    @property
    def llm_calls(self) -> int:
        return sum(1 for step in self.steps if step.kind == "llm")

    @property
    def tool_calls(self) -> int:
        return sum(1 for step in self.steps if step.kind == "tool")

    @property
    def failures(self) -> int:
        return sum(1 for step in self.steps if not step.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "engine": self.engine,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 3),
            "finish_reason": self.finish_reason,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "failures": self.failures,
            "compactions": self.compactions,
            "retries": self.retries,
            "rate_limit_waits": round(self.rate_limit_waits, 3),
            "error": self.error,
            "plan_used": self.plan_used,
            "plan_steps_executed": self.plan_steps_executed,
            "salvaged": self.salvaged,
            "cost_micros": self.cost_micros,
            "cost_usd": round(self.cost_usd, 6),
            "cost_per_task_usd": round(self.cost_per_task_usd, 6),
            "phase_cost_micros": dict(self.phase_cost_micros),
            "cache_hits": self.cache_hits,
            "cache_lookups": self.cache_lookups,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "steps": [step.to_dict() for step in self.steps],
        }

    def write(self, path: Path | str) -> Path:
        """Persist the ledger as JSON; returns the path written."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def summary_line(self) -> str:
        """One line for a CI log or a report table."""
        return (
            f"{self.run_id}: {self.finish_reason or 'running'} | "
            f"{self.llm_calls} llm / {self.tool_calls} tool | "
            f"{self.tokens_in}+{self.tokens_out} tok | "
            f"${self.cost_usd:.4f} | "
            f"cache {self.cache_hit_rate:.0%} | "
            f"{self.compactions} compact | "
            f"plan {'used' if self.plan_used else 'unused'}"
            f"{f' (salvaged)' if self.salvaged else ''} | "
            f"{self.duration_s:.1f}s"
        )

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
            f"{self.compactions} compact | {self.duration_s:.1f}s"
        )

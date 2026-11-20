"""Tracer — JSONL trace of EVERY task step (L0, LLM, tool, cache, decision).

Spec: "JSONL-trace of every task step (L0, cheap) for debugging".

Each step is ONE JSON line appended to `settings.trace_dir / f"{task_id}.jsonl"`.
Each line shape:
    {"ts","task_id","step","kind","detail","tokens","latency_ms"}

`kind` is one of: "l0" | "llm" | "tool" | "cache" | "decision".

L0 steps are cheap (deterministic, no LLM) but recorded for full auditability.
0 LLM calls in this module — pure file I/O.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gigaforge.config import Settings, get_settings

__all__ = ["Tracer"]

_VALID_KINDS = frozenset({"l0", "llm", "tool", "cache", "decision"})


class Tracer:
    """Append-only JSONL tracer per task_id.

    Construction:
        tracer = Tracer()                   # uses get_settings().trace_dir
        tracer = Tracer(trace_dir=...)      # explicit dir (tests)
    """

    def __init__(
        self,
        *,
        trace_dir: Path | str | None = None,
        settings: Settings | None = None,
    ) -> None:
        s = settings or get_settings()
        self._trace_dir: Path = Path(trace_dir) if trace_dir is not None else Path(s.trace_dir)
        self._trace_dir.mkdir(parents=True, exist_ok=True)
        # One lock per process is enough — JSONL appends are line-atomic on POSIX
        # only when small enough; we use a lock to be safe under concurrency.
        self._lock = threading.Lock()

    def _path(self, task_id: str) -> Path:
        # Sanitize task_id so a caller can't escape trace_dir. Drop `.` too —
        # that prevents `..` (parent-dir) traversal entirely.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)
        if not safe:
            safe = "anonymous"
        # LOW-07: clamp to 200 chars so the resulting filename always fits
        # under the typical POSIX filesystem NAME_MAX of 255 bytes (with room
        # for the ``.jsonl`` suffix and a small safety margin for UTF-8
        # expansion if a caller passes non-ASCII identifiers).
        safe = safe[:200]
        return self._trace_dir / f"{safe}.jsonl"

    def trace(
        self,
        task_id: str,
        step: str,
        kind: str,
        detail: Any = None,
        tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """Append one JSON line. Raises ValueError on invalid `kind`.

        No silent degradation on unknown kind — we raise so callers fix the call site.
        """
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"invalid trace kind {kind!r}; expected one of {sorted(_VALID_KINDS)}"
            )
        record: dict[str, Any] = {
            "ts": time.time(),
            "iso": datetime.now(tz=UTC).isoformat(timespec="microseconds"),
            "task_id": task_id,
            "step": step,
            "kind": kind,
        }
        if detail is not None:
            record["detail"] = detail
        if tokens is not None:
            record["tokens"] = int(tokens)
        if latency_ms is not None:
            record["latency_ms"] = int(latency_ms)

        line = json.dumps(record, ensure_ascii=False, default=_json_default)
        path = self._path(task_id)
        with self._lock:
            # os.O_APPEND ensures concurrent tracers don't interleave lines on POSIX.
            # We open in binary mode, encode, write, and add a newline ourselves.
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)

    def read_trace(self, task_id: str) -> list[dict[str, Any]]:
        """Read all steps for a task. Empty list if no trace file exists."""
        path = self._path(task_id)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._lock:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        # Don't silently swallow — surface the broken line.
                        raise ValueError(
                            f"corrupt JSONL in {path}: {e.msg} (line: {line[:120]!r})"
                        ) from e
        return out

    def clear(self, task_id: str) -> None:
        """Remove a task's trace file. Best-effort: ignores missing."""
        path = self._path(task_id)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _json_default(obj: Any) -> Any:
    """JSON fallback for non-serialisable objects (Path, set, datetime, etc.)."""
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):  # pydantic v2
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return repr(obj)

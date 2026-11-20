"""MemoryRetriever — wraps :class:`EpisodicMemory` to inject a planner prefix.

WHY IT EARNS ITS TOKENS (justification, doctrine-aligned):
    The :class:`EpisodicMemory` is just a SQLite store; the agent layer is what
    turns "we have past outcomes" into "the planner SEES them". This module
    does that translation: it takes the current task string, retrieves top-K
    similar past outcomes (0 LLM calls), and produces a one-line compact
    "previous experience" string that the pipeline can prepend to the
    planner's user message. The cost is one SQLite read + a tiny prefix
    inflation (<64 tokens); the benefit is that the planner's first attempt
    is informed by what worked or failed last time on a similar task — the
    cheapest form of "test-time compute scaling" available.

Doctrine:
- Context is budget. Retrieval is 0 LLM calls. The injected prefix is capped
  to ``_MEMORY_PREFIX_BUDGET`` (64 tokens).
- Diff is contract. Memory does NOT replace the Coder; it only enriches the
  planner's prefix.
- Bounded retrieval. Top-K (default 3, env ``GIGAFORGE_MEMORY_TOP_K``).
- Fail-safe. Any memory-store error is swallowed (memory is a best-effort
  optimisation — never a hard dependency of the pipeline).
"""
from __future__ import annotations

import logging
from typing import Any

from gigaforge.config import Settings, get_settings
from gigaforge.core.memory import (
    DEFAULT_MEMORY_TOP_K,
    EpisodicMemory,
    MemoryHit,
    TaskOutcome,
    format_memory_prefix,
    hash_ngram,
    word_bigrams,
)
from gigaforge.core.trace import Tracer

log = logging.getLogger(__name__)

__all__ = [
    "MemoryRetriever",
    "TaskOutcome",
    "MemoryHit",
    "DEFAULT_MEMORY_TOP_K",
]


def _compute_task_hash(task: str) -> str:
    """SHA-256 of the normalised task string (no salt). Used as PK in the store."""
    import hashlib
    import re

    norm = re.sub(r"\s+", " ", task.strip()).lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


class MemoryRetriever:
    """Retrieves past outcomes + formats them as a planner prefix.

    Construction:
        retriever = MemoryRetriever(memory=EpisodicMemory(...), tracer=...)
        prefix = await retriever.retrieve_prefix(task, task_id="t-1")
        # prefix: "last similar task: failed at tests after 1500 tokens (sim 0.42)"
        # or "" if no hits / memory unavailable.

    The retriever is async-friendly (its ``retrieve_prefix`` is ``async def``
    to match the agent interface, even though the underlying SQLite call is
    synchronous — it's fast enough not to block the event loop in practice).
    """

    def __init__(
        self,
        memory: EpisodicMemory | None = None,
        *,
        tracer: Tracer | None = None,
        settings: Settings | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._s = settings or get_settings()
        self._tracer = tracer
        self._memory = memory  # may be None; built lazily on first use
        # ``enabled`` defaults to True; the pipeline gates the call with the
        # ``--memory`` CLI flag. The instance flag exists so tests can disable
        # without the CLI flag.
        self._enabled = bool(enabled) if enabled is not None else True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _get_memory(self) -> EpisodicMemory | None:
        if not self._enabled:
            return None
        if self._memory is None:
            try:
                self._memory = EpisodicMemory(settings=self._s)
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.warning("memory: could not open episodic memory store: %s", exc)
                return None
        return self._memory

    async def retrieve_prefix(
        self,
        task: str,
        *,
        task_id: str,
        k: int | None = None,
    ) -> str:
        """Retrieve top-K past outcomes for ``task`` and format a prefix.

        Returns ``""`` on any error or when no hits are found. NEVER raises —
        memory is a best-effort optimisation; the pipeline must not crash
        because of a memory-store hiccup.
        """
        mem = self._get_memory()
        if mem is None:
            return ""
        try:
            hits = mem.retrieve(task, k=k)
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("memory: retrieve failed: %s", exc)
            return ""
        if not hits:
            return ""
        prefix = format_memory_prefix(hits)
        if self._tracer is not None:
            self._tracer.trace(
                task_id,
                step="memory_retrieve",
                kind="decision",
                detail={
                    "n_hits": len(hits),
                    "top_score": round(hits[0].score, 4) if hits else 0.0,
                    "top_outcome_success": hits[0].outcome.success if hits else None,
                    "prefix_chars": len(prefix),
                },
            )
        return prefix

    def record(
        self,
        *,
        task: str,
        success: bool,
        tokens: int,
        failed_step: str = "",
        fixloop_iters: int = 0,
        diff: str = "",
        target_function: str = "",
        target_file: str = "",
        task_id: str = "",
    ) -> None:
        """Record a task outcome. Best-effort — never raises.

        Called by the pipeline at the end of a run (success OR failure). The
        ``task_hash`` is computed deterministically (SHA-256 of the normalised
        task string); re-recording the same task overwrites the previous
        outcome.
        """
        mem = self._get_memory()
        if mem is None:
            return
        try:
            outcome = TaskOutcome(
                task=task,
                task_hash=_compute_task_hash(task),
                success=success,
                tokens=int(tokens),
                failed_step=failed_step,
                fixloop_iters=int(fixloop_iters),
                diff=diff,
                target_function=target_function,
                target_file=target_file,
                ngram_hashes={hash_ngram(g) for g in word_bigrams(task)},
            )
            mem.record(outcome)
            if self._tracer is not None and task_id:
                self._tracer.trace(
                    task_id,
                    step="memory_record",
                    kind="decision",
                    detail={
                        "success": success,
                        "tokens": int(tokens),
                        "failed_step": failed_step,
                        "fixloop_iters": int(fixloop_iters),
                        "target_function": target_function,
                    },
                )
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("memory: record failed: %s", exc)

    def close(self) -> None:
        if self._memory is not None:
            try:
                self._memory.close()
            except Exception:  # noqa: BLE001
                pass


# Convenience: expose the helper functions for tests that want to verify the
# n-gram hashing without going through the SQLite store.
_ = Any  # silence unused-import linters

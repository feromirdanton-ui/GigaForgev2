"""Long-term episodic memory — SQLite-backed store of past task outcomes.

WHY IT EARNS ITS TOKENS (justification, doctrine-aligned):
    Without memory, every task starts from scratch: the planner re-discovers
    that "this kind of bug took N tokens + failed at the reviewer step last
    time." With episodic memory, we INJECT one compact line into the planner's
    prefix: ``"last time this kind of task ran, it took 1500 tokens, failed at
    tests step X, and the final diff fixed a regex"``. The cost is ONE
    deterministic SQLite lookup (0 LLM calls) + a tiny prefix inflation (<50
    tokens). The payoff: the planner avoids repeating past mistakes (e.g.,
    "don't try the regex approach that failed last time") and pre-allocates a
    realistic budget. ExpeL/SPRING show that experience accumulation is the
    single biggest driver of agent quality on hard tasks; we get the cheap
    version (no embedding model, no vector DB) by hashing word-bigrams and
    Jaccard-similarity matching against the SQLite row set.

Doctrine:
- Context is budget. Retrieval is 0 LLM calls (deterministic). The injected
  prefix is capped to ``_MEMORY_PREFIX_BUDGET`` tokens (default 64).
- Diff is contract. Memory does NOT replace the Coder; it only enriches the
  planner's prefix with past outcomes.
- No new heavy deps. SQLite + stdlib only (no langchain, no vector DB, no torch).
- Bounded retrieval. Top-K (default 3, env ``GIGAFORGE_MEMORY_TOP_K``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gigaforge.config import Settings, get_settings

__all__ = [
    "EpisodicMemory",
    "TaskOutcome",
    "MemoryHit",
    "word_bigrams",
    "hash_ngram",
    "DEFAULT_MEMORY_TOP_K",
]

# Hard cap on the injected prefix (tokens). Past outcomes must fit here.
_MEMORY_PREFIX_BUDGET = 64

# Default K for top-K retrieval.
DEFAULT_MEMORY_TOP_K = 3

# Regex to extract word tokens (lowercase, alnum+underscore).
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


# --- n-gram hashing (dependency-free similarity) ---------------------------


def word_bigrams(text: str) -> set[str]:
    """Extract word bigrams from ``text`` (lowercased).

    Example: "fix the bug in validate_email" -> {"fix the", "the bug",
    "bug in", "in validate_email"}.

    Used as the unit of Jaccard similarity (the set of bigrams). Words are
    lowercased and stripped of punctuation; numbers are kept (they often
    carry signal in code tasks).
    """
    if not text:
        return set()
    words = _WORD_RE.findall(text.lower())
    if len(words) < 2:
        # Single-word task: fall back to unigrams.
        return set(words)
    return {f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)}


def hash_ngram(ngram: str) -> int:
    """Stable 64-bit hash of an n-gram (SHA-256 truncated).

    ``hash()`` is process-randomised (PYTHONHASHSEED) — unsuitable for storage
    across runs. We use SHA-256 -> int truncation instead (deterministic).
    """
    h = hashlib.sha256(ngram.encode("utf-8")).digest()
    # Take the first 8 bytes as a signed 64-bit int.
    return int.from_bytes(h[:8], byteorder="big", signed=True)


def _serialize_ngram_set(ngram_hashes: set[int]) -> str:
    """Stable JSON encoding of the hash set (sorted list of ints)."""
    return json.dumps(sorted(ngram_hashes))


def _deserialize_ngram_set(s: str) -> set[int]:
    """Inverse of :func:`_serialize_ngram_set`."""
    try:
        return set(json.loads(s))
    except (json.JSONDecodeError, TypeError):
        return set()


def _jaccard(a: set[int], b: set[int]) -> float:
    """Jaccard similarity over two hash sets (0.0 if either is empty)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# --- data models ------------------------------------------------------------


@dataclass
class TaskOutcome:
    """A past task outcome record. Stored in :class:`EpisodicMemory`.

    The ``task`` is the original natural-language task string (normalised
    lowercase + collapsed whitespace on storage). ``success`` is whether the
    pipeline reached the commit step. ``tokens`` is the total LLM token spend
    (in+out). ``failed_step`` is the step that triggered CircuitBreakerOpen
    (or empty on success). ``fixloop_iters`` is the number of fix-loop
    iterations consumed. ``diff`` is the applied diff (success) or empty
    (failure). ``target_function`` / ``target_file`` carry the localizer's
    verdict (so we can match by those too).
    """

    task: str
    task_hash: str  # sha256(normalized_task) — for exact-match dedup
    success: bool
    tokens: int
    failed_step: str = ""
    fixloop_iters: int = 0
    diff: str = ""
    target_function: str = ""
    target_file: str = ""
    ngram_hashes: set[int] = field(default_factory=set)
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "task_hash": self.task_hash,
            "success": self.success,
            "tokens": self.tokens,
            "failed_step": self.failed_step,
            "fixloop_iters": self.fixloop_iters,
            "diff": self.diff,
            "target_function": self.target_function,
            "target_file": self.target_file,
            "ts": self.ts,
        }


@dataclass
class MemoryHit:
    """A retrieval result: a past :class:`TaskOutcome` + its similarity score."""

    outcome: TaskOutcome
    score: float  # Jaccard similarity in [0, 1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "outcome": self.outcome.to_dict(),
        }


# --- the SQLite-backed store -----------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_outcomes (
    task_hash         TEXT PRIMARY KEY,
    task              TEXT,
    success           INTEGER,
    tokens            INTEGER,
    failed_step       TEXT,
    fixloop_iters     INTEGER,
    diff              TEXT,
    target_function   TEXT,
    target_file       TEXT,
    ngram_hashes      TEXT,
    ts                TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcomes_ts      ON task_outcomes(ts);
CREATE INDEX IF NOT EXISTS idx_outcomes_success ON task_outcomes(success);
"""


def _normalize_task(task: str) -> str:
    """Lowercase + collapse whitespace (mirrors :func:`cache.normalize_task`)."""
    return re.sub(r"\s+", " ", task.strip()).lower()


class EpisodicMemory:
    """SQLite-backed episodic memory of past task outcomes.

    Construction:
        mem = EpisodicMemory()                  # uses get_settings().db_path
        mem = EpisodicMemory(db_path=...)       # explicit path (tests)

    0 LLM calls in this module — pure SQLite + n-gram hashing.
    """

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        settings: Settings | None = None,
    ) -> None:
        s = settings or get_settings()
        self._db_path: Path = Path(db_path) if db_path is not None else Path(s.db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- public API ------------------------------------------------------

    def record(self, outcome: TaskOutcome) -> None:
        """Insert or REPLACE an outcome.

        ``task_hash`` is the primary key — re-recording the same task overwrites
        the previous outcome (we keep the LATEST outcome for a given task).
        """
        if not outcome.task_hash:
            raise ValueError("outcome.task_hash must be non-empty")
        # Always refresh the n-gram set + ts if empty.
        if not outcome.ngram_hashes:
            outcome.ngram_hashes = {hash_ngram(g) for g in word_bigrams(outcome.task)}
        if not outcome.ts:
            outcome.ts = datetime.now(tz=UTC).isoformat(timespec="microseconds")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO task_outcomes "
                "(task_hash, task, success, tokens, failed_step, fixloop_iters, "
                " diff, target_function, target_file, ngram_hashes, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    outcome.task_hash,
                    _normalize_task(outcome.task),
                    1 if outcome.success else 0,
                    int(outcome.tokens),
                    outcome.failed_step,
                    int(outcome.fixloop_iters),
                    outcome.diff,
                    outcome.target_function,
                    outcome.target_file,
                    _serialize_ngram_set(outcome.ngram_hashes),
                    outcome.ts,
                ),
            )

    def retrieve(
        self,
        task: str,
        *,
        k: int | None = None,
    ) -> list[MemoryHit]:
        """Return the top-K most similar past outcomes for ``task``.

        Similarity = Jaccard over word-bigram hash sets. Ties broken by
        recency (most recent first). K defaults to env
        ``GIGAFORGE_MEMORY_TOP_K`` (default 3).
        """
        if k is None:
            env_v = os.environ.get("GIGAFORGE_MEMORY_TOP_K", "")
            try:
                k = int(env_v) if env_v else DEFAULT_MEMORY_TOP_K
            except ValueError:
                k = DEFAULT_MEMORY_TOP_K
        k = max(1, k)
        query_hashes = {hash_ngram(g) for g in word_bigrams(task)}
        if not query_hashes:
            return []
        with self._lock:
            cur = self._conn.execute(
                "SELECT task_hash, task, success, tokens, failed_step, "
                "fixloop_iters, diff, target_function, target_file, "
                "ngram_hashes, ts FROM task_outcomes"
            )
            rows = cur.fetchall()
        hits: list[MemoryHit] = []
        for row in rows:
            (
                task_hash,
                task_text,
                success,
                tokens,
                failed_step,
                fixloop_iters,
                diff,
                target_function,
                target_file,
                ngram_hashes_str,
                ts,
            ) = row
            stored_hashes = _deserialize_ngram_set(ngram_hashes_str)
            score = _jaccard(query_hashes, stored_hashes)
            if score <= 0.0:
                continue
            outcome = TaskOutcome(
                task=task_text,
                task_hash=task_hash,
                success=bool(success),
                tokens=int(tokens),
                failed_step=failed_step or "",
                fixloop_iters=int(fixloop_iters),
                diff=diff or "",
                target_function=target_function or "",
                target_file=target_file or "",
                ngram_hashes=stored_hashes,
                ts=ts or "",
            )
            hits.append(MemoryHit(outcome=outcome, score=score))
        # Sort by score DESC; on ties, most-recent (ts DESC) first.
        # ISO-8601 ts sorts lexicographically; to get DESCENDING ts in an
        # ASCENDING sort key, we invert each character's ordinal so a LARGER
        # ts becomes a SMALLER inverted key. Simpler & explicit.
        hits.sort(
            key=lambda h: (-h.score, _ts_inverted_key(h.outcome.ts)),
        )
        return hits[:k]

    def count(self) -> int:
        """Number of stored outcomes (for tests / observability)."""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM task_outcomes")
            return int(cur.fetchone()[0])

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 — best-effort
                pass


def _ts_inverted_key(ts: str) -> str:
    """Return a sort key that orders ts in DESCENDING order under ASC sort.

    ISO-8601 timestamps sort lexicographically ascending (older -> newer).
    To get the MOST-RECENT ts first on a tied primary key, we invert each
    character (chr(255 - ord(c))) so a LARGER ts produces a SMALLER inverted
    key. The empty case (no ts) sorts LAST (oldest).
    """
    if not ts:
        return "\xff" * 100
    return "".join(chr(255 - ord(c)) for c in ts)


# --- a thin adapter that formats hits into a planner prefix ----------------


def format_memory_prefix(hits: list[MemoryHit], *, budget: int = _MEMORY_PREFIX_BUDGET) -> str:
    """Render top-K hits as a compact prefix string for the planner.

    Each hit renders as one bullet:
        ``"last similar task: <success|failed at step X> after N tokens (sim 0.42)"``

    The bullets are joined with " | " and truncated to ``budget`` tokens
    (chars // 4 heuristic). Empty if no hits.
    """
    if not hits:
        return ""
    bullets: list[str] = []
    for h in hits:
        if h.outcome.success:
            status = f"success in {h.outcome.tokens} tokens"
        else:
            step = h.outcome.failed_step or "unknown step"
            status = f"failed at {step} after {h.outcome.tokens} tokens"
        sim = f"{h.score:.2f}"
        bullets.append(
            f"last similar task: {status} (sim {sim})"
        )
    text = " | ".join(bullets)
    # Truncate to budget (chars // 4 = tokens heuristic).
    max_chars = budget * 4
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text

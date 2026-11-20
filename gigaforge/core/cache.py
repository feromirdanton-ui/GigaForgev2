"""Task-level result cache (M4–M5).

The cache key is the heart of this module:

    task_hash = SHA-256( normalized_task + slice_hash + git_sha )

All three components matter, and each fixes a distinct way a naive cache lies:

* ``normalized_task`` — the same request phrased with different indentation or
  spacing must be one entry.
* ``slice_hash`` — the same task against a *different* context slice is a
  different job.  Without this, a wider slice would silently reuse an answer
  computed from a narrower one.
* ``git_sha`` — the same task and slice against a different commit is a
  different job.  This is also the invalidation axis: when HEAD moves, every
  entry keyed to the old commit becomes unreachable, so a stale diff can never
  be replayed onto moved code.

Storage is the project's SQLite file.  The ``cache_entries`` table is created if
absent so the manager works on a fresh database as well as on one the client /
L0 layers already migrated.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from .config import CacheConfig, CoreConfig
from .errors import CacheError
from .state import normalize_whitespace

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
  cache_key  TEXT PRIMARY KEY,
  namespace  TEXT NOT NULL,
  value      BLOB NOT NULL,
  value_type TEXT NOT NULL DEFAULT 'json',
  model      TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  hits       INTEGER NOT NULL DEFAULT 0,
  bytes      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_cache_ns_exp ON cache_entries(namespace, expires_at);
CREATE INDEX IF NOT EXISTS ix_cache_meta     ON cache_entries(namespace, model);
CREATE TABLE IF NOT EXISTS cache_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS solutions (
  task_hash   TEXT PRIMARY KEY,
  task        TEXT,
  diff        TEXT,
  git_sha     TEXT,
  slice_hash  TEXT,
  created     TEXT,
  applied     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_solutions_git_sha  ON solutions(git_sha);
CREATE INDEX IF NOT EXISTS idx_solutions_task     ON solutions(task);
CREATE INDEX IF NOT EXISTS idx_solutions_slice    ON solutions(slice_hash);
"""

_WS_RE = re.compile(r"\s+")


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def normalize_task(task: str) -> str:
    """Canonical form of a task description, used for hashing."""
    if not isinstance(task, str):
        raise TypeError(f"normalize_task expects str, got {type(task).__name__}")
    return _WS_RE.sub(" ", task.strip()).lower()


def compute_hash(task: str, slice_hash: str, git_sha: str) -> str:
    """task_hash = sha256(normalized_task + slice_hash + git_sha) hex digest."""
    n_task = normalize_task(task)
    payload = f"{n_task}|{slice_hash}|{git_sha}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def slice_hash(slice_obj: Any) -> str:
    """Stable hash of a context slice.

    Prefers a hash the slicer already computed; otherwise hashes the symbol
    identities (path + qualified name) *and* the rendered text, since two slices
    with the same symbols but different excerpts are not interchangeable.
    """
    if slice_obj is None:
        return "no-slice"
    for attr in ("content_hash", "hash"):
        value = getattr(slice_obj, attr, None)
        if isinstance(value, str) and value:
            return value
    symbols = getattr(slice_obj, "symbols", None)
    if symbols:
        idents = sorted(
            f"{getattr(s, 'path', '')}::{getattr(s, 'qualified_name', getattr(s, 'name', ''))}"
            for s in symbols
        )
        return hash_text("\n".join(idents))
    render = getattr(slice_obj, "render", None)
    if callable(render):
        try:
            return hash_text(render())
        except Exception:  # pragma: no cover - defensive
            pass
    return hash_text(str(slice_obj))


def task_hash(task: str, slice_obj: Any = None, git_sha: str = "") -> str:
    """SHA-256 over the normalized task, the slice hash and the commit."""
    payload = "\x1f".join([normalize_task(task), slice_hash(slice_obj), git_sha or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    # Positional order compatibility for CacheEntry(*row) from solutions table:
    task_hash: str = ""
    task: str = ""
    diff: str = ""
    git_sha: str = ""
    slice_hash: str = ""
    created: str = ""
    applied: int = 0

    # Key-value fields from main:
    key: str = ""
    namespace: str = ""
    value: Any = None
    model: str = ""
    created_at: str = ""
    expires_at: Optional[str] = None
    hits: int = 0
    bytes: int = 0

    def __post_init__(self) -> None:
        if not self.key and self.task_hash:
            self.key = self.task_hash
        if not self.task_hash and self.key:
            self.task_hash = self.key
        if not self.created_at and self.created:
            self.created_at = self.created
        if not self.created and self.created_at:
            self.created = self.created_at
        if self.value is None and (self.diff or self.task or self.git_sha or self.slice_hash):
            self.value = {
                "diff": self.diff,
                "task": self.task,
                "git_sha": self.git_sha,
                "slice_hash": self.slice_hash,
            }

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "namespace": self.namespace,
            "model": self.model,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "hits": self.hits,
            "bytes": self.bytes,
            "task_hash": self.task_hash,
            "task": self.task,
            "diff": self.diff,
            "git_sha": self.git_sha,
            "slice_hash": self.slice_hash,
            "created": self.created,
            "applied": self.applied,
        }


def _now_iso(clock: Any = None) -> str:
    epoch = clock() if clock is not None else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))



class CacheManager:
    """SQLite-backed cache keyed by :func:`task_hash`."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        config: Optional[CoreConfig] = None,
        *,
        settings: Any = None,
        cache_config: Optional[CacheConfig] = None,
        metrics: Any = None,
        trace: Any = None,
        clock: Any = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        self.config = config or CoreConfig()
        self.settings = cache_config or self.config.cache
        self.metrics = metrics
        self.trace = trace
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self.enabled = bool(self.settings.enabled)

        if connection is not None:
            self.conn = connection
            self._owns_conn = False
        else:
            if db_path is None:
                if settings is not None:
                    db_path = getattr(settings, "db_path", None)
                if db_path is None:
                    try:
                        from gigaforge.config import get_settings
                        db_path = str(get_settings().db_path)
                    except Exception:
                        db_path = ":memory:"
            db_path_str = str(db_path)
            self.db_path = db_path_str
            if db_path_str != ":memory:":
                Path(db_path_str).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(db_path_str, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self._owns_conn = True
        self._migrate()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _migrate(self) -> None:
        with self._lock:
            self.conn.executescript(CACHE_SCHEMA)
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def close(self) -> None:
        if self._owns_conn:
            try:
                self.conn.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def __enter__(self) -> "CacheManager":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── keys ─────────────────────────────────────────────────────────────────
    def key_for(self, task: str, slice_obj: Any = None, git_sha: str = "") -> str:
        return task_hash(task, slice_obj, git_sha)

    # ── read / write ─────────────────────────────────────────────────────────
    def get(self, key: str) -> Optional[CacheEntry]:
        if not self.enabled:
            return None
        with self._lock:
            cur = self.conn.execute(
                "SELECT task_hash, task, diff, git_sha, slice_hash, created, applied "
                "FROM solutions WHERE task_hash = ?",
                (key,),
            )
            sol_row = cur.fetchone()
            if sol_row is not None:
                return CacheEntry(
                    task_hash=sol_row["task_hash"],
                    task=sol_row["task"],
                    diff=sol_row["diff"],
                    git_sha=sol_row["git_sha"],
                    slice_hash=sol_row["slice_hash"],
                    created=sol_row["created"],
                    applied=int(sol_row["applied"]),
                    key=sol_row["task_hash"],
                    value={
                        "diff": sol_row["diff"],
                        "task": sol_row["task"],
                        "git_sha": sol_row["git_sha"],
                        "slice_hash": sol_row["slice_hash"],
                    },
                )
            row = self.conn.execute(
                "SELECT * FROM cache_entries WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            self._record("miss", key)
            return None
        if self._expired(row["expires_at"]):
            self.delete(key)
            self._record("expired", key)
            return None
        try:
            value = json.loads(row["value"].decode("utf-8"))
        except Exception as exc:  # pragma: no cover - corrupt row
            self.delete(key)
            raise CacheError(f"corrupt cache row {key}", detail={"error": str(exc)}) from exc
        with self._lock:
            self.conn.execute(
                "UPDATE cache_entries SET hits = hits + 1 WHERE cache_key = ?", (key,)
            )
            self.conn.commit()
        self._record("hit", key, model=row["model"] or "")
        diff = value.get("diff", "") if isinstance(value, dict) else ""
        task = value.get("task", "") if isinstance(value, dict) else ""
        git_sha = value.get("git_sha", "") if isinstance(value, dict) else ""
        slice_hash_val = value.get("slice_hash", "") if isinstance(value, dict) else ""
        return CacheEntry(
            key=key,
            namespace=row["namespace"],
            value=value,
            model=row["model"] or "",
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            hits=int(row["hits"]) + 1,
            bytes=int(row["bytes"]),
            task_hash=key,
            task=task,
            diff=diff,
            git_sha=git_sha,
            slice_hash=slice_hash_val,
        )

    def put(
        self,
        key: str,
        value: Any = None,
        *,
        task: str = "",
        diff: str = "",
        git_sha: str = "",
        slice_hash: str = "",
        namespace: Optional[str] = None,
        model: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> CacheEntry:
        if diff or task or slice_hash:
            created_str = datetime.now(tz=UTC).isoformat(timespec="microseconds")
            with self._lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO solutions "
                    "(task_hash, task, diff, git_sha, slice_hash, created, applied) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (key, task, diff, git_sha, slice_hash, created_str),
                )
                self.conn.commit()
            if value is None:
                value = {"diff": diff, "task": task, "git_sha": git_sha, "slice_hash": slice_hash}
        if not self.enabled:
            return CacheEntry(
                key=key,
                namespace=namespace or self.settings.namespace,
                value=value,
                task_hash=key,
                task=task,
                diff=diff,
                git_sha=git_sha,
                slice_hash=slice_hash,
            )
        blob = json.dumps(value, ensure_ascii=False, default=_default).encode("utf-8")
        ns = namespace or self.settings.namespace
        created = _now_iso(self._clock)
        ttl = self.settings.ttl_seconds if ttl_seconds is None else ttl_seconds
        expires = None
        if ttl and ttl > 0:
            expires = _now_iso(lambda: self._clock() + float(ttl))
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO cache_entries
                    (cache_key, namespace, value, value_type, model, created_at, expires_at, hits, bytes)
                VALUES (?, ?, ?, 'json', ?, ?, ?, 0, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    value = excluded.value,
                    model = excluded.model,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at,
                    bytes = excluded.bytes
                """,
                (key, ns, blob, model, created, expires, len(blob)),
            )
            self.conn.commit()
        self._evict_if_needed()
        self._record("write", key, model=model)
        return CacheEntry(
            key=key,
            namespace=ns,
            value=value,
            model=model,
            created_at=created,
            expires_at=expires,
            bytes=len(blob),
            task_hash=key,
            task=task,
            diff=diff,
            git_sha=git_sha,
            slice_hash=slice_hash,
        )

    @staticmethod
    def hash_for(task: str, slice_hash: str, git_sha: str) -> str:
        return compute_hash(task, slice_hash, git_sha)

    def get_valid(
        self, *, task: str, slice_hash: str, git_sha: str
    ) -> Optional[CacheEntry]:
        h = compute_hash(task, slice_hash, git_sha)
        entry = self.get(h)
        if entry is None or entry.git_sha != git_sha:
            return None
        return entry

    def put_for(
        self, *, task: str, diff: str, git_sha: str, slice_hash: str
    ) -> str:
        h = compute_hash(task, slice_hash, git_sha)
        self.put(h, task=task, diff=diff, git_sha=git_sha, slice_hash=slice_hash)
        return h

    def mark_applied(self, task_hash: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE solutions SET applied = 1 WHERE task_hash = ?",
                (task_hash,),
            )
            self.conn.commit()

    def invalidate_stale_git_sha(self, git_sha: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM solutions WHERE git_sha IS NOT NULL AND git_sha != ?",
                (git_sha,),
            )
            self.conn.commit()
            return cur.rowcount or 0

    def invalidate_all(self) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM solutions")
            self.conn.commit()
            return cur.rowcount or 0

    def count(self) -> int:
        with self._lock:
            cur = self.conn.execute("SELECT COUNT(*) FROM solutions")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def list_entries(self) -> list[CacheEntry]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT task_hash, task, diff, git_sha, slice_hash, created, applied "
                "FROM solutions ORDER BY created DESC"
            )
            rows = cur.fetchall()
        return [
            CacheEntry(
                task_hash=r["task_hash"],
                task=r["task"],
                diff=r["diff"],
                git_sha=r["git_sha"],
                slice_hash=r["slice_hash"],
                created=r["created"],
                applied=int(r["applied"]),
            )
            for r in rows
        ]


    def delete(self, key: str) -> bool:
        with self._lock:
            cursor = self.conn.execute("DELETE FROM cache_entries WHERE cache_key = ?", (key,))
            self.conn.commit()
        return cursor.rowcount > 0

    # ── TTL / invalidation ───────────────────────────────────────────────────
    def _expired(self, expires_at: Optional[str]) -> bool:
        if not expires_at:
            return False
        try:
            # ``calendar.timegm`` reads the string as UTC.  ``time.mktime`` would
            # read it as *local* time, so on any non-UTC machine every expiry would
            # be off by twice the UTC offset.
            parsed = calendar.timegm(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            return False
        return self._clock() >= parsed

    def purge_expired(self) -> int:
        rows = self.conn.execute(
            "SELECT cache_key, expires_at FROM cache_entries WHERE expires_at IS NOT NULL"
        ).fetchall()
        gone = [r["cache_key"] for r in rows if self._expired(r["expires_at"])]
        if gone:
            with self._lock:
                self.conn.executemany(
                    "DELETE FROM cache_entries WHERE cache_key = ?", [(k,) for k in gone]
                )
                self.conn.commit()
        return len(gone)

    def note_git_sha(self, sha: str) -> bool:
        """Record the commit the strongest cache entries were built against.

        Returns ``True`` when the commit actually changed, which is the caller's
        cue that a fresh slice is needed.  Entries are *not* deleted — they are
        keyed by the old SHA and therefore simply unreachable — but a bounded
        sweep keeps the file from growing without limit.
        """
        if not sha:
            return False
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM cache_meta WHERE key = 'git_sha'"
            ).fetchone()
            previous = row["value"] if row else ""
            if previous == sha:
                return False
            self.conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES ('git_sha', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (sha,),
            )
            self.conn.commit()
        self._record("invalidate", f"git_sha:{previous}->{sha}")
        if self.trace is not None:
            self.trace.event("cache.invalidate", reason="git_sha_changed",
                             previous=previous, current=sha)
        return True

    def git_sha(self) -> str:
        row = self.conn.execute("SELECT value FROM cache_meta WHERE key = 'git_sha'").fetchone()
        return row["value"] if row else ""

    def invalidate_by_git(self, sha: str) -> int:
        """Drop entries whose key was built for ``sha`` — best effort.

        Keys are opaque hashes, so the SHA cannot be recovered from them.  The
        honest implementation therefore drops *all* task-namespace entries when
        the commit changes: correctness beats a cache hit that replays a diff
        onto code that has moved underneath it.
        """
        if not self.settings.invalidate_on_git_change:
            return 0
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM cache_entries WHERE namespace = ?", (self.settings.namespace,)
            )
            self.conn.commit()
        removed = cursor.rowcount
        if removed:
            self._record("purge", f"git:{sha}", count=removed)
        return removed

    def invalidate_namespace(self, namespace: str) -> int:
        with self._lock:
            cursor = self.conn.execute("DELETE FROM cache_entries WHERE namespace = ?", (namespace,))
            self.conn.commit()
        return cursor.rowcount

    def clear(self) -> int:
        with self._lock:
            cursor = self.conn.execute("DELETE FROM cache_entries")
            self.conn.commit()
        return cursor.rowcount

    # ── housekeeping ─────────────────────────────────────────────────────────
    def _evict_if_needed(self) -> None:
        limit = int(self.settings.max_entries or 0)
        if limit <= 0:
            return
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) AS n FROM cache_entries").fetchone()["n"]
            if total <= limit:
                return
            self.conn.execute(
                """
                DELETE FROM cache_entries WHERE cache_key IN (
                    SELECT cache_key FROM cache_entries
                    ORDER BY hits ASC, created_at ASC
                    LIMIT ?
                )
                """,
                (total - limit,),
            )
            self.conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(hits), 0) AS hits, COALESCE(SUM(bytes), 0) AS bytes "
                "FROM cache_entries"
            ).fetchone()
            expired = sum(
                1 for r in self.conn.execute(
                    "SELECT expires_at FROM cache_entries WHERE expires_at IS NOT NULL"
                ).fetchall() if self._expired(r["expires_at"])
            )
        return {
            "entries": int(row["n"]),
            "hits": int(row["hits"]),
            "bytes": int(row["bytes"]),
            "expired": expired,
            "enabled": self.enabled,
            "ttl_seconds": self.settings.ttl_seconds,
            "git_sha": self.git_sha(),
        }

    def _record(self, event: str, key: str, *, model: str = "", count: int = 0) -> None:
        if self.metrics is not None:
            self.metrics.incr(f"core.cache.{event}", count or 1, {"ns": self.settings.namespace})
        if self.trace is not None:
            self.trace.event("cache." + event, key=key[:16], model=model, count=count)


def _default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (set, tuple)):
        return list(value)
    return str(value)


__all__ = [
    "CacheManager",
    "CacheEntry",
    "CACHE_SCHEMA",
    "task_hash",
    "slice_hash",
    "normalize_task",
    "hash_text",
    "compute_hash",
]


"""Shared SQLite connection factory (V2).

V1 opened a *separate* connection per component — metrics, cache and
checkpoints each called ``sqlite3.connect`` on the same file, each with its own
write lock.  That is survivable while the executor is strictly sequential, but
V2 runs independent DAG nodes concurrently, and three writers on three
connections against one file is a guaranteed ``database is locked`` storm.

V2 centralises connection creation in one place:

* **one connection per database path**, shared by every component that needs it;
* **WAL** journal mode, so readers never block the single writer;
* a **busy timeout**, so brief contention waits instead of raising;
* ``synchronous=NORMAL``, which is durable under WAL and far cheaper on the hot
  path than the default ``FULL``.

The registry is process-local and keyed by resolved path.  ``:memory:`` databases
are deliberately *not* shared: two components asking for an in-memory database
want two independent databases, and silently aliasing them would make tests
depend on each other.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

DEFAULT_BUSY_TIMEOUT_MS = 5000


def configure_connection(
    conn: sqlite3.Connection, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """Apply the V2 pragmas to a connection.

    Every pragma is best-effort: an in-memory database cannot switch to WAL, and
    a read-only file cannot change ``synchronous``.  Neither is a reason to fail
    a run, so a rejected pragma is ignored rather than raised.
    """
    conn.row_factory = sqlite3.Row
    for pragma in (
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA foreign_keys=ON",
        f"PRAGMA busy_timeout={int(busy_timeout_ms)}",
    ):
        try:
            conn.execute(pragma)
        except sqlite3.Error:  # pragma: no cover - depends on the filesystem
            pass
    return conn


class Database:
    """A single, shared, thread-safe SQLite connection.

    The lock is re-entrant so a component may hold it across a nested call
    (``transaction`` inside ``transaction``) without deadlocking itself.
    """

    def __init__(
        self,
        path: str = ":memory:",
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        self.path = str(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.RLock()
        if connection is not None:
            self.conn = connection
            self._owns = False
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(
                self.path,
                check_same_thread=False,
                timeout=self.busy_timeout_ms / 1000.0,
            )
            configure_connection(self.conn, busy_timeout_ms=self.busy_timeout_ms)
            self._owns = True

    # ── raw access ────────────────────────────────────────────────────────────
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq: Any) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, seq)

    def executescript(self, script: str) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executescript(script)

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self.conn.rollback()

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
        if self._owns:
            with self._lock:
                try:
                    self.conn.close()
                except Exception:  # pragma: no cover - defensive
                    pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Database(path={self.path!r}, shared={not self._owns})"


_REGISTRY: dict[str, Database] = {}
_REGISTRY_LOCK = threading.Lock()


def get_database(
    path: str = ":memory:", *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Database:
    """Return the shared :class:`Database` for ``path``, creating it once."""
    if str(path) == ":memory:":
        return Database(path, busy_timeout_ms=busy_timeout_ms)
    key = str(Path(path).resolve())
    with _REGISTRY_LOCK:
        db = _REGISTRY.get(key)
        if db is None:
            db = Database(key, busy_timeout_ms=busy_timeout_ms)
            _REGISTRY[key] = db
        return db


def reset_registry() -> None:
    """Close and forget every shared database (used by tests and shutdown)."""
    with _REGISTRY_LOCK:
        for db in _REGISTRY.values():
            db.close()
        _REGISTRY.clear()


__all__ = [
    "Database",
    "get_database",
    "reset_registry",
    "configure_connection",
    "DEFAULT_BUSY_TIMEOUT_MS",
]

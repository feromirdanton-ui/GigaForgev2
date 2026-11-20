"""SQLite schema + migration runner for the L0 symbol index.

The schema is versioned through ``l0_meta.schema_version`` and applied by
:func:`migrate`.  Migrations are append-only: each entry in :data:`MIGRATIONS`
is a ``(version, [statements])`` pair, and only versions above the stored one
are executed.  This keeps upgrades idempotent and safe to run on every start.

Design notes
------------
* **Symbols are content-addressed** by ``symbol_id`` (path + qualified name +
  kind + byte offset), so a re-index of an unchanged file is a no-op upsert.
* **Edges are stored twice**: a normalised ``l0_edges`` table for graph work
  (PageRank, callers) and denormalised ``l0_calls`` for cheap "who calls X".
* **FTS5** indexes name / qualified name / signature / docstring / snippet so
  the repo map and the search tool can rank without loading every row.
* **WAL** + ``busy_timeout`` let the indexer write while readers query.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .config import L0Config

SCHEMA_VERSION = 1

# ── DDL ──────────────────────────────────────────────────────────────────

_PRAGMAS = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA temp_store = MEMORY;",
)

_V1 = [
    """
    CREATE TABLE IF NOT EXISTS l0_meta (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS l0_files (
      file_id      TEXT PRIMARY KEY,          -- sha1(rel_path)
      path         TEXT NOT NULL UNIQUE,      -- repo-relative, posix
      language     TEXT NOT NULL,
      source_hash  TEXT NOT NULL,             -- sha256 of raw bytes
      bytes        INTEGER NOT NULL DEFAULT 0,
      lines        INTEGER NOT NULL DEFAULT 0,
      mtime_ns     INTEGER,
      git_blob     TEXT,                      -- blob sha at index time
      symbols      INTEGER NOT NULL DEFAULT 0,
      edges        INTEGER NOT NULL DEFAULT 0,
      parse_errors INTEGER NOT NULL DEFAULT 0,
      indexed_at   TEXT NOT NULL,
      deleted_at   TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_l0_files_lang ON l0_files(language) WHERE deleted_at IS NULL;",
    "CREATE INDEX IF NOT EXISTS ix_l0_files_hash ON l0_files(source_hash);",
    """
    CREATE TABLE IF NOT EXISTS l0_symbols (
      symbol_id      TEXT PRIMARY KEY,
      file_id        TEXT NOT NULL REFERENCES l0_files(file_id) ON DELETE CASCADE,
      path           TEXT NOT NULL,
      name           TEXT NOT NULL,
      qualified_name TEXT NOT NULL,
      kind           TEXT NOT NULL,
      language       TEXT NOT NULL,
      start_line     INTEGER NOT NULL,
      end_line       INTEGER NOT NULL,
      start_byte     INTEGER NOT NULL,
      end_byte       INTEGER NOT NULL,
      signature      TEXT NOT NULL DEFAULT '',
      docstring      TEXT NOT NULL DEFAULT '',
      parent_id      TEXT,
      is_public      INTEGER NOT NULL DEFAULT 1,
      is_async       INTEGER NOT NULL DEFAULT 0,
      decorators     TEXT NOT NULL DEFAULT '[]',
      return_type    TEXT NOT NULL DEFAULT '',
      params         TEXT NOT NULL DEFAULT '[]',
      complexity     INTEGER NOT NULL DEFAULT 1,
      snippet        TEXT NOT NULL DEFAULT '',
      content_hash   TEXT NOT NULL DEFAULT '',
      updated_at     TEXT NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_l0_sym_file ON l0_symbols(file_id, start_line);",
    "CREATE INDEX IF NOT EXISTS ix_l0_sym_name ON l0_symbols(name);",
    "CREATE INDEX IF NOT EXISTS ix_l0_sym_qname ON l0_symbols(qualified_name);",
    "CREATE INDEX IF NOT EXISTS ix_l0_sym_kind ON l0_symbols(kind);",
    "CREATE INDEX IF NOT EXISTS ix_l0_sym_parent ON l0_symbols(parent_id);",
    """
    CREATE TABLE IF NOT EXISTS l0_edges (
      id     INTEGER PRIMARY KEY AUTOINCREMENT,
      src    TEXT NOT NULL,
      dst    TEXT NOT NULL,
      kind   TEXT NOT NULL,                   -- contains|calls|imports|inherits
      line   INTEGER NOT NULL DEFAULT 0,
      weight REAL NOT NULL DEFAULT 1.0,
      file_id TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_l0_edges_src ON l0_edges(src, kind);",
    "CREATE INDEX IF NOT EXISTS ix_l0_edges_dst ON l0_edges(dst, kind);",
    "CREATE INDEX IF NOT EXISTS ix_l0_edges_kind ON l0_edges(kind);",
    """
    CREATE TABLE IF NOT EXISTS l0_imports (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      file_id     TEXT NOT NULL REFERENCES l0_files(file_id) ON DELETE CASCADE,
      module      TEXT NOT NULL,
      names       TEXT NOT NULL DEFAULT '[]',
      alias       TEXT NOT NULL DEFAULT '',
      line        INTEGER NOT NULL DEFAULT 0,
      is_relative INTEGER NOT NULL DEFAULT 0
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_l0_imports_file ON l0_imports(file_id);",
    "CREATE INDEX IF NOT EXISTS ix_l0_imports_module ON l0_imports(module);",
    """
    CREATE TABLE IF NOT EXISTS l0_calls (
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      file_id   TEXT NOT NULL REFERENCES l0_files(file_id) ON DELETE CASCADE,
      caller_id TEXT,
      callee    TEXT NOT NULL,
      line      INTEGER NOT NULL DEFAULT 0,
      is_method INTEGER NOT NULL DEFAULT 0,
      receiver  TEXT NOT NULL DEFAULT ''
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_l0_calls_callee ON l0_calls(callee);",
    "CREATE INDEX IF NOT EXISTS ix_l0_calls_caller ON l0_calls(caller_id);",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS l0_symbols_fts USING fts5(
      name,
      qualified_name,
      signature,
      docstring,
      snippet,
      symbol_id UNINDEXED,
      path UNINDEXED,
      kind UNINDEXED,
      tokenize = 'unicode61 remove_diacritics 2'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS l0_index_runs (
      run_id         TEXT PRIMARY KEY,
      mode           TEXT NOT NULL,           -- full | incremental
      status         TEXT NOT NULL,           -- running | ok | failed
      started_at     TEXT NOT NULL,
      finished_at    TEXT,
      duration_ms    INTEGER,
      files_scanned  INTEGER NOT NULL DEFAULT 0,
      files_indexed  INTEGER NOT NULL DEFAULT 0,
      files_skipped  INTEGER NOT NULL DEFAULT 0,
      files_removed  INTEGER NOT NULL DEFAULT 0,
      symbols        INTEGER NOT NULL DEFAULT 0,
      edges          INTEGER NOT NULL DEFAULT 0,
      parse_errors   INTEGER NOT NULL DEFAULT 0,
      base_rev       TEXT,
      head_rev       TEXT,
      error          TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_l0_runs_started ON l0_index_runs(started_at);",
    """
    CREATE TABLE IF NOT EXISTS l0_repo_map_cache (
      cache_key  TEXT PRIMARY KEY,            -- sha1(root|rev|budget|weights)
      payload    TEXT NOT NULL,
      tokens     INTEGER NOT NULL DEFAULT 0,
      symbols    INTEGER NOT NULL DEFAULT 0,
      built_at   TEXT NOT NULL,
      rev        TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS l0_metrics (
      id     INTEGER PRIMARY KEY AUTOINCREMENT,
      ts     TEXT NOT NULL,
      metric TEXT NOT NULL,
      value  REAL NOT NULL,
      labels TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_l0_metrics ON l0_metrics(metric, ts);",
]

MIGRATIONS: tuple[tuple[int, Sequence[str]], ...] = ((1, tuple(_V1)),)


# ── connection helpers ───────────────────────────────────────────────────


def connect(
    db_path: str | Path,
    *,
    config: Optional[L0Config] = None,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open (and create if needed) the L0 database with sane pragmas."""
    path = Path(db_path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)

    uri = f"file:{path}?mode={'ro' if read_only else 'rwc'}"
    # ``check_same_thread=False`` is deliberate: the symbol index is created in
    # the composition root and then read by DAG nodes, the slicer and the UI,
    # which run on worker threads.  Python's sqlite3 serialises access to a
    # shared connection (SQLite's default SERIALIZED threading mode), and
    # busy_timeout covers the writer-vs-reader contention, so a single handle is
    # safe where a per-thread handle would triple the page cache and the WAL
    # bookkeeping.  Without this flag every cross-thread read raises
    # "SQLite objects created in a thread can only be used in that same thread".
    conn = sqlite3.connect(
        uri, uri=True, timeout=5.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row

    cfg = config or L0Config()
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(cfg.busy_timeout_ms)};")
        if not read_only:
            conn.execute(f"PRAGMA journal_mode = {cfg.journal_mode};")
            conn.execute(f"PRAGMA synchronous = {cfg.synchronous};")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA temp_store = MEMORY;")
    except sqlite3.DatabaseError:
        # A read-only or exotic filesystem may reject some pragmas; the
        # connection is still usable, so we degrade instead of failing.
        pass
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM l0_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def migrate(conn: sqlite3.Connection, *, target: int = SCHEMA_VERSION) -> int:
    """Apply every migration above the stored version. Returns the new version."""
    version = current_version(conn)
    if version >= target:
        return version

    for ver, statements in MIGRATIONS:
        if ver <= version or ver > target:
            continue
        conn.execute("BEGIN")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO l0_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(ver),),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        version = ver
    return version


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


__all__ = [
    "SCHEMA_VERSION",
    "MIGRATIONS",
    "connect",
    "migrate",
    "current_version",
    "table_names",
]

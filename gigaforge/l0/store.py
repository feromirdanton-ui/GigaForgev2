"""Persistence layer for the L0 symbol index.

:class:`L0Store` owns a single SQLite connection and exposes the operations
the indexer, the repo map and the AST tools need:

* ``upsert_file`` / ``remove_file`` — atomic per-file replacement;
* ``symbols_in_file`` / ``find_symbol`` / ``symbol_by_id`` — lookups;
* ``callers_of`` / ``callees_of`` / ``imports_of`` — graph queries;
* ``search`` — FTS5 + LIKE hybrid over names, signatures and docstrings;
* ``stats`` — counters for the CLI and the metrics table.

Every write goes through :meth:`L0Store.transaction`, which wraps the work in
``BEGIN IMMEDIATE`` so concurrent readers never see a half-indexed file.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from . import db as _db
from .config import L0Config
from .errors import IndexError_
from .parser import Edge, Import, ParsedFile, Symbol


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def file_id_for(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:20]


@dataclass
class StoreStats:
    files: int = 0
    symbols: int = 0
    edges: int = 0
    imports: int = 0
    calls: int = 0
    languages: int = 0
    parse_errors: int = 0
    db_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "symbols": self.symbols,
            "edges": self.edges,
            "imports": self.imports,
            "calls": self.calls,
            "languages": self.languages,
            "parse_errors": self.parse_errors,
            "db_bytes": self.db_bytes,
        }


class L0Store:
    """Thin, explicit wrapper around the L0 SQLite schema."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        config: Optional[L0Config] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self.config = config or L0Config()
        self.db_path = str(db_path or self.config.db_path)
        self._owns_conn = conn is None
        self.conn = conn or _db.connect(self.db_path, config=self.config)
        _db.migrate(self.conn)

    # ── lifecycle ────────────────────────────────────────────────────────
    def close(self) -> None:
        if self._owns_conn:
            try:
                self.conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass

    def __enter__(self) -> "L0Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` … ``COMMIT`` with rollback on error."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ── writes ───────────────────────────────────────────────────────────
    def upsert_file(self, parsed: ParsedFile, *, git_blob: str = "", mtime_ns: int = 0) -> dict[str, int]:
        """Replace every row belonging to ``parsed.path`` with the new parse.

        Returns a small counter dict.  The whole operation is one transaction,
        so a crash mid-way leaves the previous revision intact.
        """
        fid = file_id_for(parsed.path)
        counts = {"symbols": 0, "edges": 0, "imports": 0, "calls": 0}

        with self.transaction() as conn:
            conn.execute("DELETE FROM l0_symbols WHERE file_id = ?", (fid,))
            conn.execute("DELETE FROM l0_edges WHERE file_id = ?", (fid,))
            conn.execute("DELETE FROM l0_imports WHERE file_id = ?", (fid,))
            conn.execute("DELETE FROM l0_calls WHERE file_id = ?", (fid,))
            conn.execute(
                "DELETE FROM l0_symbols_fts WHERE path = ?", (parsed.path,)
            )

            conn.execute(
                """
                INSERT INTO l0_files(
                  file_id, path, language, source_hash, bytes, lines, mtime_ns,
                  git_blob, symbols, edges, parse_errors, indexed_at, deleted_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(file_id) DO UPDATE SET
                  path=excluded.path, language=excluded.language,
                  source_hash=excluded.source_hash, bytes=excluded.bytes,
                  lines=excluded.lines, mtime_ns=excluded.mtime_ns,
                  git_blob=excluded.git_blob, symbols=excluded.symbols,
                  edges=excluded.edges, parse_errors=excluded.parse_errors,
                  indexed_at=excluded.indexed_at, deleted_at=NULL
                """,
                (
                    fid,
                    parsed.path,
                    parsed.language,
                    parsed.source_hash,
                    parsed.bytes,
                    parsed.lines,
                    mtime_ns,
                    git_blob,
                    len(parsed.symbols),
                    len(parsed.edges),
                    len(parsed.errors),
                    _now(),
                ),
            )

            for sym in parsed.symbols:
                conn.execute(
                    """
                    INSERT INTO l0_symbols(
                      symbol_id, file_id, path, name, qualified_name, kind, language,
                      start_line, end_line, start_byte, end_byte, signature, docstring,
                      parent_id, is_public, is_async, decorators, return_type, params,
                      complexity, snippet, content_hash, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sym.symbol_id,
                        fid,
                        sym.path,
                        sym.name,
                        sym.qualified_name,
                        sym.kind,
                        sym.language,
                        sym.start_line,
                        sym.end_line,
                        sym.start_byte,
                        sym.end_byte,
                        sym.signature,
                        sym.docstring,
                        sym.parent_id,
                        int(sym.is_public),
                        int(sym.is_async),
                        json.dumps(list(sym.decorators)),
                        sym.return_type,
                        json.dumps(list(sym.params)),
                        sym.complexity,
                        sym.snippet,
                        hashlib.sha1(
                            f"{sym.signature}\x00{sym.docstring}\x00{sym.snippet}".encode("utf-8")
                        ).hexdigest()[:20],
                        _now(),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO l0_symbols_fts(
                      name, qualified_name, signature, docstring, snippet,
                      symbol_id, path, kind
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        sym.name,
                        sym.qualified_name,
                        sym.signature,
                        sym.docstring,
                        sym.snippet,
                        sym.symbol_id,
                        sym.path,
                        sym.kind,
                    ),
                )
                counts["symbols"] += 1

            for edge in parsed.edges:
                conn.execute(
                    "INSERT INTO l0_edges(src, dst, kind, line, weight, file_id) VALUES(?,?,?,?,?,?)",
                    (edge.src, edge.dst, edge.kind, edge.line, edge.weight, fid),
                )
                counts["edges"] += 1

            for imp in parsed.imports:
                conn.execute(
                    "INSERT INTO l0_imports(file_id, module, names, alias, line, is_relative) VALUES(?,?,?,?,?,?)",
                    (fid, imp.module, json.dumps(list(imp.names)), imp.alias, imp.line, int(imp.is_relative)),
                )
                counts["imports"] += 1

            for call in parsed.calls:
                conn.execute(
                    "INSERT INTO l0_calls(file_id, caller_id, callee, line, is_method, receiver) VALUES(?,?,?,?,?,?)",
                    (fid, call.caller_id, call.callee, call.line, int(call.is_method), call.receiver),
                )
                counts["calls"] += 1

        return counts

    def remove_file(self, path: str) -> bool:
        """Soft-delete a file and hard-delete its symbols/edges."""
        fid = file_id_for(path)
        with self.transaction() as conn:
            row = conn.execute("SELECT file_id FROM l0_files WHERE file_id = ?", (fid,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM l0_symbols WHERE file_id = ?", (fid,))
            conn.execute("DELETE FROM l0_edges WHERE file_id = ?", (fid,))
            conn.execute("DELETE FROM l0_imports WHERE file_id = ?", (fid,))
            conn.execute("DELETE FROM l0_calls WHERE file_id = ?", (fid,))
            conn.execute("DELETE FROM l0_symbols_fts WHERE path = ?", (path,))
            conn.execute(
                "UPDATE l0_files SET deleted_at = ?, symbols = 0, edges = 0 WHERE file_id = ?",
                (_now(), fid),
            )
        return True

    def purge_file(self, path: str) -> None:
        """Hard-delete a file row (used by tests and ``--prune``)."""
        fid = file_id_for(path)
        with self.transaction() as conn:
            conn.execute("DELETE FROM l0_files WHERE file_id = ?", (fid,))

    # ── reads ────────────────────────────────────────────────────────────
    def file_row(self, path: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM l0_files WHERE path = ?", (path,)
        ).fetchone()

    def file_hash(self, path: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT source_hash FROM l0_files WHERE path = ? AND deleted_at IS NULL",
            (path,),
        ).fetchone()
        return row["source_hash"] if row else None

    def all_files(self, *, include_deleted: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM l0_files"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY path"
        return list(self.conn.execute(sql).fetchall())

    def symbols_in_file(self, path: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM l0_symbols WHERE path = ? ORDER BY start_line",
                (path,),
            ).fetchall()
        )

    def symbol_by_id(self, symbol_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM l0_symbols WHERE symbol_id = ?", (symbol_id,)
        ).fetchone()

    def find_symbol(
        self,
        name: str,
        *,
        kind: Optional[str] = None,
        path: Optional[str] = None,
        exact: bool = True,
    ) -> list[sqlite3.Row]:
        clauses = []
        params: list[Any] = []
        if exact:
            clauses.append("(name = ? OR qualified_name = ?)")
            params.extend([name, name])
        else:
            clauses.append("(name LIKE ? OR qualified_name LIKE ?)")
            params.extend([f"%{name}%", f"%{name}%"])
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if path:
            clauses.append("path = ?")
            params.append(path)
        sql = "SELECT * FROM l0_symbols WHERE " + " AND ".join(clauses)
        sql += " ORDER BY is_public DESC, start_line LIMIT 200"
        return list(self.conn.execute(sql, params).fetchall())

    def callers_of(self, symbol_id: str, *, limit: int = 200) -> list[sqlite3.Row]:
        """Symbols that call ``symbol_id`` (resolved edges)."""
        return list(
            self.conn.execute(
                """
                SELECT s.*, e.line AS call_line
                FROM l0_edges e
                JOIN l0_symbols s ON s.symbol_id = e.src
                WHERE e.dst = ? AND e.kind = 'calls'
                ORDER BY s.path, e.line
                LIMIT ?
                """,
                (symbol_id, limit),
            ).fetchall()
        )

    def callers_of_name(self, name: str, *, limit: int = 200) -> list[sqlite3.Row]:
        """Call sites by callee name, resolved to the enclosing symbol."""
        return list(
            self.conn.execute(
                """
                SELECT c.callee, c.line, c.receiver, c.is_method,
                       s.symbol_id, s.name AS caller_name, s.qualified_name,
                       s.path, s.kind
                FROM l0_calls c
                LEFT JOIN l0_symbols s ON s.symbol_id = c.caller_id
                WHERE c.callee = ?
                ORDER BY s.path, c.line
                LIMIT ?
                """,
                (name, limit),
            ).fetchall()
        )

    def callees_of(self, symbol_id: str, *, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT e.dst, e.line, e.weight, s.name, s.qualified_name, s.path, s.kind
                FROM l0_edges e
                LEFT JOIN l0_symbols s ON s.symbol_id = e.dst
                WHERE e.src = ? AND e.kind = 'calls'
                ORDER BY e.line
                LIMIT ?
                """,
                (symbol_id, limit),
            ).fetchall()
        )

    def imports_of(self, path: str) -> list[sqlite3.Row]:
        fid = file_id_for(path)
        return list(
            self.conn.execute(
                "SELECT * FROM l0_imports WHERE file_id = ? ORDER BY line", (fid,)
            ).fetchall()
        )

    def importers_of(self, module: str, *, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT f.path, i.module, i.names, i.line
                FROM l0_imports i JOIN l0_files f ON f.file_id = i.file_id
                WHERE i.module = ? OR i.module LIKE ?
                ORDER BY f.path
                LIMIT ?
                """,
                (module, f"{module}.%", limit),
            ).fetchall()
        )

    def children_of(self, symbol_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM l0_symbols WHERE parent_id = ? ORDER BY start_line",
                (symbol_id,),
            ).fetchall()
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: Optional[str] = None,
        path_prefix: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search: FTS5 BM25 first, LIKE fallback, merged by score."""
        results: dict[str, dict[str, Any]] = {}

        fts_query = _fts_query(query)
        if fts_query:
            try:
                rows = self.conn.execute(
                    """
                    SELECT f.symbol_id, f.path, f.kind, f.name, f.qualified_name,
                           f.signature, f.docstring,
                           bm25(l0_symbols_fts, 8.0, 4.0, 2.0, 1.0, 0.5) AS score
                    FROM l0_symbols_fts f
                    WHERE l0_symbols_fts MATCH ?
                    ORDER BY score
                    LIMIT ?
                    """,
                    (fts_query, limit * 3),
                ).fetchall()
                for row in rows:
                    results[row["symbol_id"]] = {
                        "symbol_id": row["symbol_id"],
                        "path": row["path"],
                        "kind": row["kind"],
                        "name": row["name"],
                        "qualified_name": row["qualified_name"],
                        "signature": row["signature"],
                        "docstring": row["docstring"],
                        "score": -float(row["score"]),  # bm25 is lower-is-better
                        "source": "fts",
                    }
            except sqlite3.OperationalError:
                pass

        like = f"%{query}%"
        rows = self.conn.execute(
            """
            SELECT symbol_id, path, kind, name, qualified_name, signature, docstring
            FROM l0_symbols
            WHERE name LIKE ? OR qualified_name LIKE ? OR signature LIKE ?
            ORDER BY is_public DESC, length(name)
            LIMIT ?
            """,
            (like, like, like, limit * 3),
        ).fetchall()
        for row in rows:
            sid = row["symbol_id"]
            if sid in results:
                results[sid]["score"] += 0.5
                continue
            results[sid] = {
                "symbol_id": sid,
                "path": row["path"],
                "kind": row["kind"],
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "signature": row["signature"],
                "docstring": row["docstring"],
                "score": 0.5,
                "source": "like",
            }

        if not query.strip():
            return []

        out = list(results.values())
        if kind:
            out = [r for r in out if r["kind"] == kind]
        if path_prefix:
            out = [r for r in out if r["path"].startswith(path_prefix)]
        out.sort(key=lambda r: (-r["score"], r["path"], r["name"]))
        return out[:limit]

    # ── graph helpers ────────────────────────────────────────────────────
    def call_graph(self, *, kinds: Sequence[str] = ("calls",)) -> tuple[list[str], list[tuple[str, str, float]]]:
        """Return ``(nodes, edges)`` for the symbol call graph.

        Only edges whose endpoints are real symbols are returned, so the
        PageRank in :mod:`gigaforge.l0.repo_map` operates on a clean graph.
        """
        placeholders = ",".join("?" for _ in kinds)
        rows = self.conn.execute(
            f"""
            SELECT e.src, e.dst, e.weight
            FROM l0_edges e
            JOIN l0_symbols a ON a.symbol_id = e.src
            JOIN l0_symbols b ON b.symbol_id = e.dst
            WHERE e.kind IN ({placeholders})
            """,
            tuple(kinds),
        ).fetchall()
        nodes: set[str] = set()
        edges: list[tuple[str, str, float]] = []
        for row in rows:
            nodes.add(row["src"])
            nodes.add(row["dst"])
            edges.append((row["src"], row["dst"], float(row["weight"])))
        return sorted(nodes), edges

    def resolve_dangling_edges(self) -> int:
        """Rewrite call edges whose ``dst`` is a bare name into real symbol ids.

        The parser can only resolve calls within a single file, so a call to a
        symbol defined elsewhere is stored as a dangling edge keyed by name.
        This repo-level pass turns those into proper ``calls`` edges, which is
        what makes ``callers_of`` and the PageRank in the repo map work across
        module boundaries.

        Returns the number of edges that were resolved.
        """
        dangling = self.conn.execute(
            """
            SELECT e.id, e.src, e.dst, e.line, e.weight, e.file_id
            FROM l0_edges e
            LEFT JOIN l0_symbols s ON s.symbol_id = e.dst
            WHERE e.kind = 'calls' AND s.symbol_id IS NULL
            """
        ).fetchall()
        if not dangling:
            return 0

        name_map: dict[str, list[str]] = {}
        for row in self.conn.execute("SELECT symbol_id, name FROM l0_symbols"):
            name_map.setdefault(row["name"], []).append(row["symbol_id"])

        resolved = 0
        with self.transaction() as conn:
            for edge in dangling:
                targets = name_map.get(edge["dst"])
                if not targets:
                    continue
                conn.execute("DELETE FROM l0_edges WHERE id = ?", (edge["id"],))
                for target in targets:
                    if target == edge["src"]:
                        continue
                    conn.execute(
                        "INSERT INTO l0_edges(src, dst, kind, line, weight, file_id) "
                        "VALUES(?,?,?,?,?,?)",
                        (edge["src"], target, "calls", edge["line"], edge["weight"], edge["file_id"]),
                    )
                resolved += 1
        return resolved

    def in_degrees(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT e.dst AS dst, COUNT(*) AS n
            FROM l0_edges e
            JOIN l0_symbols s ON s.symbol_id = e.dst
            WHERE e.kind = 'calls'
            GROUP BY e.dst
            """
        ).fetchall()
        return {row["dst"]: int(row["n"]) for row in rows}

    def all_symbols(self, *, limit: int = 100000) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM l0_symbols ORDER BY path, start_line LIMIT ?", (limit,)
            ).fetchall()
        )

    # ── stats / metrics ──────────────────────────────────────────────────
    def stats(self) -> StoreStats:
        def scalar(sql: str, params: Sequence[Any] = ()) -> int:
            row = self.conn.execute(sql, params).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        st = StoreStats()
        st.files = scalar("SELECT COUNT(*) FROM l0_files WHERE deleted_at IS NULL")
        st.symbols = scalar("SELECT COUNT(*) FROM l0_symbols")
        st.edges = scalar("SELECT COUNT(*) FROM l0_edges")
        st.imports = scalar("SELECT COUNT(*) FROM l0_imports")
        st.calls = scalar("SELECT COUNT(*) FROM l0_calls")
        st.languages = scalar("SELECT COUNT(DISTINCT language) FROM l0_files WHERE deleted_at IS NULL")
        st.parse_errors = scalar("SELECT COALESCE(SUM(parse_errors),0) FROM l0_files WHERE deleted_at IS NULL")
        try:
            st.db_bytes = Path(self.db_path).stat().st_size
        except OSError:
            st.db_bytes = 0
        return st

    def record_metric(self, metric: str, value: float, labels: Optional[Mapping[str, Any]] = None) -> None:
        self.conn.execute(
            "INSERT INTO l0_metrics(ts, metric, value, labels) VALUES(?,?,?,?)",
            (_now(), metric, float(value), json.dumps(dict(labels or {}))),
        )

    def start_index_run(self, run_id: str, mode: str, *, base_rev: str = "", head_rev: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO l0_index_runs(run_id, mode, status, started_at, base_rev, head_rev)
            VALUES(?,?,?,?,?,?)
            """,
            (run_id, mode, "running", _now(), base_rev, head_rev),
        )

    def finish_index_run(self, run_id: str, status: str, counters: Mapping[str, int], *, error: str = "") -> None:
        self.conn.execute(
            """
            UPDATE l0_index_runs SET
              status=?, finished_at=?, duration_ms=?,
              files_scanned=?, files_indexed=?, files_skipped=?, files_removed=?,
              symbols=?, edges=?, parse_errors=?, error=?
            WHERE run_id=?
            """,
            (
                status,
                _now(),
                int(counters.get("duration_ms", 0)),
                int(counters.get("files_scanned", 0)),
                int(counters.get("files_indexed", 0)),
                int(counters.get("files_skipped", 0)),
                int(counters.get("files_removed", 0)),
                int(counters.get("symbols", 0)),
                int(counters.get("edges", 0)),
                int(counters.get("parse_errors", 0)),
                error,
                run_id,
            ),
        )

    def last_index_run(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM l0_index_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    # ── repo map cache ───────────────────────────────────────────────────
    def get_repo_map(self, cache_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM l0_repo_map_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()

    def put_repo_map(self, cache_key: str, payload: str, *, tokens: int, symbols: int, rev: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO l0_repo_map_cache(cache_key, payload, tokens, symbols, built_at, rev)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
              payload=excluded.payload, tokens=excluded.tokens,
              symbols=excluded.symbols, built_at=excluded.built_at, rev=excluded.rev
            """,
            (cache_key, payload, tokens, symbols, _now(), rev),
        )


def _fts_query(query: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression."""
    tokens = [t for t in "".join(c if c.isalnum() or c == "_" else " " for c in query).split() if t]
    if not tokens:
        return ""
    # prefix-match each token so ``parse`` finds ``parse_source``
    return " OR ".join(f'"{t}"*' for t in tokens)


__all__ = ["L0Store", "StoreStats", "file_id_for"]

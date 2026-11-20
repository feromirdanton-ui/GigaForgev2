"""Repository indexing: full scan and git-diff incremental update.

:class:`Indexer` walks the working tree, parses every supported file with
:class:`~gigaforge.l0.parser.SymbolParser` and writes the result through
:class:`~gigaforge.l0.store.L0Store`.

Two modes:

``full``
    Scan every file matching the include globs.  Files whose ``source_hash``
    already matches the stored one are skipped (idempotent re-index).

``incremental``
    Ask git for the changed / added / deleted paths between two revisions
    (``git diff --name-status <base> <head>``) and touch only those.  When git
    is unavailable or the repo is not a work tree, the indexer falls back to
    an mtime+size heuristic so the behaviour degrades gracefully.

The indexer never calls an LLM: it is pure local computation, which is the
whole point of the L0 layer.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .config import L0Config
from .errors import GitError, UnsupportedLanguageError
from .languages import spec_for_path
from .parser import SymbolParser
from .store import L0Store


@dataclass
class IndexResult:
    run_id: str
    mode: str
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    symbols: int = 0
    edges: int = 0
    parse_errors: int = 0
    duration_ms: int = 0
    base_rev: str = ""
    head_rev: str = ""
    changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "files_scanned": self.files_scanned,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "files_removed": self.files_removed,
            "symbols": self.symbols,
            "edges": self.edges,
            "parse_errors": self.parse_errors,
            "duration_ms": self.duration_ms,
            "base_rev": self.base_rev,
            "head_rev": self.head_rev,
            "changed": self.changed[:200],
            "errors": self.errors[:50],
        }


# ── git helpers ──────────────────────────────────────────────────────────


class Git:
    """Minimal, dependency-free git wrapper (subprocess only)."""

    def __init__(self, root: str | Path, config: Optional[L0Config] = None) -> None:
        self.root = Path(root)
        self.config = config or L0Config()

    def _run(self, *args: str) -> str:
        cmd = [self.config.git_bin, *args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=self.config.git_timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError(f"git binary not found: {self.config.git_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git timed out: {' '.join(args)}") from exc
        if proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}",
                detail={"returncode": proc.returncode},
            )
        return proc.stdout

    def is_repo(self) -> bool:
        try:
            out = self._run("rev-parse", "--is-inside-work-tree")
        except GitError:
            return False
        return out.strip() == "true"

    def head(self) -> str:
        try:
            return self._run("rev-parse", "HEAD").strip()
        except GitError:
            return ""

    def changed_files(self, base: str = "HEAD", head: str = "") -> list[tuple[str, str]]:
        """Return ``[(status, path)]`` for the diff ``base..head``.

        Status is one of ``A`` (added), ``M`` (modified), ``D`` (deleted),
        ``R`` (renamed — reported as delete+add).
        """
        args = ["diff", "--name-status", "--no-renames", base]
        if head:
            args.append(head)
        out = self._run(*args)
        pairs: list[tuple[str, str]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0].strip()[:1]
            path = parts[-1].strip()
            if path:
                pairs.append((status, path))
        return pairs

    def untracked_files(self) -> list[str]:
        try:
            out = self._run("ls-files", "--others", "--exclude-standard")
        except GitError:
            return []
        return [p.strip() for p in out.splitlines() if p.strip()]

    def blob_hash(self, path: str) -> str:
        try:
            return self._run("hash-object", "--", path).strip()
        except GitError:
            return ""


# ── indexer ──────────────────────────────────────────────────────────────


class Indexer:
    """Builds and maintains the L0 symbol index for a repository."""

    def __init__(
        self,
        store: L0Store,
        *,
        config: Optional[L0Config] = None,
        parser: Optional[SymbolParser] = None,
        git: Optional[Git] = None,
    ) -> None:
        self.store = store
        self.config = config or store.config
        self.parser = parser or SymbolParser(self.config)
        self.root = self.config.root
        self.git = git if git is not None else Git(self.root, self.config)

    # ── discovery ────────────────────────────────────────────────────────
    def discover(self) -> list[str]:
        """Return repo-relative posix paths of every indexable file."""
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=self.config.follow_symlinks):
            rel_dir = os.path.relpath(dirpath, self.root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

            # prune excluded directories early
            dirnames[:] = [
                d
                for d in dirnames
                if not self._excluded(f"{rel_dir}/{d}".lstrip("/") + "/")
            ]

            for name in filenames:
                rel = f"{rel_dir}/{name}".lstrip("/")
                if self._excluded(rel):
                    continue
                if not self._included(rel):
                    continue
                if spec_for_path(rel) is None:
                    continue
                try:
                    if (self.root / rel).stat().st_size > self.config.max_file_bytes:
                        continue
                except OSError:
                    continue
                out.append(rel)
        return sorted(out)

    def _included(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch("/" + rel, pat) for pat in self.config.include_globs)

    def _excluded(self, rel: str) -> bool:
        rel = rel.lstrip("/")
        for pat in self.config.exclude_globs:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch("/" + rel, pat):
                return True
            # directory prefix match: ``node_modules/**`` should prune ``node_modules``
            if pat.endswith("/**") and (rel == pat[:-3] or rel.startswith(pat[:-2])):
                return True
        return False

    # ── public entry points ──────────────────────────────────────────────
    def index_full(self, *, force: bool = False) -> IndexResult:
        started = time.perf_counter()
        run_id = uuid.uuid4().hex[:16]
        result = IndexResult(run_id=run_id, mode="full")
        result.head_rev = self.git.head() if self.config.incremental_use_git else ""

        self.store.start_index_run(run_id, "full", head_rev=result.head_rev)
        try:
            paths = self.discover()
            result.files_scanned = len(paths)
            seen = set(paths)

            for rel in paths:
                self._index_one(rel, result, force=force)

            # prune files that vanished from disk
            for row in self.store.all_files():
                if row["path"] not in seen:
                    if self.store.remove_file(row["path"]):
                        result.files_removed += 1

            # resolve cross-file call edges now that every symbol is present
            self.store.resolve_dangling_edges()

            result.duration_ms = int((time.perf_counter() - started) * 1000)
            self.store.finish_index_run(run_id, "ok", result.to_dict())
            self._record_metrics(result)
            return result
        except Exception as exc:
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            result.errors.append(str(exc))
            self.store.finish_index_run(run_id, "failed", result.to_dict(), error=str(exc))
            raise

    def index_incremental(
        self,
        *,
        base: str = "HEAD",
        head: str = "",
        include_untracked: bool = True,
    ) -> IndexResult:
        """Update only the files git reports as changed since ``base``.

        A **cold index bootstraps with a full pass**.  Git only reports files that
        differ from ``base``, so on an empty store every already-committed file is
        "unchanged" and nothing gets indexed — the index stays empty, the slicer
        finds no symbols, the coder receives no context and rewrites whole files.
        That exact chain was observed on a live run; the guard below is the fix.
        """
        started = time.perf_counter()
        if not self.store.all_files():
            full = self.index_full()
            full.mode = "incremental (bootstrapped from full)"
            return full
        run_id = uuid.uuid4().hex[:16]
        result = IndexResult(run_id=run_id, mode="incremental")
        result.base_rev = base
        result.head_rev = head or (self.git.head() if self.config.incremental_use_git else "")

        self.store.start_index_run(run_id, "incremental", base_rev=base, head_rev=result.head_rev)
        try:
            changes: list[tuple[str, str]] = []
            if self.config.incremental_use_git and self.git.is_repo():
                changes = self.git.changed_files(base, head)
                if include_untracked:
                    changes.extend(("A", p) for p in self.git.untracked_files())
            else:
                changes = self._mtime_changes()

            # de-duplicate, keeping the strongest status (D wins over M)
            merged: dict[str, str] = {}
            for status, path in changes:
                path = path.replace(os.sep, "/")
                if self._excluded(path) or spec_for_path(path) is None:
                    continue
                if merged.get(path) == "D":
                    continue
                merged[path] = status

            result.files_scanned = len(merged)
            for path, status in sorted(merged.items()):
                result.changed.append(f"{status} {path}")
                if status == "D":
                    if self.store.remove_file(path):
                        result.files_removed += 1
                    continue
                if not (self.root / path).exists():
                    if self.store.remove_file(path):
                        result.files_removed += 1
                    continue
                self._index_one(path, result, force=False)

            # a changed file may define or remove a symbol other files call
            self.store.resolve_dangling_edges()

            result.duration_ms = int((time.perf_counter() - started) * 1000)
            self.store.finish_index_run(run_id, "ok", result.to_dict())
            self._record_metrics(result)
            return result
        except Exception as exc:
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            result.errors.append(str(exc))
            self.store.finish_index_run(run_id, "failed", result.to_dict(), error=str(exc))
            raise

    def index_paths(self, paths: Sequence[str], *, force: bool = True) -> IndexResult:
        """Index an explicit list of paths (used by the AST tools after an edit)."""
        started = time.perf_counter()
        run_id = uuid.uuid4().hex[:16]
        result = IndexResult(run_id=run_id, mode="paths")
        result.files_scanned = len(paths)
        self.store.start_index_run(run_id, "paths")
        for rel in paths:
            rel = rel.replace(os.sep, "/")
            if not (self.root / rel).exists():
                if self.store.remove_file(rel):
                    result.files_removed += 1
                continue
            self._index_one(rel, result, force=force)
        self.store.resolve_dangling_edges()
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        self.store.finish_index_run(run_id, "ok", result.to_dict())
        self._record_metrics(result)
        return result

    # ── internals ────────────────────────────────────────────────────────
    def _store_markup(
        self,
        rel: str,
        text: str,
        markup: List[Any],
        source_hash: str,
        result: IndexResult,
        *,
        force: bool,
    ) -> None:
        """Persist markup symbols through the same store path as code symbols."""
        from .parser import ParsedFile, Symbol

        symbols: List[Any] = []
        for index, item in enumerate(markup):
            data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            name = str(data.get("name") or "").strip()
            if not name:
                continue
            line = max(1, int(data.get("line") or 1))
            symbols.append(Symbol(
                symbol_id=f"{rel}::{data.get('kind', 'markup')}::{name}::{line}",
                name=name,
                qualified_name=f"{rel}::{name}",
                kind=str(data.get("kind") or "markup"),
                language="markup",
                path=rel,
                start_line=line,
                end_line=line,
                start_byte=0,
                end_byte=0,
                signature=str(data.get("signature") or ""),
                docstring=str(data.get("doc") or ""),
            ))
        parsed = ParsedFile(
            path=rel,
            language="markup",
            source_hash=source_hash,
            bytes=len(text.encode("utf-8", errors="replace")),
            lines=max(1, len(text.splitlines())),
            symbols=symbols,
        )
        try:
            mtime_ns = (self.root / rel).stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        counts = self.store.upsert_file(parsed, git_blob="", mtime_ns=mtime_ns)
        result.files_indexed += 1
        result.symbols += counts["symbols"]
        result.edges += counts["edges"]

    def _index_one(self, rel: str, result: IndexResult, *, force: bool) -> None:
        abs_path = self.root / rel
        try:
            data = abs_path.read_bytes()
        except OSError as exc:
            result.errors.append(f"{rel}: {exc}")
            return

        source_hash = hashlib.sha256(data).hexdigest()
        if not force:
            existing = self.store.file_hash(rel)
            if existing == source_hash:
                result.files_skipped += 1
                return

        try:
            parsed = self.parser.parse_source(data, rel)
        except UnsupportedLanguageError:
            # Markup has no tree-sitter grammar here, but it still has structure
            # (ids, classes, headings, inline-script functions).  Indexing it as
            # symbols is what lets a web project have a repo map and a slice
            # instead of falling back to "read the whole file".
            from .markup import MARKUP_EXTENSIONS, extract_markup_symbols

            if Path(rel).suffix.lower() not in MARKUP_EXTENSIONS:
                result.files_skipped += 1
                return
            text = data.decode("utf-8", errors="replace")
            markup = extract_markup_symbols(text, rel)
            self._store_markup(rel, text, markup, source_hash, result, force=force)
            return
        except Exception as exc:  # pragma: no cover - defensive
            result.errors.append(f"{rel}: {exc}")
            result.parse_errors += 1
            return

        if parsed.errors:
            result.parse_errors += 1

        try:
            mtime_ns = abs_path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0

        blob = self.git.blob_hash(rel) if self.config.incremental_use_git and self.git.is_repo() else ""
        counts = self.store.upsert_file(parsed, git_blob=blob, mtime_ns=mtime_ns)

        result.files_indexed += 1
        result.symbols += counts["symbols"]
        result.edges += counts["edges"]

    def _mtime_changes(self) -> list[tuple[str, str]]:
        """Fallback when git is unavailable: compare mtime/size to the index."""
        changes: list[tuple[str, str]] = []
        known = {row["path"]: row for row in self.store.all_files()}
        for rel in self.discover():
            row = known.get(rel)
            if row is None:
                changes.append(("A", rel))
                continue
            try:
                st = (self.root / rel).stat()
            except OSError:
                changes.append(("D", rel))
                continue
            if st.st_mtime_ns != (row["mtime_ns"] or 0) or st.st_size != (row["bytes"] or 0):
                changes.append(("M", rel))
        for path in known:
            if not (self.root / path).exists():
                changes.append(("D", path))
        return changes

    def _record_metrics(self, result: IndexResult) -> None:
        try:
            self.store.record_metric("l0.index.duration_ms", result.duration_ms, {"mode": result.mode})
            self.store.record_metric("l0.index.files_indexed", result.files_indexed, {"mode": result.mode})
            self.store.record_metric("l0.index.symbols", result.symbols, {"mode": result.mode})
            self.store.record_metric("l0.index.parse_errors", result.parse_errors, {"mode": result.mode})
        except Exception:  # pragma: no cover - metrics must never break indexing
            pass


__all__ = ["Indexer", "IndexResult", "Git"]

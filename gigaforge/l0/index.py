"""L0 Index — tree-sitter + SQLite, ZERO LLM calls.

Backs every Context Slice. Provides symbol skeletons (no bodies), caller graph,
imports, string literals, and dependencies — all deterministic.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from gigaforge.config import get_settings

from .config import L0Config

log = logging.getLogger(__name__)

# --- tree-sitter setup (NEW API for tree_sitter 0.26) ----------------------
# Verify once at import; this is the form other agents should copy.
PY_LANG = Language(tspython.language())
_PARSER = Parser(PY_LANG)


def _parse(source: str):
    """Parse Python source; return tree.root_node."""
    return _PARSER.parse(bytes(source, "utf8")).root_node


# --- data classes (CONTRACT — Slicer / Coder consume these) ----------------


@dataclass(frozen=True)
class SymbolInfo:
    """Full info on a symbol (qname-qualified)."""

    path: str
    name: str
    kind: str  # "function" | "method" | "class"
    qname: str  # "Class.method" or "func"
    signature: str  # the `def ...(...)` or `class ...:` line text
    start_line: int  # 1-indexed
    end_line: int  # 1-indexed, inclusive
    body_hash: str  # sha256 of body text (for change detection)
    body: str | None = None  # populated ONLY by explicit body-read APIs


@dataclass(frozen=True)
class SymbolSkeleton:
    """A symbol WITHOUT its body. This is what read_structure returns.

    The L0 promise: never ship bodies unless explicitly requested.
    """

    name: str
    kind: str
    qname: str
    signature: str
    start_line: int
    end_line: int
    body: None = None  # always None — by construction


@dataclass(frozen=True)
class ImportInfo:
    path: str
    module: str  # dotted module path (relative imports: ".utils" / "..utils")
    alias: str  # local name (after `as`, or last segment of module)


@dataclass(frozen=True)
class StringLiteral:
    path: str
    value: str
    line: int


@dataclass(frozen=True)
class CallInfo:
    caller_path: str
    caller_symbol: str  # qname of enclosing function/method (or "<module>")
    callee: str  # called name (best-effort)


# --- internals --------------------------------------------------------------


_IGNORE_DIRS = {".git", "__pycache__", ".gigaforge", "node_modules", ".venv", "dist", "build", ".eggs"}
_PY_EXT = ".py"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# tree-sitter points are 0-indexed rows; we expose 1-indexed line numbers.
def _line1(point_row0: int) -> int:
    return point_row0 + 1


# --- the index ---------------------------------------------------------------


class L0Index:
    """SQLite-backed L0 index. Thread-unsafe by design (use one per task)."""

    def __init__(self, db_path: Path | None = None, *, config: L0Config | None = None) -> None:
        s = get_settings()
        self.config = config or L0Config.from_env()
        self.git_timeout_s = float(self.config.git_timeout_s)
        self.db_path = Path(db_path) if db_path is not None else Path(s.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    # -- schema --

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                sha TEXT NOT NULL,
                mtime REAL NOT NULL,
                lines INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                qname TEXT NOT NULL,
                signature TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                body_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                module TEXT NOT NULL,
                alias TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS string_literals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                value TEXT NOT NULL,
                line INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_path TEXT NOT NULL,
                caller_symbol TEXT NOT NULL,
                callee TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_symbols_qname ON symbols(qname);
            CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
            CREATE INDEX IF NOT EXISTS idx_imports_path ON imports(path);
            CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee);
            CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_symbol);
            """
        )
        self.conn.commit()

    # -- single-file indexing --

    def _clear_file(self, path: str) -> None:
        cur = self.conn.cursor()
        for tbl in ("symbols", "imports", "string_literals", "files"):
            cur.execute(f"DELETE FROM {tbl} WHERE path = ?", (path,))
        cur.execute("DELETE FROM calls WHERE caller_path = ?", (path,))
        self.conn.commit()

    def index_file(self, path: Path) -> None:
        """Parse a Python file and store its symbols/imports/calls/strings."""
        path = Path(path)
        source = path.read_text(encoding="utf-8")
        rel = str(path)
        self._clear_file(rel)

        root = _parse(source)
        if root is None or root.has_error:
            log.warning("L0: skipping %s (parse error)", rel)
            # still record file as seen
            self.conn.execute(
                "INSERT OR REPLACE INTO files (path, sha, mtime, lines) VALUES (?,?,?,?)",
                (rel, _sha(source), path.stat().st_mtime, source.count("\n") + 1),
            )
            self.conn.commit()
            return

        # walk
        symbols: list[dict] = []
        imports: list[dict] = []
        calls: list[dict] = []
        strings: list[dict] = []
        lines = source.split("\n")

        # Top-level walk: collect function/class/imports/strings, recurse into classes.
        def _walk(node, parent_qname: str, parent_kind: str):
            for child in node.children:
                t = child.type
                if t == "decorated_definition":
                    # unwrap; treat the decorated def as the symbol
                    inner = child.child_by_field_name("definition")
                    if inner is None:
                        # fallback: first function_definition/class_definition child
                        inner = next(
                            (c for c in child.children if c.type in ("function_definition", "class_definition")),
                            None,
                        )
                    if inner is not None:
                        _handle_def(inner, parent_qname, parent_kind, decorated_node=child)
                elif t == "function_definition":
                    _handle_def(child, parent_qname, parent_kind)
                elif t == "class_definition":
                    _handle_def(child, parent_qname, parent_kind)
                    # recurse into class body for methods
                    cls_name = _ident_text(child)
                    if cls_name is not None:
                        block = child.child_by_field_name("body")
                        if block is not None:
                            _walk(block, f"{parent_qname}.{cls_name}" if parent_qname else cls_name, "class")
                elif t in ("import_statement", "import_from_statement"):
                    _handle_import(child)
                elif t == "string":
                    _handle_string(child)
                # calls can be anywhere — handled by a separate pass below

        def _handle_def(node, parent_qname: str, parent_kind: str, decorated_node=None):
            name = _ident_text(node)
            if name is None:
                return
            qname = f"{parent_qname}.{name}" if parent_qname else name
            if node.type == "function_definition":
                kind = "method" if parent_kind == "class" else "function"
            else:
                kind = "class"
            # signature: the `def ...(...)` line OR `class Name(...):` line
            # We grab from start of def/class token to end of parameters (or identifier for class).
            sig = _signature_text(node, source, lines)
            # body bytes: from after `:` to end of node
            body_text = _body_text(node, source)
            # body_hash on body text (without leading colon etc.) for change detection
            bhash = _sha(body_text)
            # if decorated, span includes the decorator lines
            span_node = decorated_node if decorated_node is not None else node
            start_line = _line1(span_node.start_point[0])
            end_line = _line1(span_node.end_point[0])
            symbols.append(
                {
                    "path": rel,
                    "name": name,
                    "kind": kind,
                    "qname": qname,
                    "signature": sig,
                    "start_line": start_line,
                    "end_line": end_line,
                    "body_hash": bhash,
                }
            )

        def _handle_import(node):
            # Two forms:
            #   import a, b.c as d
            #   from mod import e as f, g
            if node.type == "import_statement":
                # children: 'import' [aliased_import|dotted_name]+ (',' ...)
                for ch in node.children:
                    if ch.type == "aliased_import":
                        mod = _dotted_text(ch.child_by_field_name("name"))
                        alias = _ident_text(ch.child_by_field_name("alias")) or mod
                    elif ch.type == "dotted_name":
                        mod = ch.text.decode("utf-8")
                        alias = mod.rsplit(".", 1)[-1]
                    else:
                        continue
                    if mod:
                        imports.append({"path": rel, "module": mod, "alias": alias})
            elif node.type == "import_from_statement":
                # module: dotted_name or relative_import (BEFORE the `import` keyword)
                # imported names: aliased_import | dotted_name (AFTER the `import` keyword)
                mod_text = ""
                seen_import_kw = False
                for ch in node.children:
                    if ch.type == "import" and ch.text == b"import":
                        seen_import_kw = True
                        continue
                    if not seen_import_kw:
                        if ch.type == "dotted_name":
                            mod_text = ch.text.decode("utf-8")
                        elif ch.type == "relative_import":
                            parts = []
                            for c in ch.children:
                                if c.type == "import_prefix":
                                    parts.append(c.text.decode("utf-8"))
                                elif c.type == "dotted_name":
                                    parts.append(c.text.decode("utf-8"))
                            mod_text = "".join(parts)
                    else:
                        # after `import` kw: collect imported names
                        if ch.type == "aliased_import":
                            nm = _dotted_text(ch.child_by_field_name("name"))
                            alias = _ident_text(ch.child_by_field_name("alias")) or nm
                        elif ch.type == "dotted_name":
                            nm = ch.text.decode("utf-8")
                            alias = nm.rsplit(".", 1)[-1]
                        elif ch.type == "wildcard_import":
                            nm = "*"
                            alias = "*"
                        else:
                            continue
                        if nm:
                            full_mod = f"{mod_text}.{nm}" if mod_text and nm != "*" else (mod_text or nm)
                            imports.append({"path": rel, "module": full_mod, "alias": alias})

        def _handle_string(node):
            try:
                raw = node.text.decode("utf-8")
            except Exception:
                return
            # strip surrounding quotes (best-effort)
            val = _strip_quotes(raw)
            strings.append({"path": rel, "value": val, "line": _line1(node.start_point[0])})

        # First pass: symbols + imports (top-level + class-body)
        _walk(root, "", "")

        # Pass for strings: full recursion to catch module-level AND nested strings
        # (docstrings, assignments, literals inside functions, etc.)
        def _collect_strings(node):
            for child in node.children:
                if child.type == "string":
                    _handle_string(child)
                _collect_strings(child)

        _collect_strings(root)

        # Second pass: calls, tracking enclosing function/method qname
        def _walk_calls(node, enclosing_qname: str, parent_kind: str):
            for child in node.children:
                t = child.type
                if t == "function_definition":
                    name = _ident_text(child) or ""
                    q = f"{enclosing_qname}.{name}" if enclosing_qname else name
                    block = child.child_by_field_name("body")
                    if block is not None:
                        _walk_calls(block, q, "function")
                    continue
                if t == "class_definition":
                    name = _ident_text(child) or ""
                    q = f"{enclosing_qname}.{name}" if enclosing_qname else name
                    block = child.child_by_field_name("body")
                    if block is not None:
                        _walk_calls(block, q, "class")
                    continue
                if t == "decorated_definition":
                    inner = child.child_by_field_name("definition") or next(
                        (c for c in child.children if c.type in ("function_definition", "class_definition")),
                        None,
                    )
                    if inner is not None:
                        _walk_calls(child, enclosing_qname, parent_kind)
                    continue
                if t == "call":
                    callee = _callee_name(child)
                    if callee:
                        calls.append(
                            {
                                "caller_path": rel,
                                "caller_symbol": enclosing_qname or "<module>",
                                "callee": callee,
                            }
                        )
                # recurse for nested (e.g. inside if/for/with)
                _walk_calls(child, enclosing_qname, parent_kind)

        _walk_calls(root, "", "")

        # persist
        cur = self.conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO files (path, sha, mtime, lines) VALUES (?,?,?,?)",
            (rel, _sha(source), path.stat().st_mtime, len(lines)),
        )
        if symbols:
            cur.executemany(
                "INSERT INTO symbols (path,name,kind,qname,signature,start_line,end_line,body_hash) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        s["path"],
                        s["name"],
                        s["kind"],
                        s["qname"],
                        s["signature"],
                        s["start_line"],
                        s["end_line"],
                        s["body_hash"],
                    )
                    for s in symbols
                ],
            )
        if imports:
            cur.executemany(
                "INSERT INTO imports (path,module,alias) VALUES (?,?,?)",
                [(i["path"], i["module"], i["alias"]) for i in imports],
            )
        if strings:
            cur.executemany(
                "INSERT INTO string_literals (path,value,line) VALUES (?,?,?)",
                [(s["path"], s["value"], s["line"]) for s in strings],
            )
        if calls:
            cur.executemany(
                "INSERT INTO calls (caller_path,caller_symbol,callee) VALUES (?,?,?)",
                [(c["caller_path"], c["caller_symbol"], c["callee"]) for c in calls],
            )
        self.conn.commit()

    # -- repo indexing --

    def index_repo(self, root: Path) -> int:
        """Walk .py files (respecting _IGNORE_DIRS) and index each. Returns count."""
        root = Path(root)
        n = 0
        for path in _walk_py(root, _IGNORE_DIRS):
            try:
                self.index_file(path)
                n += 1
            except Exception as e:  # noqa: BLE001 — never crash the whole repo walk
                log.warning("L0: failed to index %s: %s", path, e)
        self._set_meta("last_index_root", str(root.resolve()))
        self._set_meta("last_index_ts", str(__import__("time").time()))
        return n

    def incremental_update(self, root: Path) -> int:
        """Reindex only changed files. Tries git diff; falls back to mtime+sha."""
        root = Path(root)
        changed: list[Path] = []
        last_commit = self._get_meta("last_index_commit")
        try:
            head = _git_head(root, timeout=self.git_timeout_s)
        except FileNotFoundError:
            log.warning("L0: git not available; falling back to mtime+sha scan")
            head = None
        except subprocess.CalledProcessError:
            log.warning("L0: %s is not a git repo; falling back to mtime+sha scan", root)
            head = None
        except subprocess.TimeoutExpired:
            log.warning("L0: git head lookup timed out; falling back to mtime+sha scan")
            head = None

        if head is not None and last_commit and last_commit != head:
            # git diff between commits
            try:
                names = _git_diff_names(root, last_commit, head, timeout=self.git_timeout_s)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                log.warning("L0: git diff failed (%s); falling back to mtime+sha scan", exc)
                head = None
            if head is not None:
                for nm in names:
                    p = root / nm
                    if p.suffix == _PY_EXT and p.exists() and not _is_ignored(p, root, _IGNORE_DIRS):
                        changed.append(p)
                # also detect deletions: remove from index files that no longer exist
                self._prune_missing_files(root)
                self._set_meta("last_index_commit", head)
                n_indexed = 0
                for p in changed:
                    self.index_file(p)
                    n_indexed += 1
                return n_indexed
        elif head is not None and not last_commit:
            # first git-aware run: index everything
            n = self.index_repo(root)
            self._set_meta("last_index_commit", head)
            return n
        # If git is unavailable, unchanged, invalid, or too slow, use the
        # deterministic filesystem fingerprint path instead.
        return self._mtime_sha_update(root)

    def _mtime_sha_update(self, root: Path) -> int:
        root = Path(root)
        cur = self.conn.cursor()
        cur.execute("SELECT path, sha, mtime FROM files")
        known = {r["path"]: (r["sha"], r["mtime"]) for r in cur.fetchall()}
        n = 0
        for path in _walk_py(root, _IGNORE_DIRS):
            rel = str(path)
            try:
                st = path.stat()
            except OSError:
                continue
            old = known.get(rel)
            source = path.read_text(encoding="utf-8")
            sha = _sha(source)
            if old is None or old[0] != sha or abs(old[1] - st.st_mtime) > 1e-6:
                self.index_file(path)
                n += 1
        # prune deleted
        self._prune_missing_files(root)
        return n

    def _prune_missing_files(self, root: Path) -> None:
        root = Path(root)
        cur = self.conn.cursor()
        cur.execute("SELECT path FROM files")
        rows = cur.fetchall()
        for r in rows:
            p = Path(r["path"])
            if not p.exists() and not (root / r["path"]).exists():
                # remove
                self._clear_file(r["path"])

    # -- meta --

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
        )
        self.conn.commit()

    def _get_meta(self, key: str) -> str | None:
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
        r = cur.fetchone()
        return r["value"] if r else None

    # -- queries --

    def get_symbol(self, name: str) -> SymbolInfo | None:
        """Look up a symbol by qname (preferred) or by short name.

        Returns the first match. Body is NOT populated — use read_body() for that.
        """
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM symbols WHERE qname = ? LIMIT 1", (name,))
        r = cur.fetchone()
        if r is None:
            # Fallback to short name; prefer top-level definitions (qname == name)
            # so that "add" resolves to the module-level `add`, not `Calculator.add`.
            cur.execute(
                "SELECT * FROM symbols WHERE name = ? ORDER BY (qname = name) DESC LIMIT 1",
                (name,),
            )
            r = cur.fetchone()
        if r is None:
            return None
        return SymbolInfo(
            path=r["path"],
            name=r["name"],
            kind=r["kind"],
            qname=r["qname"],
            signature=r["signature"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            body_hash=r["body_hash"],
            body=None,
        )

    def get_callers(self, name: str) -> list[str]:
        """Return list of caller qnames that call `name` (or its short form)."""
        cur = self.conn.cursor()
        # match either qname (Class.method) or short name
        short = name.rsplit(".", 1)[-1]
        cur.execute(
            "SELECT DISTINCT caller_symbol FROM calls WHERE callee = ? OR callee = ?",
            (name, short),
        )
        return [r["caller_symbol"] for r in cur.fetchall()]

    def get_imports(self, path: str) -> list[ImportInfo]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM imports WHERE path = ?", (path,))
        return [
            ImportInfo(path=r["path"], module=r["module"], alias=r["alias"])
            for r in cur.fetchall()
        ]

    def read_structure(self, path: str) -> list[SymbolSkeleton]:
        """File skeleton: signatures + line numbers, NO bodies. The L0 promise."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM symbols WHERE path = ? ORDER BY start_line",
            (path,),
        )
        return [
            SymbolSkeleton(
                name=r["name"],
                kind=r["kind"],
                qname=r["qname"],
                signature=r["signature"],
                start_line=r["start_line"],
                end_line=r["end_line"],
                body=None,
            )
            for r in cur.fetchall()
        ]

    def dependencies_of(self, name: str) -> list[str]:
        """Imported modules referenced inside the body of `name`."""
        sym = self.get_symbol(name)
        if sym is None:
            return []
        try:
            source = Path(sym.path).read_text(encoding="utf-8")
        except OSError:
            return []
        lines = source.split("\n")
        body_lines = lines[sym.start_line - 1 : sym.end_line]
        body_text = "\n".join(body_lines)
        imports = self.get_imports(sym.path)
        # Build alias -> module map; pick the longest alias that matches each word.
        deps: set[str] = set()
        for imp in imports:
            alias = imp.alias or imp.module.rsplit(".", 1)[-1]
            if not alias or alias == "*":
                continue
            # word-boundary match the alias in body
            if re.search(rf"\b{re.escape(alias)}\b", body_text):
                deps.add(imp.module)
        return sorted(deps)

    def all_symbols(self, path: str | None = None) -> list[SymbolInfo]:
        cur = self.conn.cursor()
        if path:
            cur.execute("SELECT * FROM symbols WHERE path = ? ORDER BY start_line", (path,))
        else:
            cur.execute("SELECT * FROM symbols ORDER BY path, start_line")
        return [
            SymbolInfo(
                path=r["path"],
                name=r["name"],
                kind=r["kind"],
                qname=r["qname"],
                signature=r["signature"],
                start_line=r["start_line"],
                end_line=r["end_line"],
                body_hash=r["body_hash"],
                body=None,
            )
            for r in cur.fetchall()
        ]

    def close(self) -> None:
        self.conn.close()


# --- tree-sitter node helpers -----------------------------------------------


def _ident_text(node) -> str | None:
    """Identifier child text of a def/class node."""
    for ch in node.children:
        if ch.type == "identifier":
            return ch.text.decode("utf-8")
    return None


def _dotted_text(node) -> str:
    if node is None:
        return ""
    return node.text.decode("utf-8")


def _signature_text(node, source: str, lines: list[str]) -> str:
    """For function_definition: `def name(params) -> Ret` (no trailing colon).
    For class_definition: `class Name` or `class Name(Bases)`.
    Returns the text up to (but not including) the block / colon.

    CRIT-05: the return-type annotation (`-> Ret`) MUST be included. We extend
    the end boundary to the `return_type` field child when present, so the
    stored signature is the full `def name(params) -> Ret` form (the Coder and
    the RepoMap both rely on the complete signature).

    NOTE: tree-sitter offsets are BYTE offsets. Slicing a Python `str` with byte
    offsets corrupts output when the source contains non-ASCII (e.g. Cyrillic
    comments) before the node. We slice the UTF-8 bytes and decode instead.
    """
    src_bytes = source.encode("utf-8")

    def _slice(start_byte: int, end_byte: int) -> str:
        return src_bytes[start_byte:end_byte].decode("utf-8", errors="replace")

    if node.type == "function_definition":
        # span from `def` to end of `parameters` (or `name` if no params).
        params = node.child_by_field_name("parameters")
        ident = node.child_by_field_name("name")
        if ident is None:
            return node.text.decode("utf-8").splitlines()[0] if node.text else ""
        # Base end = end of params (or identifier if params missing).
        end_byte = params.end_byte if params is not None else ident.end_byte
        end_row0 = params.end_point[0] if params is not None else ident.end_point[0]
        # CRIT-05: extend to the return-type annotation `-> Ret` if present.
        ret = node.child_by_field_name("return_type")
        if ret is not None:
            end_byte = ret.end_byte
            end_row0 = ret.end_point[0]
        if end_row0 != node.start_point[0]:
            # signature spans multiple lines — grab up to end of params/return
            chunk = _slice(node.start_byte, end_byte)
            # collapse internal newlines+indent to single space
            chunk = re.sub(r"\s*\n\s*", " ", chunk)
            return chunk
        return _slice(node.start_byte, end_byte)
    elif node.type == "class_definition":
        # class Name(Bases)
        # find `:` token; signature is everything from `class` to before `:`
        colon = None
        for ch in node.children:
            if ch.type == ":":
                colon = ch
                break
        if colon is None:
            return lines[node.start_point[0]].strip()
        end = colon.start_byte
        if node.start_point[0] != colon.start_point[0]:
            chunk = _slice(node.start_byte, end)
            chunk = re.sub(r"\s*\n\s*", " ", chunk)
            return chunk
        return _slice(node.start_byte, end)
    return node.text.decode("utf-8").splitlines()[0] if node.text else ""


def _body_text(node, source: str) -> str:
    """Return body text of a function/class (the block child's text, or empty)."""
    body = node.child_by_field_name("body")
    if body is None:
        return ""
    return body.text.decode("utf-8")


def _callee_name(call_node) -> str:
    """Best-effort callee name for a `call` node."""
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return ""
    if fn.type == "identifier":
        return fn.text.decode("utf-8")
    if fn.type == "attribute":
        # obj.method -> method
        attr = fn.child_by_field_name("attribute")
        if attr is not None:
            return attr.text.decode("utf-8")
    if fn.type == "call":
        # chained: foo()(...) -> recurse
        return _callee_name(fn)
    # fallback: full text
    return fn.text.decode("utf-8")


def _strip_quotes(raw: str) -> str:
    if len(raw) >= 6 and raw[:3] in ('"""', "'''") and raw[-3:] == raw[:3]:
        return raw[3:-3]
    if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


# --- repo walking -----------------------------------------------------------


def _walk_py(root: Path, ignore: set[str]):
    """Yield .py files under root, skipping ignore dirs (any depth)."""
    for p in root.rglob("*.py"):
        # skip if any path part is in ignore set
        if any(part in ignore for part in p.parts):
            continue
        yield p


def _is_ignored(path: Path, root: Path, ignore: set[str]) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part in ignore for part in rel.parts)


def _git_head(root: Path, *, timeout: float = 30.0) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return out.stdout.strip()


def _git_diff_names(root: Path, a: str, b: str, *, timeout: float = 30.0) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", a, b],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]

"""tree-sitter based symbol extraction.

:class:`SymbolParser` turns a source file into a :class:`ParsedFile`:

* **symbols** — functions, methods, classes, types, constants, with a
  normalised signature, docstring, byte/line span and a stable symbol id;
* **imports** — module-level imports with their bound names;
* **calls** — every call site, resolved to a best-effort callee name;
* **edges** — ``contains`` (parent → child), ``calls`` (caller → callee),
  ``imports`` (file → module), ``inherits`` (class → base).

The parser is pure: it never touches the database or the filesystem beyond
reading the bytes it is handed.  That keeps it trivially testable and lets
the indexer decide what to persist.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .config import L0Config
from .errors import ParseError, UnsupportedLanguageError
from .languages import LanguageSpec, make_parser, spec_for_path

# ── data model ───────────────────────────────────────────────────────────


@dataclass
class Symbol:
    """One named definition extracted from a file."""

    symbol_id: str
    name: str
    qualified_name: str
    kind: str  # function | method | class | interface | type | const | variable | ...
    language: str
    path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    signature: str = ""
    docstring: str = ""
    parent_id: Optional[str] = None
    is_public: bool = True
    is_async: bool = False
    decorators: tuple[str, ...] = ()
    return_type: str = ""
    params: tuple[str, ...] = ()
    complexity: int = 0
    snippet: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "language": self.language,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "signature": self.signature,
            "docstring": self.docstring,
            "parent_id": self.parent_id,
            "is_public": int(self.is_public),
            "is_async": int(self.is_async),
            "decorators": list(self.decorators),
            "return_type": self.return_type,
            "params": list(self.params),
            "complexity": self.complexity,
            "snippet": self.snippet,
        }


@dataclass
class Import:
    module: str
    names: tuple[str, ...]
    alias: str = ""
    line: int = 0
    is_relative: bool = False

    def to_row(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "names": list(self.names),
            "alias": self.alias,
            "line": self.line,
            "is_relative": int(self.is_relative),
        }


@dataclass
class CallSite:
    callee: str
    line: int
    caller_id: Optional[str] = None
    is_method: bool = False
    receiver: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "callee": self.callee,
            "line": self.line,
            "caller_id": self.caller_id,
            "is_method": int(self.is_method),
            "receiver": self.receiver,
        }


@dataclass
class Edge:
    src: str
    dst: str
    kind: str  # contains | calls | imports | inherits
    line: int = 0
    weight: float = 1.0

    def to_row(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "kind": self.kind,
            "line": self.line,
            "weight": self.weight,
        }


@dataclass
class ParsedFile:
    path: str
    language: str
    source_hash: str
    bytes: int
    lines: int
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def symbol_by_id(self, symbol_id: str) -> Optional[Symbol]:
        for sym in self.symbols:
            if sym.symbol_id == symbol_id:
                return sym
        return None

    def find(self, name: str, kind: Optional[str] = None) -> list[Symbol]:
        out = []
        for sym in self.symbols:
            if sym.name != name and sym.qualified_name != name:
                continue
            if kind is not None and sym.kind != kind:
                continue
            out.append(sym)
        return out


# ── helpers ──────────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BRANCH_KINDS = {
    "if_statement",
    "elif_clause",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "case_clause",
    "switch_case",
    "match_arm",
    "catch_clause",
    "except_clause",
    "conditional_expression",
    "ternary_expression",
    "boolean_operator",
    "binary_operator",
    "and",
    "or",
}


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0].strip()


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _node_field(node: Any, field: str) -> Optional[Any]:
    try:
        return node.child_by_field_name(field)
    except Exception:  # pragma: no cover - defensive
        return None


def _walk(node: Any) -> Iterator[Any]:
    """Depth-first pre-order walk over a tree-sitter node."""
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        # push children in reverse so traversal is left-to-right
        for child in reversed(cur.children):
            stack.append(child)


def _count_branches(node: Any) -> int:
    """Cyclomatic-ish complexity: 1 + number of branch nodes in the body."""
    count = 1
    for child in _walk(node):
        if child.type in _BRANCH_KINDS:
            count += 1
    return count


def _is_public(name: str, language: str) -> bool:
    if not name:
        return False
    if language == "python":
        return not name.startswith("_")
    if language == "go":
        return name[:1].isupper()
    if language == "rust":
        return not name.startswith("_")
    return not name.startswith("_")


def _symbol_id(path: str, qualified_name: str, kind: str, start_byte: int) -> str:
    raw = f"{path}\x00{qualified_name}\x00{kind}\x00{start_byte}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:20]


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── parser ───────────────────────────────────────────────────────────────


class SymbolParser:
    """Extract symbols / imports / calls / edges from source text."""

    def __init__(self, config: Optional[L0Config] = None) -> None:
        self.config = config or L0Config()
        self._parsers: dict[str, Any] = {}

    # ── public API ───────────────────────────────────────────────────────
    def parse_source(
        self,
        source: str | bytes,
        path: str,
        language: Optional[str] = None,
    ) -> ParsedFile:
        data = source.encode("utf-8") if isinstance(source, str) else source
        spec = self._resolve_spec(path, language)
        parser = self._parser_for(spec)

        tree = parser.parse(data)
        root = tree.root_node

        parsed = ParsedFile(
            path=path,
            language=spec.name,
            source_hash=_hash_bytes(data),
            bytes=len(data),
            lines=data.count(b"\n") + 1,
        )

        if root.has_error:
            # Record but keep going: partial trees still yield useful symbols.
            parsed.errors.append("syntax_error")

        self._collect(root, data, spec, parsed, path)
        self._resolve_edges(parsed)
        return parsed

    def parse_file(self, path: str | Path) -> ParsedFile:
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise ParseError(f"cannot read {p}: {exc}", detail={"path": str(p)}) from exc
        rel = str(p)
        return self.parse_source(data, rel)

    # ── internals ────────────────────────────────────────────────────────
    def _resolve_spec(self, path: str, language: Optional[str]) -> LanguageSpec:
        if language:
            from .languages import get_spec

            return get_spec(language)
        spec = spec_for_path(path)
        if spec is None:
            raise UnsupportedLanguageError(
                f"no grammar for extension of {path!r}",
                detail={"path": path},
            )
        return spec

    def _parser_for(self, spec: LanguageSpec) -> Any:
        if spec.name not in self._parsers:
            self._parsers[spec.name] = make_parser(spec)
        return self._parsers[spec.name]

    def _collect(
        self,
        root: Any,
        src: bytes,
        spec: LanguageSpec,
        parsed: ParsedFile,
        path: str,
    ) -> None:
        """Walk the tree once, emitting symbols, imports and call sites."""
        # stack of (node, parent_symbol_id, qualified_prefix)
        stack: list[tuple[Any, Optional[str], str]] = [(root, None, "")]
        seen_defs: set[int] = set()

        while stack:
            node, parent_id, prefix = stack.pop()
            ntype = node.type

            if ntype in spec.import_kinds:
                imp = self._parse_import(node, src, spec)
                if imp is not None:
                    parsed.imports.append(imp)
                    parsed.edges.append(
                        Edge(src=parsed.path, dst=imp.module, kind="imports", line=imp.line)
                    )

            if ntype in spec.call_kinds:
                call = self._parse_call(node, src, spec, parent_id)
                if call is not None:
                    parsed.calls.append(call)

            kind = spec.definition_kinds.get(ntype)
            if kind is not None and id(node) not in seen_defs:
                # ``decorated_definition`` wraps the real definition; unwrap it.
                target = node
                if kind == "decorated":
                    inner = self._unwrap_decorated(node, spec)
                    if inner is not None:
                        target = inner
                        kind = spec.definition_kinds.get(inner.type, "function")
                    else:
                        kind = "function"

                sym = self._build_symbol(target, src, spec, kind, parent_id, prefix, path)
                if sym is not None:
                    seen_defs.add(id(node))
                    seen_defs.add(id(target))
                    parsed.symbols.append(sym)
                    if parent_id is not None:
                        parsed.edges.append(
                            Edge(src=parent_id, dst=sym.symbol_id, kind="contains", line=sym.start_line)
                        )
                    # recurse into the body with this symbol as parent
                    body = _node_field(target, spec.body_field)
                    child_prefix = sym.qualified_name
                    if body is not None:
                        for child in reversed(body.children):
                            stack.append((child, sym.symbol_id, child_prefix))
                    # also walk decorators / bases for inheritance edges
                    self._collect_inheritance(target, src, spec, sym, parsed)
                    continue

            for child in reversed(node.children):
                stack.append((child, parent_id, prefix))

    def _unwrap_decorated(self, node: Any, spec: LanguageSpec) -> Optional[Any]:
        for child in node.children:
            if child.type in spec.definition_kinds and child.type != "decorated_definition":
                return child
        return None

    def _build_symbol(
        self,
        node: Any,
        src: bytes,
        spec: LanguageSpec,
        kind: str,
        parent_id: Optional[str],
        prefix: str,
        path: str,
    ) -> Optional[Symbol]:
        name_node = _node_field(node, spec.name_field)
        if name_node is None:
            # arrow functions / anonymous defs: derive a name from the parent
            name = self._infer_name(node, src, spec)
            if not name:
                return None
        else:
            name = _text(name_node, src).strip()

        if not name:
            return None

        qualified = f"{prefix}.{name}" if prefix else name
        start_byte, end_byte = node.start_byte, node.end_byte
        symbol_id = _symbol_id(path, qualified, kind, start_byte)

        signature = self._signature(node, src, spec)
        docstring = self._docstring(node, src, spec)
        params = self._params(node, src, spec)
        return_type = self._return_type(node, src, spec)
        decorators = self._decorators(node, src, spec)
        is_async = self._is_async(node, src)

        body = _node_field(node, spec.body_field)
        complexity = _count_branches(body) if body is not None else 1

        snippet = ""
        if self.config.store_source_snippets:
            snippet = _text(node, src)[: self.config.snippet_max_chars]

        return Symbol(
            symbol_id=symbol_id,
            name=name,
            qualified_name=qualified,
            kind=kind,
            language=spec.name,
            path=path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=start_byte,
            end_byte=end_byte,
            signature=signature,
            docstring=docstring,
            parent_id=parent_id,
            is_public=_is_public(name, spec.name),
            is_async=is_async,
            decorators=decorators,
            return_type=return_type,
            params=params,
            complexity=complexity,
            snippet=snippet,
        )

    def _infer_name(self, node: Any, src: bytes, spec: LanguageSpec) -> str:
        parent = node.parent
        if parent is None:
            return ""
        # ``const foo = () => {}`` / ``foo = function() {}``
        if parent.type in {"variable_declarator", "assignment", "pair", "field_definition"}:
            for field in ("name", "left", "key"):
                cand = _node_field(parent, field)
                if cand is not None:
                    return _text(cand, src).strip()
        return ""

    def _signature(self, node: Any, src: bytes, spec: LanguageSpec) -> str:
        """Render a one-line signature: ``name(params) -> ret``."""
        name_node = _node_field(node, spec.name_field)
        name = _text(name_node, src).strip() if name_node is not None else self._infer_name(node, src, spec)
        params_node = _node_field(node, spec.params_field)
        params = _collapse_ws(_text(params_node, src)) if params_node is not None else "()"
        ret = self._return_type(node, src, spec)

        sig = f"{name}{params}"
        if ret:
            sig += f" -> {ret}"
        if len(sig) > self.config.max_signature_chars:
            sig = sig[: self.config.max_signature_chars - 1] + "…"
        return sig

    def _params(self, node: Any, src: bytes, spec: LanguageSpec) -> tuple[str, ...]:
        params_node = _node_field(node, spec.params_field)
        if params_node is None:
            return ()
        out: list[str] = []
        for child in params_node.named_children:
            if child.type in {"comment", "(", ")", ","}:
                continue
            text = _collapse_ws(_text(child, src))
            if not text:
                continue
            # strip a trailing default value for a cleaner param list
            text = text.split("=", 1)[0].strip()
            out.append(text)
        return tuple(out)

    def _return_type(self, node: Any, src: bytes, spec: LanguageSpec) -> str:
        ret = _node_field(node, spec.return_field)
        if ret is None:
            return ""
        return _collapse_ws(_text(ret, src))

    def _decorators(self, node: Any, src: bytes, spec: LanguageSpec) -> tuple[str, ...]:
        out: list[str] = []
        parent = node.parent
        if parent is not None and parent.type == "decorated_definition":
            for child in parent.children:
                if child.type == "decorator":
                    out.append(_collapse_ws(_text(child, src)).lstrip("@"))
        # python: decorators may also be direct children of the definition
        for child in node.children:
            if child.type == "decorator":
                out.append(_collapse_ws(_text(child, src)).lstrip("@"))
        return tuple(out)

    def _is_async(self, node: Any, src: bytes) -> bool:
        for child in node.children:
            if child.type == "async":
                return True
        head = _text(node, src)[:20]
        return head.startswith("async ")

    def _docstring(self, node: Any, src: bytes, spec: LanguageSpec) -> str:
        body = _node_field(node, spec.body_field)
        if body is None:
            return ""
        for child in body.named_children:
            if child.type in spec.docstring_kinds:
                text = _text(child, src)
                return self._clean_docstring(text, spec)[: self.config.max_docstring_chars]
            # first non-doc statement ends the search
            if child.type not in {"comment", "line_comment", "block_comment"}:
                break
        # fall back to a leading comment block directly above the definition
        return self._leading_comment(node, src, spec)

    def _clean_docstring(self, text: str, spec: LanguageSpec) -> str:
        stripped = text.strip()
        for quote in ('"""', "'''", '"', "'", "`"):
            if stripped.startswith(quote) and stripped.endswith(quote) and len(stripped) >= 2 * len(quote):
                stripped = stripped[len(quote) : -len(quote)]
                break
        lines = [ln.strip() for ln in stripped.splitlines()]
        return "\n".join(ln for ln in lines if ln).strip()

    def _leading_comment(self, node: Any, src: bytes, spec: LanguageSpec) -> str:
        prev = node.prev_sibling
        collected: list[str] = []
        while prev is not None and prev.type in {"comment", "line_comment", "block_comment"}:
            collected.append(_text(prev, src))
            prev = prev.prev_sibling
        if not collected:
            return ""
        collected.reverse()
        cleaned = []
        for raw in collected:
            line = raw.strip()
            for prefix in ("///", "//!", "//", "#", "/*", "*/", "*"):
                if line.startswith(prefix):
                    line = line[len(prefix) :].strip()
            if line:
                cleaned.append(line)
        return "\n".join(cleaned)[: self.config.max_docstring_chars]

    def _parse_import(self, node: Any, src: bytes, spec: LanguageSpec) -> Optional[Import]:
        text = _collapse_ws(_text(node, src))
        line = node.start_point[0] + 1
        if spec.name == "python":
            return self._parse_python_import(node, src, line)
        if spec.name in {"javascript", "typescript"}:
            return self._parse_js_import(node, src, line)
        if spec.name == "go":
            return self._parse_go_import(node, src, line)
        if spec.name == "rust":
            return self._parse_rust_import(node, src, line)
        if spec.name == "java":
            return self._parse_java_import(node, src, line)
        return Import(module=text, names=(), line=line)

    def _parse_python_import(self, node: Any, src: bytes, line: int) -> Import:
        text = _collapse_ws(_text(node, src))
        if text.startswith("from "):
            rest = text[5:]
            module, _, names_part = rest.partition(" import ")
            names = tuple(
                n.strip().split(" as ")[0].strip()
                for n in names_part.split(",")
                if n.strip()
            )
            return Import(
                module=module.strip(),
                names=names,
                line=line,
                is_relative=module.strip().startswith("."),
            )
        # import a, b as c
        names = []
        for part in text[len("import ") :].split(","):
            part = part.strip()
            if not part:
                continue
            names.append(part.split(" as ")[0].strip())
        return Import(module=names[0] if names else text, names=tuple(names), line=line)

    def _parse_js_import(self, node: Any, src: bytes, line: int) -> Import:
        text = _collapse_ws(_text(node, src))
        m = re.search(r"from\s+['\"]([^'\"]+)['\"]", text)
        module = m.group(1) if m else ""
        names: list[str] = []
        brace = re.search(r"\{([^}]*)\}", text)
        if brace:
            names = [n.strip().split(" as ")[0].strip() for n in brace.group(1).split(",") if n.strip()]
        default = re.match(r"import\s+([A-Za-z_$][\w$]*)", text)
        if default:
            names.insert(0, default.group(1))
        return Import(module=module, names=tuple(names), line=line, is_relative=module.startswith("."))

    def _parse_go_import(self, node: Any, src: bytes, line: int) -> Import:
        text = _collapse_ws(_text(node, src))
        mods = re.findall(r'"([^"]+)"', text)
        module = mods[0] if mods else text
        return Import(module=module, names=tuple(mods), line=line)

    def _parse_rust_import(self, node: Any, src: bytes, line: int) -> Import:
        text = _collapse_ws(_text(node, src))
        body = text[len("use ") :].rstrip(";") if text.startswith("use ") else text
        names = tuple(p.strip() for p in body.split("::") if p.strip())
        return Import(module=body, names=names, line=line)

    def _parse_java_import(self, node: Any, src: bytes, line: int) -> Import:
        text = _collapse_ws(_text(node, src)).rstrip(";")
        module = text[len("import ") :].strip() if text.startswith("import ") else text
        return Import(module=module, names=(module.rsplit(".", 1)[-1],), line=line)

    def _parse_call(
        self,
        node: Any,
        src: bytes,
        spec: LanguageSpec,
        caller_id: Optional[str],
    ) -> Optional[CallSite]:
        fn = _node_field(node, "function")
        if fn is None:
            # go / java use different field names
            for field in ("function", "name", "constructor", "method"):
                fn = _node_field(node, field)
                if fn is not None:
                    break
        if fn is None:
            return None

        receiver = ""
        callee = ""
        is_method = False

        if fn.type in {"attribute", "member_expression", "field_expression", "selector_expression"}:
            obj = _node_field(fn, "object") or _node_field(fn, "value")
            attr = _node_field(fn, "attribute") or _node_field(fn, "property") or _node_field(fn, "field")
            receiver = _collapse_ws(_text(obj, src)) if obj is not None else ""
            callee = _text(attr, src).strip() if attr is not None else _text(fn, src).strip()
            is_method = True
        elif fn.type in {"identifier", "type_identifier", "scoped_identifier", "field_identifier"}:
            callee = _text(fn, src).strip()
        else:
            callee = _collapse_ws(_text(fn, src))

        if not callee:
            return None
        # normalise ``self.foo`` / ``this.foo`` to the bare method name
        if is_method and receiver in {"self", "this", "cls"}:
            pass
        return CallSite(
            callee=callee,
            line=node.start_point[0] + 1,
            caller_id=caller_id,
            is_method=is_method,
            receiver=receiver,
        )

    def _collect_inheritance(
        self,
        node: Any,
        src: bytes,
        spec: LanguageSpec,
        sym: Symbol,
        parsed: ParsedFile,
    ) -> None:
        if sym.kind not in {"class", "interface", "struct", "trait", "impl"}:
            return
        bases: list[str] = []
        for field in ("superclasses", "bases", "interfaces", "superclass"):
            cand = _node_field(node, field)
            if cand is not None:
                for child in cand.named_children:
                    text = _collapse_ws(_text(child, src))
                    if text:
                        bases.append(text)
        # python: argument_list after the class name
        if spec.name == "python":
            for child in node.children:
                if child.type == "argument_list":
                    for arg in child.named_children:
                        text = _collapse_ws(_text(arg, src))
                        if text:
                            bases.append(text)
        for base in bases:
            parsed.edges.append(
                Edge(src=sym.symbol_id, dst=base, kind="inherits", line=sym.start_line)
            )

    def _resolve_edges(self, parsed: ParsedFile) -> None:
        """Turn call sites into ``calls`` edges against known symbols."""
        by_name: dict[str, list[Symbol]] = {}
        for sym in parsed.symbols:
            by_name.setdefault(sym.name, []).append(sym)

        for call in parsed.calls:
            if call.caller_id is None:
                continue
            targets = by_name.get(call.callee)
            if not targets:
                # unresolved: keep a dangling edge to the bare name so the
                # repo map can still see the dependency
                parsed.edges.append(
                    Edge(src=call.caller_id, dst=call.callee, kind="calls", line=call.line, weight=0.5)
                )
                continue
            for target in targets:
                if target.symbol_id == call.caller_id:
                    continue
                parsed.edges.append(
                    Edge(src=call.caller_id, dst=target.symbol_id, kind="calls", line=call.line)
                )


__all__ = [
    "Symbol",
    "Import",
    "CallSite",
    "Edge",
    "ParsedFile",
    "SymbolParser",
]

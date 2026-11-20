"""L0 AST Tools — structural code editing gated by syntax verification and lint.

Integrates tree-sitter AST surgical modifications with the L0 symbol index
and :class:`Linter.gate` to guarantee every edit is valid, lint-clean,
and automatically re-indexed or rolled back on failure.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from .config import L0Config
from .errors import L0Error, ParseError, SymbolNotFoundError
from .lint import Linter
from .store import L0Store

PY_LANG = Language(tspython.language())
_PARSER = Parser(PY_LANG)


@dataclass
class EditResult:
    """Outcome of an AST edit operation."""

    ok: bool = True
    changed: bool = False
    reindexed: bool = False
    rolled_back: bool = False
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _parse(source: str):
    return _PARSER.parse(bytes(source, "utf-8")).root_node


def _reparse_ok(source: str) -> bool:
    root = _parse(source)
    return not root.has_error


def _iter_top_defs(node):
    for child in node.children:
        if child.type == "decorated_definition":
            inner = next(
                (c for c in child.children if c.type in ("function_definition", "class_definition")),
                None,
            )
            if inner is not None:
                yield child, inner
        elif child.type in ("function_definition", "class_definition"):
            yield child, child


def _ident_text(node) -> Optional[str]:
    ident = node.child_by_field_name("name")
    if ident is not None:
        return ident.text.decode("utf-8")
    return None


def _find_def(root, symbol_name: str):
    parts = symbol_name.split(".")
    head = parts[0]

    def _search_class(class_node, remaining: list[str]):
        block = class_node.child_by_field_name("body")
        if block is None:
            return None
        head = remaining[0]
        for span, inner in _iter_top_defs(block):
            name = _ident_text(inner)
            if name == head:
                if len(remaining) == 1:
                    return span, inner
                if inner.type == "class_definition":
                    res = _search_class(inner, remaining[1:])
                    if res is not None:
                        return res
        return None

    for span, inner in _iter_top_defs(root):
        name = _ident_text(inner)
        if name == head:
            if len(parts) == 1:
                return span, inner
            if inner.type == "class_definition":
                return _search_class(inner, parts[1:])
    return None


def _colon_end_byte(def_node) -> int:
    block = def_node.child_by_field_name("body")
    if block is None:
        if def_node.children:
            return def_node.children[-1].start_byte
        return 0
    block_start = block.start_byte
    end = 0
    for ch in def_node.children:
        if ch.start_byte >= block_start and ch.end_byte == block.end_byte:
            return end
        if ch.end_byte > end:
            end = ch.end_byte
    return end


class ASTTools:
    """High-level AST editing operations with syntax verification and rollback."""

    def __init__(self, store: Optional[L0Store] = None, config: Optional[L0Config] = None) -> None:
        self.config = config if config is not None else L0Config.from_env()
        self.store = store
        self.linter = Linter(self.config)

    def _resolve_path(self, rel_or_abs: str | Path) -> Path:
        p = Path(rel_or_abs)
        if p.is_absolute():
            return p
        return Path(self.config.repo_root) / p

    def read_structure(self, path: str | Path) -> dict[str, Any]:
        """Read and inspect the symbols defined in a file."""
        target = self._resolve_path(path)
        if not target.is_file():
            return {"symbol_count": 0, "symbols": []}
        source = target.read_text(encoding="utf-8")
        root = _parse(source)
        symbols = []
        for span, inner in _iter_top_defs(root):
            name = _ident_text(inner)
            if name:
                symbols.append({"name": name, "kind": inner.type})
        return {"symbol_count": len(symbols), "symbols": symbols}

    def _apply_gate_and_save(
        self,
        path: str | Path,
        original: str,
        candidate: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> EditResult:
        target = self._resolve_path(path)
        rel_str = str(path)
        gate_res = self.linter.gate(original, candidate, rel_str)
        if not gate_res.ok or gate_res.rolled_back:
            target.write_text(original, encoding="utf-8")
            return EditResult(
                ok=False,
                changed=False,
                rolled_back=True,
                reason=gate_res.reason or "gate_rejected",
                extra=extra or {},
            )

        target.write_text(gate_res.source, encoding="utf-8")
        reindexed = False
        if self.store is not None:
            try:
                from .indexer import Indexer

                indexer = Indexer(self.store, config=self.config)
                indexer.index_paths([rel_str])
                reindexed = True
            except Exception:
                pass

        return EditResult(
            ok=True,
            changed=(gate_res.source != original),
            reindexed=reindexed,
            rolled_back=False,
            extra=extra or {},
        )

    def edit_function_body(
        self,
        path: str | Path,
        symbol_name: str,
        new_body: str,
    ) -> EditResult:
        target = self._resolve_path(path)
        source = target.read_text(encoding="utf-8")
        root = _parse(source)
        found = _find_def(root, symbol_name)
        if found is None:
            return EditResult(ok=False, reason=f"symbol not found: {symbol_name}")
        span_node, def_node = found
        if def_node.type != "function_definition":
            return EditResult(ok=False, reason=f"{symbol_name} is not a function")
        block = def_node.child_by_field_name("body")
        if block is None:
            return EditResult(ok=False, reason=f"{symbol_name} has no body block")

        body_indent = block.start_point[1]
        dedented = textwrap.dedent(new_body).strip("\n")
        pad = " " * body_indent
        indented_lines = ["" if ln.strip() == "" else pad + ln for ln in dedented.split("\n")]
        indented_body = "\n".join(indented_lines)

        src_bytes = source.encode("utf-8")
        colon_end_byte = _colon_end_byte(def_node)
        new_bytes = (
            src_bytes[:colon_end_byte]
            + b"\n"
            + indented_body.encode("utf-8")
            + src_bytes[block.end_byte :]
        )
        candidate = new_bytes.decode("utf-8", errors="replace")
        return self._apply_gate_and_save(path, source, candidate)

    def update_signature(
        self,
        path: str | Path,
        symbol_name: str,
        new_signature: str,
    ) -> EditResult:
        """Replace the signature / parameters of a function."""
        target = self._resolve_path(path)
        source = target.read_text(encoding="utf-8")
        root = _parse(source)
        found = _find_def(root, symbol_name)
        if found is None:
            return EditResult(ok=False, reason=f"symbol not found: {symbol_name}")
        span_node, def_node = found
        if def_node.type != "function_definition":
            return EditResult(ok=False, reason=f"{symbol_name} is not a function")
        params = def_node.child_by_field_name("parameters")
        if params is None:
            return EditResult(ok=False, reason=f"{symbol_name} has no parameters")

        colon_end = _colon_end_byte(def_node)
        colon_start = colon_end - 1
        sig = new_signature.strip()
        if sig.startswith("def "):
            idx = sig.find("(")
            if idx != -1:
                sig = sig[idx:]
        if sig.endswith(":"):
            sig = sig[:-1].rstrip()

        src_bytes = source.encode("utf-8")
        new_bytes = (
            src_bytes[: params.start_byte]
            + sig.encode("utf-8")
            + src_bytes[colon_start:]
        )
        candidate = new_bytes.decode("utf-8", errors="replace")
        return self._apply_gate_and_save(path, source, candidate)

    def add_symbol(
        self,
        path: str | Path,
        new_source: str,
        parent_name: Optional[str] = None,
    ) -> EditResult:
        """Append a symbol to a class or module."""
        target = self._resolve_path(path)
        source = target.read_text(encoding="utf-8")
        new_src = new_source.strip("\n")

        if parent_name:
            root = _parse(source)
            found = _find_def(root, parent_name)
            if found is None:
                return EditResult(ok=False, reason=f"parent not found: {parent_name}")
            cls_node = found[1]
            cls_indent = cls_node.start_point[1]
            pad = " " * (cls_indent + 4)
            indented = textwrap.indent(new_src, pad)
            src_bytes = source.encode("utf-8")
            insert_pos = cls_node.end_byte
            pre = src_bytes[:insert_pos]
            post = src_bytes[insert_pos:]
            if not pre.endswith(b"\n"):
                pre += b"\n"
            candidate = (pre + indented.encode("utf-8") + post).decode("utf-8", errors="replace")
        else:
            if not source.endswith("\n") and source:
                source += "\n"
            sep = "" if source.endswith("\n\n") else "\n"
            candidate = source + sep + new_src + "\n"

        return self._apply_gate_and_save(path, source, candidate)

    def rename_symbol(
        self,
        path: str | Path,
        old_name: str,
        new_name: str,
        scope: str = "file",
    ) -> EditResult:
        """Rename a function or class definition identifier."""
        target = self._resolve_path(path)
        source = target.read_text(encoding="utf-8")
        root = _parse(source)
        found = _find_def(root, old_name)
        if found is None:
            return EditResult(ok=False, reason=f"symbol not found: {old_name}")
        def_node = found[1]
        ident = None
        for ch in def_node.children:
            if ch.type == "identifier":
                ident = ch
                break
        if ident is None:
            return EditResult(ok=False, reason=f"no identifier on {old_name}")

        src_bytes = source.encode("utf-8")
        new_bytes = (
            src_bytes[: ident.start_byte]
            + new_name.encode("utf-8")
            + src_bytes[ident.end_byte :]
        )
        candidate = new_bytes.decode("utf-8", errors="replace")
        return self._apply_gate_and_save(path, source, candidate, extra={"replacements": 1})

    def remove_symbol(
        self,
        path: str | Path,
        symbol_name: str,
    ) -> EditResult:
        target = self._resolve_path(path)
        source = target.read_text(encoding="utf-8")
        root = _parse(source)
        found = _find_def(root, symbol_name)
        if found is None:
            return EditResult(ok=False, reason=f"symbol not found: {symbol_name}")
        span = found[0]
        src_bytes = source.encode("utf-8")
        start = span.start_byte
        end = span.end_byte
        if end < len(src_bytes) and src_bytes[end : end + 1] == b"\n":
            end += 1
        new_bytes = src_bytes[:start] + src_bytes[end:]
        candidate = new_bytes.decode("utf-8", errors="replace")
        candidate = re.sub(r"\n\n\n+", "\n\n", candidate)
        return self._apply_gate_and_save(path, source, candidate)


__all__ = ["ASTTools", "EditResult"]

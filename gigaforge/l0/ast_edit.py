"""L0 AST-level surgical edits.

Replaces ONLY the region of interest (function body, identifier, signature,
whole symbol) by re-parsing the file fresh each call — never relies on stale
line numbers. After every edit, re-parses to confirm syntactic validity;
raises ``ASTEditError`` if the edit produced invalid Python.

No ruff here — that's the pipeline's job. We only keep the file syntactically
valid Python.
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from gigaforge.client.errors import GigaForgeError

PY_LANG = Language(tspython.language())
_PARSER = Parser(PY_LANG)


class ASTEditError(GigaForgeError):
    """An AST edit produced invalid Python or could not locate the symbol."""


@dataclass(frozen=True)
class EditResult:
    applied: bool
    new_text: str  # full file content after the edit
    symbol_start: int  # 1-indexed line where the symbol (or its decorator) starts
    symbol_end: int  # 1-indexed line where the symbol ends


def _parse(source: str):
    return _PARSER.parse(bytes(source, "utf8")).root_node


def _reparse_ok(source: str) -> bool:
    root = _parse(source)
    return not root.has_error


# --- node finders -----------------------------------------------------------


def _iter_top_defs(node):
    """Yield (decorated_or_def, inner_def) for every function/class at any
    nesting we care about (top-level + nested-in-class)."""
    for child in node.children:
        if child.type == "decorated_definition":
            inner = child.child_by_field_name("definition") or next(
                (c for c in child.children if c.type in ("function_definition", "class_definition")),
                None,
            )
            if inner is not None:
                yield child, inner
        elif child.type in ("function_definition", "class_definition"):
            yield child, child


def _find_def(root, symbol_name: str):
    """Locate a function/class node by name. Supports `Class.method` qualified
    names by descending into the class. Returns (span_node, def_node) tuple,
    where span_node is the decorated_definition (if any) or the def itself.
    """
    parts = symbol_name.split(".")

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

    head = parts[0]
    for span, inner in _iter_top_defs(root):
        name = _ident_text(inner)
        if name == head:
            if len(parts) == 1:
                return span, inner
            if inner.type == "class_definition":
                res = _search_class(inner, parts[1:])
                if res is not None:
                    return res
    return None


def _ident_text(node) -> str | None:
    for ch in node.children:
        if ch.type == "identifier":
            return ch.text.decode("utf-8")
    return None


# --- edits ------------------------------------------------------------------


def edit_function_body(path: Path, symbol_name: str, new_body: str) -> EditResult:
    """Replace ONLY the body of the named function/method.

    - Re-parses the file to find the symbol fresh (no stale line numbers).
    - Dedents ``new_body`` (textwrap.dedent) then re-indents to the function's
      body indent level (column of the block child).
    - No echo of old code; no other function's body touched.
    """
    path = Path(path)
    source = path.read_text(encoding="utf-8")
    root = _parse(source)

    found = _find_def(root, symbol_name)
    if found is None:
        raise ASTEditError(f"symbol not found: {symbol_name}")
    span_node, def_node = found
    if def_node.type != "function_definition":
        raise ASTEditError(f"{symbol_name} is not a function (kind={def_node.type})")

    block = def_node.child_by_field_name("body")
    if block is None:
        raise ASTEditError(f"{symbol_name} has no body block")

    body_indent = block.start_point[1]

    # Normalize the incoming body: dedent, strip outer blank lines, re-indent.
    dedented = textwrap.dedent(new_body).strip("\n")
    pad = " " * body_indent
    indented_lines = []
    for ln in dedented.split("\n"):
        if ln.strip() == "":
            indented_lines.append("")  # preserve blank lines (ruff strips them later)
        else:
            indented_lines.append(pad + ln)
    indented_body = "\n".join(indented_lines)

    # Replace from the end of `:` token to the end of the block (i.e. body region).
    # We construct: prefix (up to and including colon) + "\n" + indented_body + suffix.
    # NOTE: tree-sitter offsets are BYTE offsets — slice bytes, then decode.
    src_bytes = source.encode("utf-8")
    colon_end_byte = _colon_end_byte(def_node)
    new_bytes = (
        src_bytes[:colon_end_byte]
        + b"\n"
        + indented_body.encode("utf-8")
        + src_bytes[block.end_byte :]
    )
    new_source = new_bytes.decode("utf-8", errors="replace")

    if not _reparse_ok(new_source):
        raise ASTEditError(f"edit produced invalid Python for {symbol_name}")

    path.write_text(new_source, encoding="utf-8")

    return EditResult(
        applied=True,
        new_text=new_source,
        symbol_start=span_node.start_point[0] + 1,
        symbol_end=_recalc_symbol_end(new_source, symbol_name, span_node.start_point[0] + 1),
    )


def _colon_end_byte(def_node) -> int:
    """Return the end_byte of the `:` token that introduces the def/class body.

    The body block is the LAST field-named child; everything before it (def,
    name, params, `:`) ends at the colon's end_byte. We compare by byte range
    rather than `is` because tree-sitter may return distinct node objects for
    field access vs. iteration.
    """
    block = def_node.child_by_field_name("body")
    if block is None:
        # fallback: last child is the block
        return def_node.children[-1].start_byte if def_node.children else 0
    block_start = block.start_byte
    end = 0
    for ch in def_node.children:
        if ch.start_byte >= block_start and ch.end_byte == block.end_byte:
            break
        if ch.end_byte > end:
            end = ch.end_byte
    return end


def _recalc_symbol_end(new_source: str, symbol_name: str, start_line_1: int) -> int:
    """After an edit, re-parse and compute the new end_line for the symbol."""
    root = _parse(new_source)
    found = _find_def(root, symbol_name)
    if found is None:
        return start_line_1
    span, _ = found
    return span.end_point[0] + 1


def rename_symbol(path: Path, old_name: str, new_name: str) -> EditResult:
    """Rename a function/class definition's identifier. Does NOT touch
    references — those are the Coder/diff's responsibility (callers analysis
    is a separate concern)."""
    path = Path(path)
    source = path.read_text(encoding="utf-8")
    root = _parse(source)
    found = _find_def(root, old_name)
    if found is None:
        raise ASTEditError(f"symbol not found: {old_name}")
    span, def_node = found

    ident = None
    for ch in def_node.children:
        if ch.type == "identifier":
            ident = ch
            break
    if ident is None:
        raise ASTEditError(f"no identifier on {old_name}")

    # NOTE: tree-sitter offsets are BYTE offsets — slice bytes, then decode.
    src_bytes = source.encode("utf-8")
    new_bytes = (
        src_bytes[: ident.start_byte] + new_name.encode("utf-8") + src_bytes[ident.end_byte :]
    )
    new_source = new_bytes.decode("utf-8", errors="replace")
    if not _reparse_ok(new_source):
        raise ASTEditError(f"rename produced invalid Python for {old_name}")
    path.write_text(new_source, encoding="utf-8")
    return EditResult(
        applied=True,
        new_text=new_source,
        symbol_start=span.start_point[0] + 1,
        symbol_end=span.end_point[0] + 1,
    )


def update_signature(path: Path, symbol_name: str, new_signature: str) -> EditResult:
    """Replace the parameter list of a function with ``new_signature``.

    ``new_signature`` should be the full params incl. parens, e.g. ``(a, b, c)``.
    If the user passes ``def foo(a, b)`` we extract the parens-span.
    """
    path = Path(path)
    source = path.read_text(encoding="utf-8")
    root = _parse(source)
    found = _find_def(root, symbol_name)
    if found is None:
        raise ASTEditError(f"symbol not found: {symbol_name}")
    span, def_node = found
    if def_node.type != "function_definition":
        raise ASTEditError(f"{symbol_name} is not a function")

    params = def_node.child_by_field_name("parameters")
    if params is None:
        raise ASTEditError(f"{symbol_name} has no parameters node")

    sig = new_signature.strip()
    # Accept either "def name(a, b)" or just "(a, b)"
    if sig.startswith("def "):
        # take everything from first '(' to matching ')'
        i = sig.find("(")
        if i == -1:
            raise ASTEditError("new_signature with 'def ' must contain '('")
        sig = sig[i:]
    if not (sig.startswith("(") and sig.endswith(")")):
        raise ASTEditError(
            f"new_signature must be '(params)' form; got: {new_signature!r}"
        )

    # NOTE: tree-sitter offsets are BYTE offsets — slice bytes, then decode.
    src_bytes = source.encode("utf-8")
    new_bytes = src_bytes[: params.start_byte] + sig.encode("utf-8") + src_bytes[params.end_byte :]
    new_source = new_bytes.decode("utf-8", errors="replace")
    if not _reparse_ok(new_source):
        raise ASTEditError(f"signature update produced invalid Python for {symbol_name}")
    path.write_text(new_source, encoding="utf-8")
    return EditResult(
        applied=True,
        new_text=new_source,
        symbol_start=span.start_point[0] + 1,
        symbol_end=span.end_point[0] + 1,
    )


def add_symbol(path: Path, parent_name_or_None: str | None, new_source: str) -> EditResult:
    """Append ``new_source`` (a def/class) to the file. If ``parent_name`` is
    given, append inside that class body at the right indentation."""
    path = Path(path)
    src = path.read_text(encoding="utf-8")
    root = _parse(src)
    new_src = new_source.strip("\n")

    if parent_name_or_None:
        found = _find_def(root, parent_name_or_None)
        if found is None:
            raise ASTEditError(f"parent class not found: {parent_name_or_None}")
        _, cls_node = found
        if cls_node.type != "class_definition":
            raise ASTEditError(f"{parent_name_or_None} is not a class")
        block = cls_node.child_by_field_name("body")
        if block is None:
            raise ASTEditError(f"{parent_name_or_None} has no body")
        # indentation = column of first child of block, or class indent + 4
        cls_indent = cls_node.start_point[1]
        body_indent = cls_indent + 4
        # Check if we can detect an existing child indent
        for ch in block.children:
            if ch.type in ("function_definition", "class_definition", "decorated_definition"):
                body_indent = ch.start_point[1]
                break
        pad = " " * body_indent
        indented = textwrap.indent(new_src, pad)
        # NOTE: tree-sitter offsets are BYTE offsets — slice bytes, then decode.
        src_bytes = src.encode("utf-8")
        # insert at end of class body (i.e., at end of cls_node)
        insert_pos = cls_node.end_byte
        pre = src_bytes[:insert_pos]
        post = src_bytes[insert_pos:]
        # Ensure newline before the new symbol (after the last block line)
        if not pre.endswith(b"\n"):
            pre = pre + b"\n"
        new_bytes_full = pre + indented.encode("utf-8") + post
        new_full = new_bytes_full.decode("utf-8", errors="replace")
        # Re-parse to find symbol span
        if not _reparse_ok(new_full):
            raise ASTEditError(f"add_symbol produced invalid Python inside {parent_name_or_None}")
        path.write_text(new_full, encoding="utf-8")
        new_root = _parse(new_full)
        new_found = _find_def(new_root, parent_name_or_None)
        # find the new child by scanning children
        new_end = new_found[0].end_point[0] + 1 if new_found else 1
        # locate the added symbol start: it's the line just before new_end at the end
        # Simpler: re-parse and find by name from the dedented new_source
        new_name = _extract_def_name(new_src)
        if new_name:
            inner_found = _find_def_in_class(new_root, parent_name_or_None, new_name)
            if inner_found:
                return EditResult(True, new_full, inner_found[0].start_point[0] + 1, inner_found[0].end_point[0] + 1)
        return EditResult(True, new_full, 1, new_end)

    # top-level: append at end of file (after trailing newline)
    if not src.endswith("\n") and src:
        src = src + "\n"
    # add a blank separator line if needed
    if src and not src.endswith("\n\n"):
        new_full = src + "\n" + new_src + "\n"
    else:
        new_full = src + new_src + "\n"
    if not _reparse_ok(new_full):
        raise ASTEditError("add_symbol produced invalid Python at module level")
    path.write_text(new_full, encoding="utf-8")
    new_root = _parse(new_full)
    new_name = _extract_def_name(new_src)
    if new_name:
        for span, inner in _iter_top_defs(new_root):
            if _ident_text(inner) == new_name:
                return EditResult(True, new_full, span.start_point[0] + 1, span.end_point[0] + 1)
    return EditResult(True, new_full, 1, 1)


def _find_def_in_class(root, parent_name: str, target_name: str):
    """Find a direct child def named ``target_name`` inside the class
    ``parent_name``."""
    # Find the class first
    cls_found = _find_def(root, parent_name)
    if cls_found is None or cls_found[1].type != "class_definition":
        return None
    cls_node = cls_found[1]
    block = cls_node.child_by_field_name("body")
    if block is None:
        return None
    for span, inner in _iter_top_defs(block):
        if _ident_text(inner) == target_name:
            return (span, inner)
    return None


def _extract_def_name(src: str) -> str | None:
    """Best-effort: parse a tiny snippet to extract its def/class name."""
    root = _parse(src)
    for _span, inner in _iter_top_defs(root):
        nm = _ident_text(inner)
        if nm:
            return nm
    # fallback: regex
    m = re.search(r"^\s*(?:async\s+def|def|class)\s+(\w+)", src, re.MULTILINE)
    return m.group(1) if m else None


def remove_symbol(path: Path, symbol_name: str) -> EditResult:
    """Remove a function/class (and its decorator, if any) by name."""
    path = Path(path)
    source = path.read_text(encoding="utf-8")
    root = _parse(source)
    found = _find_def(root, symbol_name)
    if found is None:
        raise ASTEditError(f"symbol not found: {symbol_name}")
    span, _ = found

    # Remove from span.start_byte to span.end_byte, plus one trailing newline if present.
    # NOTE: tree-sitter offsets are BYTE offsets — slice bytes, then decode.
    src_bytes = source.encode("utf-8")
    start = span.start_byte
    end = span.end_byte
    # Extend end to swallow a single trailing newline
    if end < len(src_bytes) and src_bytes[end : end + 1] == b"\n":
        end += 1
        # swallow preceding blank line(s) (cosmetic)
    # also swallow leading blank line(s)? keep simple: just remove the symbol region.
    new_bytes = src_bytes[:start] + src_bytes[end:]
    new_source = new_bytes.decode("utf-8", errors="replace")
    # LOW-03: collapse triple-or-more blank lines left behind by removal into
    # the PEP-8 maximum of two (a single blank separator between blocks).
    # ``\n\n\n+`` -> ``\n\n``. Idempotent on already-clean text.
    new_source = re.sub(r"\n\n\n+", "\n\n", new_source)
    if not _reparse_ok(new_source):
        raise ASTEditError(f"remove_symbol produced invalid Python for {symbol_name}")
    path.write_text(new_source, encoding="utf-8")
    # symbol no longer exists; return line where it WAS
    return EditResult(
        applied=True,
        new_text=new_source,
        symbol_start=span.start_point[0] + 1,
        symbol_end=span.end_point[0] + 1,
    )

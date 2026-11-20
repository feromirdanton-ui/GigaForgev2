"""Symbol extraction for markup: HTML, CSS and JSON-ish config.

tree-sitter has no grammar for these in this build, so a web project produced an
empty index: no symbols, no repo map, no slice — the coder had to be handed whole
files.  A page still *has* structure worth indexing: ids, classes, headings, the
functions declared in its inline ``<script>``, and the selectors a stylesheet
defines.  Those are what a goal usually names ("the login button", "the hero
section"), so they are extracted by a small, dependency-free scanner and stored as
ordinary symbols of kind ``markup``.

The scanner is deliberately shallow: it never claims to parse HTML.  It reports
what it can prove from a line, which is exactly the level of precision a slice
needs to pick the right file and quote the right region.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

__all__ = ["MarkupSymbol", "MARKUP_EXTENSIONS", "extract_markup_symbols"]

MARKUP_EXTENSIONS: tuple[str, ...] = (".html", ".htm", ".xhtml", ".css", ".svg")

_ID_RE = re.compile(r"""\bid\s*=\s*["']([A-Za-z][\w:.-]{1,60})["']""")
_CLASS_RE = re.compile(r"""\bclass\s*=\s*["']([^"']{1,200})["']""")
_HEADING_RE = re.compile(r"<h(?P<level>[1-6])[^>]*>(?P<text>[^<]{2,120})</h\d>",
                         re.IGNORECASE)
_TAG_RE = re.compile(r"<(?P<tag>section|header|footer|nav|main|form|article|aside|dialog)"
                     r"[^>]*\bid\s*=\s*[\"'](?P<id>[\w:.-]+)[\"']", re.IGNORECASE)
_SCRIPT_FN_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
_JS_CONST_RE = re.compile(r"\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|function)")
_SELECTOR_RE = re.compile(r"(?m)^\s*(?P<sel>[.#][A-Za-z][\w:.#\- ]{0,80})\s*\{")
_CSS_VAR_RE = re.compile(r"(?m)^\s*(?P<name>--[a-z0-9-]{2,40})\s*:")


@dataclass(frozen=True)
class MarkupSymbol:
    """One indexable region of a markup file."""

    name: str
    kind: str
    line: int
    signature: str = ""
    doc: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "kind": self.kind, "line": self.line,
                "signature": self.signature, "doc": self.doc}


def extract_markup_symbols(source: str, path: str = "") -> List[MarkupSymbol]:
    """Return the symbols a markup file exposes, in file order.

    Kinds: ``element`` (id-bearing landmark), ``class``, ``heading``,
    ``function`` / ``const`` (inline script), ``selector`` / ``css-var``.
    """
    suffix = ("." + str(path).rsplit(".", 1)[-1].lower()) if "." in str(path) else ""
    found: dict[tuple[str, int], MarkupSymbol] = {}

    def add(name: str, kind: str, line: int, signature: str = "", doc: str = "") -> None:
        name = (name or "").strip()
        if not name or len(name) > 80:
            return
        key = (name, line)
        if key in found:
            return
        found[key] = MarkupSymbol(name=name, kind=kind, line=line,
                                  signature=signature[:200], doc=doc[:200])

    lines = source.splitlines()
    for number, text in enumerate(lines, start=1):
        if suffix in {".css"}:
            for match in _SELECTOR_RE.finditer(text):
                selector = match.group("sel").strip()
                add(selector, "selector", number, signature=selector)
            for match in _CSS_VAR_RE.finditer(text):
                add(match.group("name"), "css-var", number)
            continue

        for match in _TAG_RE.finditer(text):
            add(match.group("id"), "element", number,
                signature=f"<{match.group('tag').lower()}>")
        for match in _ID_RE.finditer(text):
            add(match.group(1), "element", number)
        for match in _CLASS_RE.finditer(text):
            for token in match.group(1).split():
                if token and not token.startswith("{"):
                    add(token, "class", number)
        for match in _HEADING_RE.finditer(text):
            level = match.group("level")
            add(match.group("text").strip(), f"heading{level}", number,
                signature=f"<h{level}>")
        for match in _SCRIPT_FN_RE.finditer(text):
            add(match.group(1), "function", number, signature="function")
        for match in _JS_CONST_RE.finditer(text):
            add(match.group(1), "const", number)

    return [found[key] for key in sorted(found, key=lambda k: (k[1], k[0]))]

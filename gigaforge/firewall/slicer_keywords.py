"""Keyword / path extraction for the Context Slicer — no LLM involved.

The slicer has to answer one question deterministically: *given this task text,
which symbols in the index are worth showing the model?*  A model call to answer
that would defeat the whole point of the firewall, so the signals are extracted
with plain lexical work:

* **identifiers** — ``snake_case``, ``camelCase``, ``PascalCase`` and
  ``dot.paths`` are recognised as single units rather than split on case;
* **paths** — ``src/app.py``, ``./core/engine.ts``, ``gigaforge/l0/store``;
* **quoted strings** — ``"FooBar"``, ``'do_thing'``;
* **backticked symbols** — ```read_structure``` in prose is usually a symbol;
* **CAMEL / SCREAMING_CASE** — constants such as ``MAX_RETRIES``;
* **domain words** — remaining lowercase words, minus stopwords.

Every term carries a :class:`TermKind` and a weight so the slicer can prefer an
exact symbol hit over a generic English word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .config import FirewallConfig

# ── lexical patterns ─────────────────────────────────────────────────────
# A dotted identifier, a snake_case name, or a bare word.  Ordered alternation
# matters: longest, most specific shape first.
_IDENT_RE = re.compile(
    r"(?P<value>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    r"|\b[A-Za-z][A-Za-z0-9_]*[A-Z][A-Za-z0-9_]*\b"
    r"|\b[a-z][a-z0-9_]*_[a-z0-9_]+\b"
    r"|\b[A-Z][A-Z0-9_]*\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*\b)"
)
_PATH_RE = re.compile(
    r"(?P<value>(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+)"
)
_BACKTICK_RE = re.compile(r"`(?P<value>[^`\n]{1,80})`")
_QUOTED_RE = re.compile(r"[\"'](?P<value>[^\"'\n]{1,80})[\"']")
_FILE_HINT_RE = re.compile(r"([\w.-]+)\.([A-Za-z0-9]{1,6})\b")

#: A bare file name mentioned in prose — ``math_utils.py``, ``App.tsx``.  Without
#: this a goal like "add clamp to math_utils.py" produced *no* path signal, so the
#: slicer found nothing, the coder saw no context and re-emitted the whole file as
#: a brand-new one (which then truncated it).  A file name is a path even when the
#: task forgets to write ``./`` in front of it.
_SOURCE_EXTS = (
    "py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt",
    "rb", "cs", "cpp", "cc", "c", "h", "hpp", "php", "swift", "scala", "sh", "ps1",
    "sql", "html", "css", "scss", "vue", "svelte", "md", "json", "ya?ml", "toml",
    "ini", "cfg", "txt",
)
_FILENAME_RE = re.compile(
    r"(?<![\w/.-])(?P<value>[\w.-]+\.(?:" + "|".join(_SOURCE_EXTS) + r"))\b"
)
#: Absolute paths — ``M:\\YESTER\\index.html`` or ``/srv/app/main.py``.  Users
#: paste these straight from a file manager; the slicer must see them as paths even
#: though the repository root is relative.
_ABS_PATH_RE = re.compile(
    r"(?P<value>(?:[A-Za-z]:[\\/]|/)[^\s\"'<>|*?]+?\.[A-Za-z0-9]{1,6})(?![\w.])"
)

#: Token kinds, ordered by retrieval weight (highest first).
KIND_WEIGHTS: dict[str, float] = {
    "path": 3.0,
    "backtick": 2.6,
    "quoted": 2.4,
    "dotted": 2.2,
    "snake": 2.0,
    "camel": 2.0,
    "constant": 1.8,
    "word": 1.0,
}


def _is_camel(value: str) -> bool:
    return any(ch.isupper() for ch in value[1:]) and any(ch.islower() for ch in value)


def _is_snake(value: str) -> bool:
    return "_" in value and value.lower() == value


def _is_constant(value: str) -> bool:
    return value.isupper() and len(value) >= 3 and value.replace("_", "").isalnum()


def _is_dotted(value: str) -> bool:
    return "." in value and not value.endswith((".py", ".ts", ".js", ".go", ".rs", ".java"))


def _classify(value: str) -> str:
    if "/" in value or _FILENAME_RE.fullmatch(value):
        return "path"
    if _is_dotted(value):
        return "dotted"
    if _is_constant(value):
        return "constant"
    if _is_snake(value):
        return "snake"
    if _is_camel(value):
        return "camel"
    return "word"


@dataclass
class Term:
    """One extracted term with its kind and retrieval weight."""

    value: str
    kind: str
    weight: float
    count: int = 1

    @property
    def score(self) -> float:
        return self.weight * (1.0 + 0.15 * min(self.count - 1, 4))

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "kind": self.kind,
            "weight": round(self.weight, 3),
            "count": self.count,
        }


@dataclass
class Extraction:
    """Everything the slicer needs from a task description."""

    terms: list[Term] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)

    @property
    def keywords(self) -> list[str]:
        return [t.value for t in self.terms]

    def to_dict(self) -> dict[str, object]:
        return {
            "terms": [t.to_dict() for t in self.terms],
            "paths": list(self.paths),
            "symbols": list(self.symbols),
            "extensions": list(self.extensions),
        }


class Extractor:
    """Deterministic term extractor for task descriptions."""

    def __init__(self, config: Optional[FirewallConfig] = None) -> None:
        self.config = config or FirewallConfig()
        self._stop = {w.lower() for w in self.config.stopwords}

    # ── public ───────────────────────────────────────────────────────────
    def extract(self, text: str, *, extra: Sequence[str] = ()) -> Extraction:
        """Pull every retrieval signal out of ``text`` (plus any ``extra``)."""
        cfg = self.config
        blob = "\n".join([text, *extra])
        acc: dict[tuple[str, str], Term] = {}
        paths: list[str] = []
        symbols: list[str] = []

        # 1. explicit paths (highest signal, recorded verbatim)
        for match in _PATH_RE.finditer(blob):
            value = match.group("value").lstrip("./")
            if value not in paths:
                paths.append(value)
            self._add(acc, value, "path")

        # 2. backticked spans — a symbol in prose
        for match in _BACKTICK_RE.finditer(blob):
            value = match.group("value").strip()
            if self._usable(value):
                symbols.append(value)
                self._add(acc, value, "backtick")

        # 3. quoted spans — often an expected name or a literal
        for match in _QUOTED_RE.finditer(blob):
            value = match.group("value").strip()
            if self._usable(value) and not any(ch.isspace() for ch in value):
                self._add(acc, value, "quoted")

        # 4. every remaining identifier-shaped token
        for match in _IDENT_RE.finditer(blob):
            value = match.group("value")
            if not self._usable(value):
                continue
            kind = _classify(value)
            self._add(acc, value, kind)
            # a dotted path contributes its head and tail too: ``a.b.Call``
            # should match both the module ``a.b`` and the symbol ``Call``.
            if kind == "dotted":
                head, _, tail = value.rpartition(".")
                if self._usable(head):
                    self._add(acc, head, "dotted")
                if self._usable(tail):
                    self._add(acc, tail, _classify(tail))

        # 4b. bare file names — treated as paths, not as identifiers
        for match in _FILENAME_RE.finditer(blob):
            value = match.group("value").lstrip("./")
            if not value:
                continue
            if value not in paths:
                paths.append(value)
            self._add(acc, value, "path")
            stem = value.rsplit(".", 1)[0]
            if self._usable(stem):
                self._add(acc, stem, "word")

        # 4c. absolute paths, kept verbatim so the reader can open them directly
        for match in _ABS_PATH_RE.finditer(blob):
            value = match.group("value").strip()
            if value and value not in paths:
                paths.append(value)
                self._add(acc, value.replace("\\", "/"), "path")

        # 5. file extensions mentioned in prose
        extensions: list[str] = []
        for match in _FILE_HINT_RE.finditer(blob):
            ext = match.group(2).lower()
            if ext not in extensions:
                extensions.append(ext)

        terms = sorted(acc.values(), key=lambda t: (-t.score, t.value))
        terms = terms[: cfg.slice_max_keywords]
        return Extraction(terms=terms, paths=paths, symbols=symbols, extensions=extensions)

    # ── internals ────────────────────────────────────────────────────────
    def _usable(self, value: str) -> bool:
        v = value.strip()
        if len(v) < self.config.slice_min_keyword_len:
            return False
        if v.lower() in self._stop:
            return False
        if "_" in v and set(v) == {"_"}:
            return False
        if v.isdigit():
            return False
        return True

    @staticmethod
    def _add(acc: dict[tuple[str, str], Term], value: str, kind: str) -> None:
        weight = KIND_WEIGHTS.get(kind, 1.0)
        key = (value, kind)
        existing = acc.get(key)
        if existing is None:
            acc[key] = Term(value=value, kind=kind, weight=weight)
        else:
            existing.count += 1


def keywords(text: str, config: Optional[FirewallConfig] = None) -> list[str]:
    """Convenience wrapper: the ranked keyword list for ``text``."""
    return Extractor(config).extract(text).keywords


__all__ = ["Term", "Extraction", "Extractor", "keywords", "KIND_WEIGHTS"]

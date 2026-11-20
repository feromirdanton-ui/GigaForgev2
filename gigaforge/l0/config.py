"""L0 configuration.

All knobs live here and are overridable from the environment with the
``GF_L0__`` prefix (double underscore = nesting, matching the IGAFORGE
``core.config.Settings`` convention).  Nothing in the L0 layer hardcodes a
path, a binary name or a budget: it is all read from :class:`L0Config`.

Example::

    GF_L0__DB_PATH=.gigaforge/l0.sqlite3
    GF_L0__REPO_MAP_BUDGET_TOKENS=6000
    GF_L0__RUFF_ENABLED=false
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

ENV_PREFIX = "GF_L0__"

DEFAULT_INCLUDE = (
    "**/*.py",
    "**/*.pyi",
    "**/*.js",
    "**/*.jsx",
    "**/*.mjs",
    "**/*.cjs",
    "**/*.ts",
    "**/*.tsx",
    "**/*.go",
    "**/*.rs",
    "**/*.java",
    # Markup: no tree-sitter grammar, but ``l0/markup.py`` extracts ids, classes,
    # headings and inline-script functions.  Without these globs a web project has
    # an empty index, an empty repo map and therefore no slice at all.
    "**/*.html",
    "**/*.htm",
    "**/*.xhtml",
    "**/*.css",
    "**/*.svg",
)

DEFAULT_EXCLUDE = (
    ".git/**",
    ".hg/**",
    ".svn/**",
    ".venv/**",
    "venv/**",
    "env/**",
    "node_modules/**",
    "dist/**",
    "build/**",
    "target/**",
    "__pycache__/**",
    ".mypy_cache/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".gigaforge/**",
    "*.min.js",
    "*.min.css",
    "*.lock",
    "*.pyc",
)

#: Relative importance of each ranking signal in the repo map.
DEFAULT_RANK_WEIGHTS: Mapping[str, float] = {
    "pagerank": 1.0,
    "in_degree": 0.35,
    "public": 0.20,
    "size": 0.15,
    "recency": 0.10,
    "kind": 0.10,
}


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_tuple(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class L0Config:
    """Immutable configuration for the L0 symbol-index layer."""

    # ── storage ──────────────────────────────────────────────────────────
    db_path: str = ".gigaforge/l0.sqlite3"
    repo_root: str = "."
    busy_timeout_ms: int = 5000
    journal_mode: str = "WAL"
    synchronous: str = "NORMAL"

    # ── discovery ────────────────────────────────────────────────────────
    include_globs: tuple[str, ...] = DEFAULT_INCLUDE
    exclude_globs: tuple[str, ...] = DEFAULT_EXCLUDE
    max_file_bytes: int = 1_500_000
    follow_symlinks: bool = False
    languages: tuple[str, ...] = (
        "python",
        "javascript",
        "typescript",
        "go",
        "rust",
        "java",
    )

    # ── parsing ──────────────────────────────────────────────────────────
    fts_tokenizer: str = "unicode61 remove_diacritics 2"
    max_docstring_chars: int = 2000
    max_signature_chars: int = 500
    store_source_snippets: bool = True
    snippet_max_chars: int = 4000

    # ── repo map ─────────────────────────────────────────────────────────
    repo_map_budget_tokens: int = 4000
    chars_per_token: float = 4.0
    pagerank_damping: float = 0.85
    pagerank_iters: int = 30
    pagerank_tol: float = 1e-9
    rank_weights: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_RANK_WEIGHTS)
    )
    repo_map_max_symbols: int = 2000

    # ── lint / format ────────────────────────────────────────────────────
    ruff_enabled: bool = True
    ruff_bin: str = "ruff"
    ruff_timeout_s: float = 20.0
    ruff_select: tuple[str, ...] = ("E", "F", "W")
    #: ruff code prefixes that BLOCK an edit.  These are breakage codes
    #: (E9 = syntax, F82 = undefined names / redefinitions), not style codes:
    #: an edit that leaves an unused import behind is reported but allowed.
    ruff_fatal_select: tuple[str, ...] = ("E9", "F82")
    lint_on_edit: bool = True
    format_on_edit: bool = True
    rollback_on_lint_error: bool = True
    rollback_on_syntax_error: bool = True

    # ── git ──────────────────────────────────────────────────────────────
    git_bin: str = "git"
    git_timeout_s: float = 30.0
    incremental_use_git: bool = True

    # ── misc ─────────────────────────────────────────────────────────────
    tenant: str = "default"
    log_level: str = "INFO"

    # ─────────────────────────────────────────────────────────────────────
    @property
    def db_file(self) -> Path:
        return Path(self.db_path)

    @property
    def root(self) -> Path:
        return Path(self.repo_root).resolve()

    def with_(self, **changes: Any) -> "L0Config":
        """Return a copy with ``changes`` applied (frozen dataclass helper)."""
        return replace(self, **changes)

    # ─────────────────────────────────────────────────────────────────────
    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        **overrides: Any,
    ) -> "L0Config":
        """Build a config from ``GF_L0__*`` environment variables.

        Precedence: explicit ``overrides`` → environment → dataclass defaults.
        """
        source = dict(os.environ if env is None else env)
        values: dict[str, Any] = {}

        for key, raw in source.items():
            if not key.startswith(ENV_PREFIX):
                continue
            name = key[len(ENV_PREFIX) :].lower()
            if name not in cls.__dataclass_fields__:
                continue
            values[name] = _coerce(name, raw, cls.__dataclass_fields__[name].type)

        values.update(overrides)
        return cls(**values)


def _coerce(name: str, raw: str, declared: Any) -> Any:
    """Coerce an env string to the field's declared type."""
    text = raw.strip()
    declared_str = str(declared)

    if "bool" in declared_str:
        return _as_bool(text)
    if "tuple" in declared_str or "Sequence" in declared_str:
        return _as_tuple(text)
    # Mapping must be checked BEFORE float: ``Mapping[str, float]`` contains
    # the substring "float" and would otherwise be coerced to a scalar.
    if "Mapping" in declared_str or "dict" in declared_str:
        out: dict[str, float] = {}
        for pair in _as_tuple(text):
            if "=" in pair:
                k, v = pair.split("=", 1)
                out[k.strip()] = float(v)
        return out
    if "int" in declared_str:
        return int(text)
    if "float" in declared_str:
        return float(text)
    return text


__all__ = ["L0Config", "ENV_PREFIX", "DEFAULT_INCLUDE", "DEFAULT_EXCLUDE"]

"""Context Firewall configuration (M3).

Every knob of the firewall lives here and is overridable from the environment
with the ``GF_FW__`` prefix (double underscore = nesting, matching the
IGAFORGE ``core.config.Settings`` / ``L0Config`` conventions).  No module in
this package hardcodes a limit, a path or a regex: they all read from
:class:`FirewallConfig`.

Example::

    GF_FW__SLICE_BUDGET_TOKENS=8000
    GF_FW__PYTEST_MAX_FAILURES=10
    GF_FW__MAX_RETRIES=2
    GF_FW__STRIP_CODE_FENCES=true
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional

ENV_PREFIX = "GF_FW__"

#: Words that carry no retrieval signal in a task description.  Kept
#: deliberately small: the slicer also drops tokens that do not occur in the
#: symbol index, which removes most of the remaining noise.
DEFAULT_STOPWORDS: tuple[str, ...] = (
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "for",
    "of", "to", "in", "on", "at", "by", "with", "without", "from", "into",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "this", "that", "these", "those", "it", "its", "as", "so", "than",
    "add", "make", "made", "new", "use", "using", "need", "needs", "should",
    "must", "can", "could", "would", "will", "shall", "may", "might",
    "fix", "change", "update", "please", "we", "you", "i", "me", "my",
    "our", "your", "their", "his", "her", "them", "they", "he", "she",
    "when", "where", "which", "who", "what", "why", "how", "all", "any",
    "not", "no", "yes", "ok", "okay", "also", "just", "only", "very",
    "there", "here", "about", "after", "before", "over", "under", "between",
)

#: Path globs the diff applier refuses to touch.  Anything that looks like a
#: secret store, VCS internals or a vendored tree is off limits regardless of
#: what a model asks for.
DEFAULT_DENY_GLOBS: tuple[str, ...] = (
    ".git/**",
    ".hg/**",
    ".svn/**",
    "**/.git/**",
    "*.pem",
    "*.key",
    "*.p8",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.secret",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
    ".ssh/**",
    "**/.ssh/**",
    ".aws/**",
    "**/.aws/**",
    ".azure/**",
    "**/.azure/**",
    "**/.gnupg/**",
    ".gnupg/**",
    "secrets/**",
    "**/credentials*",
    "credentials*",
    "credentials/**",
    "**/credentials/**",
    "service-account*.json",
    "service_account*.json",
    "token.json",
    "auth.json",
    "api-key.json",
    "api_key.json",
    "apikey.json",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*.env",
    "*.env.*",
    ".envrc",
    "**/.envrc",
    "**/secrets/**",
    "tokens/**",
    "**/tokens/**",
    ".tokens/**",
    "**/.tokens/**",
    ".credentials/**",
    "**/.credentials/**",
    ".env.d/**",
    "**/.env.d/**",
    ".secrets/**",
    "**/.secrets/**",
    ".netrc",
    "**/.netrc",
    "_netrc",
    "**/_netrc",
    ".npmrc",
    "**/.npmrc",
    ".pypirc",
    "**/.pypirc",
    ".git-credentials",
    "**/.git-credentials",
    "**/node_modules/**",
    ".venv/**",
    "venv/**",
)

#: Glob patterns considered "tests" when the slicer looks for coverage.
DEFAULT_TEST_GLOBS: tuple[str, ...] = (
    "**/test_*.py",
    "**/*_test.py",
    "**/tests/**/*.py",
    "**/*.test.ts",
    "**/*.spec.ts",
    "**/*_test.go",
    "**/*Test.java",
)


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_tuple(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class FirewallConfig:
    """Immutable configuration for the Context Firewall (M3)."""

    # ── compression: shared ──────────────────────────────────────────────
    #: Appended whenever a field, list or excerpt is cut.
    truncation_marker: str = "[truncated]"
    #: Token estimate divisor; mirrors L0Config.chars_per_token so both layers
    #: agree on what "one token" costs.
    chars_per_token: float = 4.0
    #: Hard ceiling for any single string field in a compressed payload.
    max_field_chars: int = 4000
    #: Hard ceiling for any list in a compressed payload.
    max_items: int = 200
    #: Deterministic, no-LLM summarisation: never call a model to compress.
    allow_llm: bool = False

    # ── compressor: pytest ───────────────────────────────────────────────
    pytest_max_failures: int = 25
    pytest_max_errors: int = 15
    pytest_max_message_chars: int = 600
    pytest_max_excerpt_lines: int = 40
    pytest_max_excerpt_chars: int = 2000
    pytest_max_line_chars: int = 400
    pytest_max_captured_sections: int = 30

    # ── compressor: grep ─────────────────────────────────────────────────
    grep_max_matches: int = 100
    grep_max_line_chars: int = 300
    grep_max_files: int = 40
    grep_context_lines: int = 2
    grep_max_context_chars: int = 1200

    # ── compressor: git diff ─────────────────────────────────────────────
    diff_max_files: int = 50
    diff_max_hunks_per_file: int = 40
    diff_max_hunk_lines: int = 120
    diff_max_line_chars: int = 400
    diff_max_stat_files: int = 100
    diff_keep_context_lines: int = 2

    # ── compressor: build errors ─────────────────────────────────────────
    build_max_errors: int = 40
    build_max_warnings: int = 20
    build_max_message_chars: int = 600
    build_max_total: int = 60
    build_dedup: bool = True

    # ── context slicer ───────────────────────────────────────────────────
    slice_budget_tokens: int = 6000
    slice_max_seeds: int = 12
    slice_expand_depth: int = 1
    slice_max_symbols: int = 80
    slice_max_callers: int = 8
    slice_max_callees: int = 8
    slice_max_blast: int = 15
    slice_symbol_snippet_chars: int = 1200
    slice_max_keywords: int = 24
    slice_min_keyword_len: int = 3
    slice_include_repo_map: bool = True
    slice_repo_map_tokens: int = 1500
    slice_include_blame: bool = False
    slice_max_files: int = 40
    #: Files matching these globs are listed as coverage evidence.
    slice_test_globs: tuple[str, ...] = DEFAULT_TEST_GLOBS
    slice_max_tests: int = 20
    stopwords: tuple[str, ...] = DEFAULT_STOPWORDS

    # ── diff parsing / matching ──────────────────────────────────────────
    #: A model routinely wraps a diff in a ```diff fence; strip it.
    strip_code_fences: bool = True
    #: SequenceMatcher ratio below which a fuzzy hunk match is rejected.
    fuzzy_threshold: float = 0.60
    #: Fraction of context lines that must appear on the target line run before
    #: the character-level matcher accepts a location.
    char_match_min_ratio: float = 0.50
    #: How far (in lines) the fuzzy matcher may search around the hint.
    fuzzy_search_window: int = 200

    # ── diff application (worktree) ──────────────────────────────────────
    apply_enabled: bool = True
    apply_max_files: int = 30
    apply_max_hunks: int = 200
    apply_allow_new_files: bool = True
    #: A "new file" diff that targets an existing path.  Off by default: a model
    #: without context tends to re-emit a whole file as new, which silently
    #: replaces the real content.  Observed on a live run — the target ended up
    #: truncated to zero bytes, with no backup to restore from.
    apply_overwrite_existing: bool = False
    apply_allow_deletes: bool = False
    apply_deny_globs: tuple[str, ...] = DEFAULT_DENY_GLOBS
    #: Empty tuple = the whole repo root is allowed (still deny-glob filtered).
    apply_allow_paths: tuple[str, ...] = ()
    apply_write_backups: bool = True
    #: A failed apply restores every file it had already touched.
    apply_atomic: bool = True
    #: Apply each file independently.  With whole-diff atomicity a single
    #: hallucinated hunk in one file discarded correct edits to every other file —
    #: the model then had no valid result to repair from.  A file that fails is
    #: reported on its own; the diff only fails when nothing applied.
    apply_partial: bool = True
    #: Run ruff --fix / formatter on changed files before deciding the verdict.
    auto_fix: bool = False

    # ── validator loop ───────────────────────────────────────────────────
    #: Exactly this many repair attempts after the first failure (2 => 3 total
    #: passes including the initial one).
    max_retries: int = 2
    check_ruff: bool = True
    check_syntax: bool = True
    check_signatures: bool = True
    check_callers: bool = True
    check_missing_symbols: bool = True
    signature_check_limit: int = 200
    caller_check_limit: int = 200
    missing_symbol_limit: int = 50
    reindex_after_apply: bool = True
    #: ruff codes that block; breakage only (E9 syntax, F82 undefined names).
    #: Codes that hard-fail verification. ``WEB`` covers the structural HTML checks
    #: (no <html>/<body>/<title>, or a page that is a stub) — the only gate that
    #: exists for markup, since neither ruff nor tree-sitter parses it.
    #: Diagnostic prefixes that make a diff fatal.
    #:
    #: ``E9`` covered syntax errors until ruff renamed the code from ``E999`` to
    #: ``invalid-syntax``.  ``_is_fatal`` matches by *prefix*, and no prefix in
    #: this tuple matches ``invalid-syntax``, so a file that does not parse
    #: stopped being fatal -- measured on ``psf__requests-1142``: ruff reported
    #: ``invalid-syntax: Expected an indented block after 'if' statement`` and the
    #: diff was accepted.  The explicit name is listed rather than relying on the
    #: numeric prefix, which no longer describes the code.
    ruff_fatal_select: tuple[str, ...] = ("E9", "F82", "WEB", "invalid-syntax")

    # ── misc ─────────────────────────────────────────────────────────────
    repo_root: str = "."
    tenant: str = "default"
    log_level: str = "INFO"

    # ─────────────────────────────────────────────────────────────────────
    @property
    def root(self) -> Path:
        return Path(self.repo_root).resolve()

    def with_(self, **changes: Any) -> "FirewallConfig":
        """Return a copy with ``changes`` applied (frozen dataclass helper)."""
        return replace(self, **changes)

    def limits(self, **overrides: Any) -> "FirewallConfig":
        """Alias for :meth:`with_` used by per-call limit overrides."""
        return replace(self, **overrides)

    # ─────────────────────────────────────────────────────────────────────
    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        **overrides: Any,
    ) -> "FirewallConfig":
        """Build a config from ``GF_FW__*`` environment variables.

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


__all__ = [
    "FirewallConfig",
    "ENV_PREFIX",
    "DEFAULT_STOPWORDS",
    "DEFAULT_DENY_GLOBS",
    "DEFAULT_TEST_GLOBS",
]

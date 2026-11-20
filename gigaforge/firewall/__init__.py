"""Context Firewall (M3) — compress, slice, validate. Zero LLM tokens.

Three cooperating mechanisms keep an agent's context small and its edits safe:

* **Compression** — raw tool output (pytest, grep, git diff, compiler
  diagnostics) is parsed into bounded structured JSON with hard field limits and
  explicit ``[truncated]`` markers (:mod:`gigaforge.firewall.compress`).
* **Context Slicing** — the relevant symbols, call edges, blast radius and
  candidate tests are assembled from the L0 symbol index without a model call
  (:mod:`gigaforge.firewall.slicer`).
* **Diff Validation** — a model's unified diff is parsed, located with
  exact→fuzzy→character matching, applied atomically to the worktree, then
  verified with tree-sitter, ruff and signature/caller analysis, with exactly
  two repair retries (:mod:`gigaforge.firewall.validator`).

Typical use::

    from gigaforge.firewall import ContextFirewall

    fw = ContextFirewall(store, config=FirewallConfig.from_env())
    packed = fw.compress(pytest_output, kind="pytest")
    slice_ = fw.slice("fix division by zero in div")
    outcome = fw.validate_diff(model_diff, retry_fn=ask_model_again)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from gigaforge.firewall.compressors import (
    BuildErrCompressor,
)
from gigaforge.firewall.compressors import (
    GitDiffCompressor as GitDiffCompressorOld,
)
from gigaforge.firewall.compressors import (
    GrepCompressor as GrepCompressorOld,
)
from gigaforge.firewall.compressors import (
    PytestCompressor as PytestCompressorOld,
)
from gigaforge.l0.store import L0Store

from .compress import (
    KINDS,
    BuildErrorCompressor,
    CompressionResult,
    GitDiffCompressor,
    GrepCompressor,
    PytestCompressor,
    compress_auto,
    passthrough,
    sniff,
)
from .compress import (
    compress as compress_m5,
)

COMPRESSORS: dict[str, Any] = {
    "pytest": PytestCompressorOld(),
    "grep": GrepCompressorOld(),
    "gitdiff": GitDiffCompressorOld(),
    "builderr": BuildErrCompressor(),
}


def compress(
    arg1: str,
    arg2: str = "",
    config: FirewallConfig | None = None,
    *,
    limit_tokens: int = 4000,
    **kwargs: Any,
) -> CompressionResult:
    """Dispatch raw output to a compressor; **always** return a result envelope.

    Two call shapes are accepted, because both are in use across the codebase:
    ``compress(kind, raw)`` and ``compress(raw, kind)``.

    **The return type is part of the contract.**  This function used to return
    whatever the selected compressor produced: a ``CompressionResult`` from the
    newer compressors and a bare ``dict`` from the legacy ones in
    :mod:`gigaforge.firewall.compressors`.  Every caller then had to guess, and
    the ones that did the documented thing —

        packed = compress("grep", raw)
        prompt += packed.text

    — crashed with ``AttributeError: 'dict' object has no attribute 'text'``
    whenever the legacy branch was taken.  A public function whose return type
    depends on an internal branch is not a contract, so the branch is now
    invisible: :func:`~gigaforge.firewall.compress.base.coerce_result` wraps a
    legacy payload in the same envelope the new compressors already return.
    """
    from gigaforge.firewall.compress.base import coerce_result, shrink_or_passthrough

    if arg1 in COMPRESSORS:
        kind, source = arg1, arg2
        compressor = COMPRESSORS[kind]
        try:
            raw = compressor.compress(source, limit_tokens=limit_tokens, **kwargs)
        except TypeError:
            raw = compressor.compress(source, limit_tokens=limit_tokens)
        except Exception as exc:
            from gigaforge.firewall.base import fail_dict as _fail
            raw = _fail(f"compressor {kind!r} failed: {exc.__class__.__name__}: {exc}", source)
        return shrink_or_passthrough(coerce_result(raw, kind=kind, source=source), source)

    if arg2 in COMPRESSORS:
        kind, source = arg2, arg1
        compressor = COMPRESSORS[kind]
        try:
            raw = compressor.compress(source, limit_tokens=limit_tokens, **kwargs)
        except TypeError:
            raw = compressor.compress(source, limit_tokens=limit_tokens)
        except Exception as exc:
            from gigaforge.firewall.base import fail_dict as _fail
            raw = _fail(f"compressor {kind!r} failed: {exc.__class__.__name__}: {exc}", source)
        return shrink_or_passthrough(coerce_result(raw, kind=kind, source=source), source)

    return compress_m5(arg1, arg2 or "auto", config=config, **kwargs)


from .compress.base import MARKER, Truncator, estimate_tokens
from .config import (
    DEFAULT_DENY_GLOBS,
    DEFAULT_STOPWORDS,
    DEFAULT_TEST_GLOBS,
    ENV_PREFIX,
    FirewallConfig,
)
from .diff import (
    Applier,
    ApplyResult,
    DiffFile,
    Hunk,
    MatchResult,
    ParsedDiff,
    apply_hunks,
    is_path_allowed,
    locate,
    parse,
    parse_file,
    parse_hunk,
    strip_code_fences,
)
from .errors import (
    ApplyRollbackError,
    CompressionError,
    ContextMismatchError,
    DiffApplyError,
    DiffParseError,
    EmptySliceError,
    FirewallError,
    PathNotAllowedError,
    RetryBudgetExhausted,
    SliceError,
    UnsupportedSourceError,
    ValidationError,
)
from .slicer import ContextSlice, ContextSlicer, SliceSymbol
from .slicer_keywords import Extraction, Extractor, Term, keywords
from .validator import (
    Attempt,
    CallerBreak,
    DiffValidator,
    RepairRequest,
    SignatureChange,
    ValidationOutcome,
)

__version__ = "0.3.0"


class ContextFirewall:
    """Facade over the compress / slice / validate trio.

    The firewall is stateless between calls apart from the L0 store it reads and
    the worktree it writes, so a single instance can serve a whole pipeline run.
    """

    def __init__(
        self,
        store: L0Store | None = None,
        *,
        config: FirewallConfig | None = None,
        slicer: ContextSlicer | None = None,
        validator: DiffValidator | None = None,
    ) -> None:
        self.store = store
        self.config = config or FirewallConfig()
        self._slicer = slicer
        self._validator = validator

    # ── compression ──────────────────────────────────────────────────────
    def compress(
        self,
        source: str,
        kind: str = "auto",
        *,
        hint: str = "",
        **overrides: Any,
    ) -> CompressionResult:
        """Compress raw tool output. ``kind="auto"`` sniffs the payload."""
        if kind in {"auto", "", None}:
            return compress_auto(source, self.config, hint=hint, **overrides)
        return compress(source, kind, self.config, **overrides)

    # ── context slicing ──────────────────────────────────────────────────
    @property
    def slicer(self) -> ContextSlicer:
        if self._slicer is None:
            if self.store is None:
                raise SliceError(
                    "context slicing needs an L0Store",
                    detail={"hint": "pass store=... to ContextFirewall"},
                )
            self._slicer = ContextSlicer(self.store, config=self.config)
        return self._slicer

    def slice(
        self,
        task: str,
        *,
        files: Sequence[str] = (),
        symbols: Sequence[str] = (),
        budget_tokens: int | None = None,
        focus_paths: Sequence[str] = (),
        include_repo_map: bool | None = None,
    ) -> ContextSlice:
        """Assemble a deterministic context slice for ``task``."""
        return self.slicer.build(
            task,
            files=files,
            symbols=symbols,
            budget_tokens=budget_tokens,
            focus_paths=focus_paths,
            include_repo_map=include_repo_map,
        )

    # ── diff validation ──────────────────────────────────────────────────
    @property
    def validator(self) -> DiffValidator:
        if self._validator is None:
            self._validator = DiffValidator(self.store, config=self.config)
        return self._validator

    def validate_diff(
        self,
        diff_text: str,
        *,
        retry_fn: Any = None,
        task: str = "",
        dry_run: bool = False,
        max_retries: int | None = None,
    ) -> ValidationOutcome:
        """Apply and verify a diff, with the bounded repair loop."""
        return self.validator.validate(
            diff_text,
            retry_fn=retry_fn,
            task=task,
            dry_run=dry_run,
            max_retries=max_retries,
        )

    def apply_diff(self, diff_text: str, *, dry_run: bool = False) -> ApplyResult:
        """Parse and apply a diff without verification (structural use only)."""
        parsed = parse(diff_text, self.config)
        return Applier(self.config).apply(parsed, dry_run=dry_run)

    # ── introspection ────────────────────────────────────────────────────
    def limits(self) -> dict[str, Any]:
        """The active limits, for logs and the pipeline's audit trail."""
        cfg = self.config
        return {
            "compression": {
                "max_field_chars": cfg.max_field_chars,
                "max_items": cfg.max_items,
                "truncation_marker": cfg.truncation_marker,
                "kinds": list(KINDS),
            },
            "slicer": {
                "budget_tokens": cfg.slice_budget_tokens,
                "max_symbols": cfg.slice_max_symbols,
                "expand_depth": cfg.slice_expand_depth,
                "include_repo_map": cfg.slice_include_repo_map,
            },
            "diff": {
                "fuzzy_threshold": cfg.fuzzy_threshold,
                "char_match_min_ratio": cfg.char_match_min_ratio,
                "strip_code_fences": cfg.strip_code_fences,
                "apply_enabled": cfg.apply_enabled,
                "max_files": cfg.apply_max_files,
                "atomic": cfg.apply_atomic,
            },
            "validator": {
                "max_retries": cfg.max_retries,
                "check_syntax": cfg.check_syntax,
                "check_ruff": cfg.check_ruff,
                "check_signatures": cfg.check_signatures,
                "check_callers": cfg.check_callers,
                "ruff_fatal_select": list(cfg.ruff_fatal_select),
            },
            "llm_calls": 0,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"ContextFirewall(root={self.config.repo_root!r}, "
            f"store={'yes' if self.store is not None else 'no'}, "
            f"budget={self.config.slice_budget_tokens})"
        )


__all__ = [
    # facade
    "ContextFirewall",
    # config
    "FirewallConfig",
    "ENV_PREFIX",
    "DEFAULT_STOPWORDS",
    "DEFAULT_DENY_GLOBS",
    "DEFAULT_TEST_GLOBS",
    # compression
    "compress",
    "compress_auto",
    "sniff",
    "passthrough",
    "KINDS",
    "CompressionResult",
    "Truncator",
    "MARKER",
    "estimate_tokens",
    "PytestCompressor",
    "GrepCompressor",
    "GitDiffCompressor",
    "BuildErrorCompressor",
    # slicing
    "ContextSlicer",
    "ContextSlice",
    "SliceSymbol",
    "Extractor",
    "Extraction",
    "Term",
    "keywords",
    # diff
    "parse",
    "parse_file",
    "parse_hunk",
    "locate",
    "apply_hunks",
    "strip_code_fences",
    "is_path_allowed",
    "ParsedDiff",
    "DiffFile",
    "Hunk",
    "MatchResult",
    "Applier",
    "ApplyResult",
    # validation
    "DiffValidator",
    "ValidationOutcome",
    "Attempt",
    "RepairRequest",
    "SignatureChange",
    "CallerBreak",
    # errors
    "FirewallError",
    "CompressionError",
    "UnsupportedSourceError",
    "SliceError",
    "EmptySliceError",
    "DiffParseError",
    "DiffApplyError",
    "PathNotAllowedError",
    "ContextMismatchError",
    "ApplyRollbackError",
    "ValidationError",
    "RetryBudgetExhausted",
]

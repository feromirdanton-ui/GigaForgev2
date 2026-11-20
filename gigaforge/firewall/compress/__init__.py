"""Compressor registry and dispatcher.

``compress()`` routes a payload to the right compressor by explicit ``kind``;
``compress_auto()`` sniffs it.  Both are pure and deterministic — the firewall
never spends a token to compress a token.

The dispatcher returns a
:class:`~gigaforge.firewall.compress.base.CompressionResult` for every branch,
including the unknown-source fallback, so callers never special-case ``None``.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from ..config import FirewallConfig
from ..errors import UnsupportedSourceError
from .base import (
    MARKER,
    CompressionResult,
    Truncator,
    clip,
    clip_multiline,
    estimate_tokens,
    finalise,
    squeeze,
    take,
)
from .builderr import BuildErrorCompressor
from .gitdiff import GitDiffCompressor
from .grep import GrepCompressor
from .pytest import PytestCompressor

CompressorFn = Callable[..., CompressionResult]

#: Every supported source kind, in dispatch order.
KINDS: tuple[str, ...] = ("pytest", "grep", "gitdiff", "builderr")

_PYTEST_HINT = re.compile(r"^=+\s*(?:FAILURES|test session starts|\d+ (?:passed|failed))", re.M)
_GREP_HINT = re.compile(r"^[^\s:][^:]*:(?:\d+):(?:(?:\d+):)?\S", re.M)
_DIFF_HINT = re.compile(r"^diff --git ", re.M)
_STAT_HINT = re.compile(r"\|\s*\d+\s*[+\-]*\s*$", re.M)
_BUILD_HINT = re.compile(
    r"(?:^|\s)(?:error|warning)\s*(?:\[|\b|:)|Traceback \(most recent call last\)", re.M
)


def compress(
    source: str,
    kind: str,
    config: Optional[FirewallConfig] = None,
    **overrides: Any,
) -> CompressionResult:
    """Compress ``source`` with the named compressor.

    ``kind`` must be one of :data:`KINDS`; anything else raises
    :class:`UnsupportedSourceError`.  ``overrides`` are per-call limit overrides.
    """
    cfg = config or FirewallConfig()
    factory = _FACTORIES.get(kind)
    if factory is None:
        raise UnsupportedSourceError(
            f"unknown compressor kind: {kind!r}",
            detail={"kind": kind, "supported": list(KINDS)},
        )
    return factory(cfg).compress(source, **overrides)


def compress_auto(
    source: str,
    config: Optional[FirewallConfig] = None,
    *,
    hint: str = "",
    **overrides: Any,
) -> CompressionResult:
    """Sniff the payload's kind and compress it.

    ``hint`` (a tool name such as ``"pytest"`` or ``"rg"``) wins over sniffing
    when it names a known compressor.  The fallback is a bounded raw passthrough
    that still reports honest token accounting.
    """
    cfg = config or FirewallConfig()
    resolved = _resolve_hint(hint) or sniff(source)
    if resolved is None:
        return passthrough(source, config=cfg)
    return compress(source, resolved, config=cfg, **overrides)


def sniff(source: str) -> Optional[str]:
    """Best-effort kind detection; ``None`` means "unknown"."""
    if _PYTEST_HINT.search(source):
        return "pytest"
    if _DIFF_HINT.search(source):
        return "gitdiff"
    if BuildErrorCompressor(FirewallConfig()).detect(source) != "generic":
        return "builderr"
    if _STAT_HINT.search(source):
        return "gitdiff"
    if _GREP_HINT.search(source):
        return "grep"
    if _BUILD_HINT.search(source):
        return "builderr"
    return None


def passthrough(source: str, config: Optional[FirewallConfig] = None) -> CompressionResult:
    """Bounded fallback: keep the head and the tail, mark the gap."""
    cfg = config or FirewallConfig()
    tr = Truncator()
    text = squeeze(source)
    budget = max(200, cfg.max_field_chars * 2)
    if len(text) <= budget:
        body = text
    else:
        head = budget // 3
        tail = budget - head
        body = f"{text[:head]}\n{cfg.truncation_marker}\n{text[-tail:]}"
        tr.note(True)
    data: dict[str, Any] = {"kind": "raw", "text": body, "lines": len(text.splitlines())}
    return finalise("raw", data, source, tr, chars_per_token=cfg.chars_per_token)


def _resolve_hint(hint: str) -> Optional[str]:
    h = (hint or "").strip().lower()
    if not h:
        return None
    if h in _FACTORIES:
        return h
    alias = {
        "py.test": "pytest",
        "rg": "grep",
        "ripgrep": "grep",
        "ag": "grep",
        "git-diff": "gitdiff",
        "diff": "gitdiff",
        "patch": "gitdiff",
        "git": "gitdiff",
        "build": "builderr",
        "compiler": "builderr",
        "rustc": "builderr",
        "tsc": "builderr",
        "mypy": "builderr",
        "gcc": "builderr",
        "make": "builderr",
        "traceback": "builderr",
    }
    return alias.get(h)


_FACTORIES: dict[str, type] = {
    "pytest": PytestCompressor,
    "grep": GrepCompressor,
    "gitdiff": GitDiffCompressor,
    "builderr": BuildErrorCompressor,
}


__all__ = [
    "KINDS",
    "compress",
    "compress_auto",
    "sniff",
    "passthrough",
    "PytestCompressor",
    "GrepCompressor",
    "GitDiffCompressor",
    "BuildErrorCompressor",
    "CompressionResult",
    "Truncator",
    "MARKER",
    "clip",
    "clip_multiline",
    "take",
    "squeeze",
    "estimate_tokens",
    "finalise",
]

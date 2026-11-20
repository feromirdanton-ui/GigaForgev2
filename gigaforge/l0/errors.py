"""L0 error taxonomy.

Every failure raised by the L0 layer carries a stable ``code`` so callers,
metrics and the audit trail can branch on it without string matching.
Codes follow the ``GF-L0-<AREA>-<NNN>`` convention used across IGAFORGE.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class L0Error(Exception):
    """Base class for every L0 failure."""

    code: str = "GF-L0-000"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        retryable: Optional[bool] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.detail: dict[str, Any] = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


class UnsupportedLanguageError(L0Error):
    code = "GF-L0-LANG-001"


class ParseError(L0Error):
    """Source could not be parsed into a usable tree."""

    code = "GF-L0-PARSE-010"


class SymbolNotFoundError(L0Error):
    code = "GF-L0-SYM-020"


class AmbiguousSymbolError(L0Error):
    code = "GF-L0-SYM-021"


class EditConflictError(L0Error):
    """The on-disk text no longer matches the indexed revision."""

    code = "GF-L0-EDIT-030"


class EditRejectedError(L0Error):
    """An edit was refused (lint failure, syntax break, policy)."""

    code = "GF-L0-EDIT-031"


class RollbackError(L0Error):
    code = "GF-L0-EDIT-032"


class IndexError_(L0Error):
    """Indexing failed for a file or a batch."""

    code = "GF-L0-INDEX-040"


class GitError(L0Error):
    code = "GF-L0-GIT-050"


class LintToolError(L0Error):
    code = "GF-L0-LINT-060"


__all__ = [
    "L0Error",
    "UnsupportedLanguageError",
    "ParseError",
    "SymbolNotFoundError",
    "AmbiguousSymbolError",
    "EditConflictError",
    "EditRejectedError",
    "RollbackError",
    "IndexError_",
    "GitError",
    "LintToolError",
]

"""SpecificationAudit — make the model re-read its own output against the ask.

Ported from ``ai-forever/deepagents-gigachat``'s
``SpecificationAuditMiddleware``.  The failure mode is "declared correct
without checking": the agent writes a file, reads it back, and finishes — while
the file has the wrong field names, a string where a number was required, or a
missing branch.

The audit is task-agnostic.  It records the files that existed when the run
started; when the agent later reads a file that is **not** in that snapshot
(i.e. it just created it), it gets one concise audit pass focused on schema
fidelity.  No benchmark names, filenames, expected values or domain rules are
encoded here.

Injected once per file, as a human message (GigaChat's system-message rule).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from gigaforge.providers.messages import count_nudges, nudge

__all__ = [
    "SpecificationAudit",
    "SPECIFICATION_AUDIT_MARKER",
    "snapshot_workspace_files",
    "AUDIT_CHECKLIST",
]

SPECIFICATION_AUDIT_MARKER = "[SPECIFICATION-AUDIT]"

#: Directories never worth walking when snapshotting a workspace.
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)

MAX_SNAPSHOT_FILES = 20_000

AUDIT_CHECKLIST = (
    "- Exact filename, field/column/key names, nesting, and no extra fields.\n"
    "- Exact value types: number vs string vs null vs empty string vs list/object.\n"
    "- Required ordering, stable input order, tie-breakers, and deduplication rules.\n"
    "- Every named status/branch and all positive, negative, missing, retry, and "
    "boundary cases in the request.\n"
    "- For code, execute focused examples covering every stated branch and fix the "
    "implementation if any result differs."
)


def snapshot_workspace_files(workspace: Path | str | None) -> frozenset[str]:
    """Workspace-relative POSIX paths present at run start (bounded)."""
    if workspace is None:
        return frozenset()
    root = Path(workspace)
    if not root.is_dir():
        return frozenset()
    files: set[str] = set()
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS]
            base = Path(dirpath)
            for name in filenames:
                try:
                    files.add((base / name).relative_to(root).as_posix())
                except ValueError:  # pragma: no cover - defensive
                    continue
                if len(files) >= MAX_SNAPSHOT_FILES:
                    return frozenset(files)
    except OSError:  # pragma: no cover - unreadable workspace
        pass
    return frozenset(files)


def _role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("role", "")).lower()
    return str(getattr(message, "role", "")).lower()


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, Mapping):
        calls = message.get("tool_calls") or []
    else:
        calls = getattr(message, "tool_calls", None) or []
    return [c for c in calls if isinstance(c, Mapping)]


def _read_path(call: Mapping[str, Any]) -> str:
    args = call.get("args") or {}
    if not isinstance(args, Mapping):
        return ""
    raw = args.get("file_path") or args.get("path") or ""
    return str(raw).lstrip("/")


@dataclass
class SpecificationAudit:
    """Audit newly created files against the user's contract, once each."""

    workspace: Optional[Path] = None
    initial_files: frozenset[str] = frozenset()
    audited: set[str] = field(default_factory=set)

    @classmethod
    def for_workspace(cls, workspace: Path | str | None) -> "SpecificationAudit":
        path = Path(workspace) if workspace is not None else None
        return cls(workspace=path, initial_files=snapshot_workspace_files(path))

    # -- detection -----------------------------------------------------------
    def _last_read_path(self, messages: Sequence[Any]) -> str:
        for message in reversed(messages):
            if _role(message) not in {"assistant", "ai", "model"}:
                continue
            calls = _tool_calls(message)
            if not calls:
                continue
            call = calls[-1]
            if str(call.get("name", "")) != "read_file":
                return ""
            return _read_path(call)
        return ""

    def is_new_file(self, path: str) -> bool:
        if not path:
            return False
        return path not in self.initial_files

    # -- injection -----------------------------------------------------------
    def message(self, path: str) -> dict[str, Any]:
        return nudge(
            f"You just inspected the generated output `{path}`. Before finishing, "
            "compare it field-by-field with the user's original contract; do not "
            "merely declare it correct.\n"
            f"{AUDIT_CHECKLIST}\n"
            "If any requirement is not visibly satisfied, fix the output now and "
            "read it once more. Otherwise finish.",
            marker=SPECIFICATION_AUDIT_MARKER,
        )

    def maybe_inject(self, messages: Sequence[Any]) -> Optional[dict[str, Any]]:
        """Return the audit nudge when the last action read a new file."""
        if not messages:
            return None
        path = self._last_read_path(messages)
        if not self.is_new_file(path):
            return None
        if path in self.audited:
            return None
        if count_nudges(messages, SPECIFICATION_AUDIT_MARKER) > 0:
            # One audit pass per run is enough; a second would be noise.
            return None
        self.audited.add(path)
        return self.message(path)

    def describe(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace) if self.workspace else None,
            "initial_files": len(self.initial_files),
            "audited": sorted(self.audited),
        }

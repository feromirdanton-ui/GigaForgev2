"""GigaForge sandbox — allowlist + async executor.

Public surface:
- :data:`ALLOWED` — the default command allowlist.
- :func:`validate_command` — validator (returns ``(ok, reason)``).
- :class:`CommandRejected` — raised when an argv fails validation.
- :func:`sanitize_cwd` — worktree path resolver.
- :class:`SandboxExecutor` — the async runner.
- :class:`ExecutionResult` — the structural result (returncode + ok +
  raw_output + compressed dict + kind + elapsed_s).

Design invariants:
- No command runs unless it passes :func:`validate_command`.
- ``cwd`` is the worktree (resolved ``strict=True``, must be a directory).
- ALL stdout+stderr is routed through the Firewall's ``compress(kind, ...)``.
- ``asyncio.create_subprocess_exec`` (shell=False) is the only spawn path.
- Rejections are logged (no silent degradation).
"""
from __future__ import annotations

from gigaforge.sandbox.allowlist import (
    ALLOWED,
    FORBIDDEN_CHARS,
    CommandRejected,
    sanitize_cwd,
    validate_command,
)
from gigaforge.sandbox.executor import ExecutionResult, SandboxExecutor

__all__ = [
    "ALLOWED",
    "CommandRejected",
    "ExecutionResult",
    "FORBIDDEN_CHARS",
    "SandboxExecutor",
    "sanitize_cwd",
    "validate_command",
]

"""Async sandbox executor: runs allowlisted commands in a git worktree.

The executor is the ONLY component in the orchestration harness that spawns
subprocesses on behalf of the LLM. It enforces three invariants:

1. **Allowlist** — every command passes
   :func:`gigaforge.sandbox.allowlist.validate_command`. Anything not in the
   allowlist raises :class:`CommandRejected`. There is no "best-effort"
   path; rejections are logged and surfaced.

2. **Worktree cwd** — the subprocess always runs with
   ``cwd=sanitize_cwd(self.worktree)``. The cwd is NEVER the repo root
   unless the worktree IS the repo root. Path traversal is rejected by the
   allowlist; the cwd is resolved ``strict=True`` and confirmed to be a
   directory.

3. **Firewall routing** — ALL stdout/stderr is combined and routed through
   :func:`gigaforge.firewall.compress` before any caller (LLM or otherwise)
   sees it. The executor returns :class:`ExecutionResult.compressed` (a
   small dict) and ``raw_output`` (the full combined output, for the
   pipeline's own debugging — never echoed to the LLM). The compressed
   payload is guaranteed to satisfy
   ``estimate_tokens(json.dumps(compressed)) < firewall_limit_tokens``.

The executor uses :func:`asyncio.create_subprocess_exec` with
``shell=False`` (never the shell-enabled form). Subprocess timeout is
enforced via :func:`asyncio.wait_for` around ``proc.communicate()``; on
timeout the process is killed and a non-``ok`` :class:`ExecutionResult`
is returned
(no exception, no crash — the spec demands this).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gigaforge.firewall import compress
from gigaforge.sandbox.allowlist import (
    ALLOWED,
    CommandRejected,
    sanitize_cwd,
    validate_command,
)

__all__ = ["ExecutionResult", "SandboxExecutor"]

log = logging.getLogger(__name__)


# Map of well-known bare-command aliases → real executable on Windows.
# On POSIX the command is resolved by the OS itself via PATH, so we leave
# argv untouched. The allowlist keeps both the canonical name (``python3``)
# and the OS-specific real name (``python``) — this map lets the executor
# transparently substitute one for the other at spawn time, so test and
# pipeline contracts that emit ``python3`` work on Windows without any
# changes. ``py`` is the Windows Python Launcher; if it's the only thing on
# PATH (no ``python``) we use it too.
_WINDOWS_CMD_ALIASES: dict[str, tuple[str, ...]] = {
    "python3": ("python", "py"),
    "pip3": ("pip",),
    "gitbash": ("bash",),
}


def _resolve_command(argv: list[str]) -> list[str]:
    """Resolve the argv head on Windows to a binary that actually exists.

    POSIX systems honour any name on PATH, so this is a no-op there. On
    Windows, ``python3`` is not installed by any standard Python
    distribution — only ``python`` (and the ``py`` launcher) is. We keep
    ``python3`` in the default allowlist so contracts / tests stay
    portable, then substitute at execution time. Idempotent: if the head
    resolves fine on its own the argv is returned unchanged.
    """
    if not argv or os.name != "nt":
        return argv
    head = argv[0]
    candidates = _WINDOWS_CMD_ALIASES.get(head)
    if not candidates:
        # Try the head as-is first (lets ``python`` work directly).
        if shutil.which(head) is not None:
            return argv
        # Try the candidates for any well-known alias that matches.
        for alias, names in _WINDOWS_CMD_ALIASES.items():
            if head in names and shutil.which(alias) is not None:
                return [alias, *argv[1:]]
        return argv
    # Head is an alias like ``python3``. Find an installed replacement.
    for name in candidates:
        if shutil.which(name) is not None:
            return [name, *argv[1:]]
    # Alias requested but no installed replacement found: keep the argv
    # verbatim so the OS produces a clear "executable not found" error
    # instead of a silent fall-through to ``python`` (which might not be
    # the right interpreter for a virtualenv-aware test runner).
    return argv


@dataclass
class ExecutionResult:
    """The structural result of one sandbox command execution.

    Fields
    ------
    returncode:
        The process exit code. On timeout, set to ``-1`` (the executor
        doesn't raise on timeout — it returns a non-``ok`` result with a
        timeout marker in ``compressed``).
    ok:
        ``True`` iff ``returncode == 0`` AND the command was not timed out.
    raw_output:
        The full combined ``stdout + "\\n" + stderr``. Kept on the result
        for the pipeline's own debugging; NEVER echo this to an LLM. Use
        :attr:`compressed` for that.
    compressed:
        The Firewall-compressed structural JSON dict. Always bounded by
        ``firewall_limit_tokens`` — see
        :func:`gigaforge.firewall.estimate_tokens`.
    compressed_kind:
        The compressor kind that produced ``compressed`` (``"pytest"``,
        ``"gitdiff"``, ``"builderr"``, etc.). Lets the pipeline pick the
        right prompt template.
    elapsed_s:
        Wall-clock seconds the subprocess took (or the timeout value if
        timed out).
    argv:
        The argv that was executed (post-validation). Stored for the
        pipeline's trace; never echoed to the LLM.
    timed_out:
        ``True`` iff the subprocess was killed by the executor's timeout.
    """

    returncode: int
    ok: bool
    raw_output: str
    compressed: dict[str, Any]
    compressed_kind: str
    elapsed_s: float
    argv: list[str] = field(default_factory=list)
    timed_out: bool = False

    def summary(self) -> str:
        """One-line human-readable summary for logs/traces."""
        return (
            f"ExecutionResult(argv0={self.argv[0] if self.argv else '?'}, "
            f"rc={self.returncode}, ok={self.ok}, "
            f"kind={self.compressed_kind}, elapsed={self.elapsed_s:.2f}s, "
            f"timed_out={self.timed_out})"
        )


class SandboxExecutor:
    """Async sandbox runner: allowlist + Firewall routing, no shell.

    Parameters
    ----------
    worktree:
        The directory the subprocess will run in. Resolved via
        :func:`sanitize_cwd` on every :meth:`run` call (NOT in
        ``__init__`` — the worktree may not exist yet when the executor is
        constructed; e.g. a sub-agent may receive a worktree path that
        gets ``git worktree add``-ed later).
    allowlist:
        Override the default allowlist for tests or restricted sub-shells.
        Defaults to :data:`gigaforge.sandbox.allowlist.ALLOWED`.
    timeout_s:
        Per-command wall-clock timeout. Enforced via
        :func:`asyncio.wait_for` around ``proc.communicate()``. On
        timeout the process is killed and a non-``ok`` result is returned.
    firewall_limit_tokens:
        Hard cap on ``estimate_tokens(json.dumps(compressed))``. The
        Firewall enforces this internally; the executor passes it straight
        through to :func:`compress`.
    """

    def __init__(
        self,
        *,
        worktree: Path,
        allowlist: frozenset[str] = ALLOWED,
        timeout_s: int = 120,
        firewall_limit_tokens: int = 4000,
    ) -> None:
        self.worktree = Path(worktree)
        # We accept any iterable of strings; normalise to a frozenset for
        # O(1) membership checks and to make the contract explicit.
        self.allowlist: frozenset[str] = (
            allowlist if isinstance(allowlist, frozenset) else frozenset(allowlist)
        )
        self.timeout_s = timeout_s
        self.firewall_limit_tokens = firewall_limit_tokens

    # -------------------------------------------------------------- public API

    async def run(self, argv: list[str], *, kind: str = "pytest") -> ExecutionResult:
        """Validate + spawn + compress one command.

        Raises :class:`CommandRejected` if validation fails — the pipeline
        should surface this as a tool error to the LLM (with the reason
        string; never the raw argv, which may contain LLM-emitted garbage).
        Never raises on subprocess failure or timeout — those return a
        non-``ok`` :class:`ExecutionResult`.
        """
        # 1. Validate against the allowlist BEFORE we touch the filesystem.
        # If the executor was constructed with a non-default allowlist,
        # use that for the membership check; the structural validations
        # (no path traversal, no metachars, bare command head) are universal.
        ok, reason = self._validate(argv)
        if not ok:
            log.warning("sandbox: rejected argv head=%r reason=%r", argv[0] if argv else None, reason)
            raise CommandRejected(reason, argv=argv)

        # 2. Resolve + confirm cwd (the worktree). Done here, not in
        # __init__, because the worktree may not exist yet at construction.
        cwd = sanitize_cwd(self.worktree)

        # 2b. Resolve the head command — Windows has no ``python3.exe`` by
        # default, only ``python.exe``. We keep both names in the default
        # allowlist (so contracts / tests assert ``python3`` as a stable
        # identifier), but substitute at execution time so the right binary
        # is invoked on whatever OS we're running on. Cross-platform and
        # idempotent: a no-op when the head is found directly.
        resolved_argv = _resolve_command(argv) if os.name == "nt" else argv

        # 3. Spawn (shell=False, always). Capture both streams.
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *resolved_argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # The executable wasn't on PATH (e.g. ``ls``/``cat`` on Windows,
            # or a per-platform alias mismatch). The executor contract says we
            # NEVER raise on subprocess failures — translate to a structured
            # non-ok result so the LLM sees a precise tool error and the
            # pipeline can decide whether to fall back / retry / re-plan.
            elapsed = time.monotonic() - started
            log.warning("sandbox: executable not found argv=%r err=%s", argv, exc)
            compressed: dict[str, Any] = {
                "error": "executable not found",
                "argv_head": argv[0] if argv else "",
                "resolved_argv_head": resolved_argv[0] if resolved_argv else "",
                "exception": str(exc),
                "stderr": "",
            }
            compressed = self._enforce_budget(compressed)
            return ExecutionResult(
                returncode=-1,
                ok=False,
                raw_output=str(exc),
                compressed=compressed,
                compressed_kind=kind,
                elapsed_s=elapsed,
                argv=list(argv),
                timed_out=False,
            )

        # 4. Communicate with timeout. On TimeoutError: kill the process,
        # drain its pipes (best-effort), return a non-ok result. No raise.
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_s
            )
            elapsed = time.monotonic() - started
            returncode = proc.returncode if proc.returncode is not None else -1
            timed_out = False
        except TimeoutError:
            elapsed = time.monotonic() - started
            log.warning(
                "sandbox: timeout after %.1fs argv=%r", elapsed, argv
            )
            stdout_b, stderr_b = await self._kill_and_drain(proc)
            returncode = -1
            timed_out = True

        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        combined = stdout
        if stderr:
            combined = combined + ("\n" if combined and not combined.endswith("\n") else "")
            combined = combined + stderr

        # 5. Compress via the Firewall. NEVER return raw output larger than
        # the budget; the Firewall enforces this internally. For timeout
        # we construct a bounded structural dict directly (no real output
        # to parse). For all other paths we route through compress(kind, ...).
        if timed_out:
            compressed: dict[str, Any] = {
                "error": f"timeout after {self.timeout_s}s",
                "timeout_s": self.timeout_s,
                "argv_head": argv[0] if argv else "",
                # Best-effort: include whatever the process managed to emit
                # before being killed (bounded by the Firewall helper).
                "partial_stdout": combined[: self.firewall_limit_tokens * 4],
            }
            # Final belt-and-braces: ensure the serialised form fits.
            compressed = self._enforce_budget(compressed)
        else:
            compressed = compress(
                kind, combined, limit_tokens=self.firewall_limit_tokens
            )

        result = ExecutionResult(
            returncode=returncode,
            ok=(returncode == 0 and not timed_out),
            raw_output=combined,
            compressed=compressed,
            compressed_kind=kind,
            elapsed_s=elapsed,
            argv=list(argv),
            timed_out=timed_out,
        )
        log.info("sandbox: %s", result.summary())
        return result

    async def run_tests(self, test_args: list[str] | None = None) -> ExecutionResult:
        """Run pytest in the worktree; kind="pytest"."""
        argv = ["pytest"] + (list(test_args) if test_args else [])
        return await self.run(argv, kind="pytest")

    async def run_lint(self, path: Path) -> ExecutionResult:
        """Run ``ruff check <path>`` in the worktree; kind="builderr"."""
        argv = ["ruff", "check", str(path)]
        return await self.run(argv, kind="builderr")

    async def git(self, args: list[str]) -> ExecutionResult:
        """Run ``git <args>`` in the worktree.

        Kind is ``"gitdiff"`` if ``args`` contains a ``"diff"`` subcommand
        (matching the Firewall's gitdiff compressor); otherwise
        ``"builderr"`` (the most general compressor — handles plain text
        output via its bounded ``fail_dict`` fallback).
        """
        argv = ["git"] + list(args)
        kind = "gitdiff" if "diff" in args else "builderr"
        return await self.run(argv, kind=kind)

    # -------------------------------------------------------------- internals

    def _validate(self, argv: list[str]) -> tuple[bool, str]:
        """Validate argv against this executor's allowlist.

        HIGH-08: pass ``allowed=self.allowlist`` so a custom allowlist can
        ADD commands beyond the global ALLOWED (previously the global check
        ran first and rejected any head not in ALLOWED, making a custom
        allowlist's extra commands un-runnable).
        """
        return validate_command(argv, allowed=self.allowlist)

    async def _kill_and_drain(self, proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        """Kill a timed-out process and best-effort drain its pipes."""
        try:
            proc.kill()
        except ProcessLookupError:
            # Already gone — nothing to kill. Safe.
            pass
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("sandbox: kill() raised %r", exc)
        # Drain: communicate() will read remaining stdout/stderr and wait
        # for the process to actually exit. We bound this with a short
        # timeout so a wedged pipe doesn't hang the executor.
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except Exception:
            # Truly stuck; give up. Best-effort empty drain.
            stdout_b, stderr_b = b"", b""
        return stdout_b, stderr_b

    def _enforce_budget(self, result: dict[str, Any]) -> dict[str, Any]:
        """Last-resort guard: ensure serialised result fits the token budget.

        The Firewall does this for compress() output, but for the timeout
        path we construct the dict directly — apply the same shrinking here.
        """

        from gigaforge.firewall.base import enforce_token_budget

        return enforce_token_budget(result, self.firewall_limit_tokens)

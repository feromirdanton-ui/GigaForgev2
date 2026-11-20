"""Command allowlist + validator for the GigaForge sandbox executor.

The sandbox runs LLM-produced plans/diffs but NEVER trusts LLM-emitted shell
commands. Every command must pass :func:`validate_command` before
:class:`gigaforge.sandbox.executor.SandboxExecutor` will spawn it via
``asyncio.create_subprocess_exec`` (shell=False). Anything outside this
module's allowlist is rejected with :class:`CommandRejected` and logged.

Rules
-----
1. ``argv`` MUST be non-empty.
2. The FIRST token MUST be a bare command name (no ``/``, no ``\\``, no
   whitespace) AND a member of :data:`ALLOWED`. Absolute paths
   (``/usr/bin/pytest``) and relative paths (``./pytest``, ``../bin/pytest``)
   are rejected — only PATH-resolved bare names are accepted.
3. No argument may contain the path-traversal token ``..`` (defensive;
   rejects ``cat ../../etc/passwd`` even though ``shell=False`` makes it
   safe).
4. No argument may contain a shell metacharacter from :data:`FORBIDDEN_CHARS`
   (``;``, ``|``, ``&``, ``$``, `` ` ``, ``>``, ``<``). ``shell=False`` makes
   these safe in theory but we reject them defensively because the LLM may
   emit them by mistake and they would still produce confusing argv for the
   child process.

The allowlist is a frozen set; it can be substituted at
:class:`SandboxExecutor` construction time for tests or for restricted
sub-shells, but the default (:data:`ALLOWED`) is the contract surface for the
orchestration harness.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from gigaforge.client.errors import GigaForgeError

__all__ = [
    "ALLOWED",
    "CommandRejected",
    "FORBIDDEN_CHARS",
    "sanitize_cwd",
    "validate_command",
    "validate_structure",
]

log = logging.getLogger(__name__)


# Default allowlist — the only commands the sandbox will ever exec.
# Read once from env (comma-separated) to allow operator-tuned sub-shells; falls
# back to the spec's sane default set. Tests construct a fresh executor with the
# default; this module-level constant IS the contract.
_DEFAULT = ("pytest", "ruff", "git", "python", "python3", "ls", "cat")


def _load_allowlist() -> frozenset[str]:
    """Read the allowlist from env if set; else return the sane default.

    Env form: ``GIGAFORGE_SANDBOX_ALLOWLIST=pytest,ruff,git,python3``. We
    union with the default set so the harness always has its baseline tools
    available — operators can ONLY add commands, never remove the baseline.

    NOTE (LOW-02): this is invoked exactly ONCE at module import time (see
    ``ALLOWED`` below). Re-reading ``GIGAFORGE_SANDBOX_ALLOWLIST`` after
    import requires a process restart — the module-level ``ALLOWED`` constant
    is the contract surface and is intentionally frozen for the lifetime of
    the process. Tests that need a different allowlist construct a fresh
    ``SandboxExecutor`` with an explicit ``allowed`` set.
    """
    env = os.environ.get("GIGAFORGE_SANDBOX_ALLOWLIST", "")
    if not env.strip():
        return frozenset(_DEFAULT)
    extra = {tok.strip() for tok in env.split(",") if tok.strip()}
    return frozenset(_DEFAULT) | extra


# Frozen at import time. To pick up env changes the process MUST be restarted
# (see _load_allowlist docstring above). Substitutable per-SandboxExecutor for
# tests / restricted sub-shells — but the module-level value is the contract.
ALLOWED: frozenset[str] = _load_allowlist()


# Defensive: shell metacharacters we refuse in any argv token. Even though
# the executor uses ``shell=False`` (so these are passed verbatim to the
# child as argv elements, not parsed by a shell), the LLM should never emit
# them in a tool-call payload — rejecting them surfaces the mistake loudly
# rather than passing a confusing token to pytest / ruff / git.
FORBIDDEN_CHARS: frozenset[str] = frozenset(";|&`$<>")


class CommandRejected(GigaForgeError):
    """A command was rejected by the allowlist validator.

    Raised by :class:`SandboxExecutor.run` when :func:`validate_command`
    returns ``(False, reason)``. The ``reason`` is a single human-readable
    string suitable for feeding back to the LLM as a tool error.
    """

    def __init__(self, reason: str, argv: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        # The argv that was rejected (for logging / metrics; never echoed to
        # the LLM payload — that's the caller's concern).
        self.argv = list(argv or [])

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"CommandRejected: {self.reason}"


def _is_bare_command(token: str) -> bool:
    """True iff *token* is a bare command name (no path separators, non-empty)."""
    if not token or not token.strip():
        return False
    # Reject anything that looks like a path: ``/usr/bin/x``, ``./x``,
    # ``../bin/x``, ``C:\\x``, ``x/y``. Only PATH-resolved names are accepted.
    if "/" in token or "\\" in token:
        return False
    # No whitespace — a bare command name is a single token.
    if any(c.isspace() for c in token):
        return False
    return True


def _contains_forbidden(token: str) -> bool:
    """True iff *token* carries a defensive-rejection metacharacter."""
    return any(ch in FORBIDDEN_CHARS for ch in token)


def validate_structure(argv: list[str]) -> tuple[bool, str]:
    """Structural-only validation: argv non-empty, head is a bare command
    name (no path separators / whitespace), no ``..`` traversal in any arg,
    no shell metacharacter in any arg.

    Allowlist-AGNOSTIC half of :func:`validate_command`. Callers that want a
    custom allowlist use this + their own membership check.
    """
    if argv is None:
        return False, "argv is None"
    if len(argv) == 0:
        return False, "argv is empty"

    head = argv[0]
    if not _is_bare_command(head):
        log.warning("sandbox: rejected non-bare command head (len=%d)", len(head))
        return (
            False,
            "command must be a bare name resolved via PATH "
            "(no '/', '\\', or whitespace); got a path-like token",
        )

    for idx, tok in enumerate(argv[1:], start=1):
        if not isinstance(tok, str):
            return False, f"argv[{idx}] is not a string ({type(tok).__name__})"
        if ".." in tok:
            log.warning("sandbox: rejected '..' in argv[%d]", idx)
            return (False, f"path traversal refused: argv[{idx}] contains '..'")
        if _contains_forbidden(tok):
            log.warning("sandbox: rejected forbidden char in argv[%d]", idx)
            return (
                False,
                f"shell metacharacter refused in argv[{idx}] "
                f"(forbidden chars: {sorted(FORBIDDEN_CHARS)})",
            )
    return True, "ok"


def validate_command(
    argv: list[str], *, allowed: frozenset[str] | None = None
) -> tuple[bool, str]:
    """Validate *argv* against the allowlist + injection rules.

    HIGH-08: ``allowed`` lets a caller pass a CUSTOM allowlist (a stricter
    subset OR extra commands beyond the global :data:`ALLOWED`). Default
    ``None`` means "use the global ALLOWED". Structural rules are always
    enforced via :func:`validate_structure`. Never raises — returns a tuple.
    """
    ok, reason = validate_structure(argv)
    if not ok:
        return ok, reason
    head = argv[0]
    allow = allowed if allowed is not None else ALLOWED
    if head not in allow:
        log.warning("sandbox: rejected command %r not in allowlist", head)
        return (
            False,
            f"command {head!r} is not in the allowlist "
            f"(allowed: {sorted(allow)})",
        )
    return True, "ok"

def sanitize_cwd(cwd: Path) -> Path:
    """Resolve *cwd* and confirm it's an existing directory.

    Raises :class:`CommandRejected` if the path doesn't exist or isn't a
    directory — the executor can't run a subprocess in a missing cwd.
    """
    if cwd is None:
        raise CommandRejected("worktree cwd is None")
    try:
        resolved = Path(cwd).resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise CommandRejected(f"worktree cwd does not exist: {cwd!r} ({exc})") from exc
    if not resolved.is_dir():
        raise CommandRejected(f"worktree cwd is not a directory: {resolved!r}")
    return resolved

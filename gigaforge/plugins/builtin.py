"""Built-in tools — GrepTool, GitTool, RuffTool, PytestTool.

Each Tool wraps :class:`gigaforge.sandbox.executor.SandboxExecutor`, which
already enforces the allowlist, runs ``shell=False`` subprocess, AND routes
every byte of stdout/stderr through
:func:`gigaforge.firewall.compress` before returning. So
``ToolResult.output`` is the Firewall-compressed structural JSON ready for
LLM consumption (no raw subprocess output ever leaves the sandbox).

These are NOT a substitute for the pipeline's internal use of the executor
— the pipeline already calls ``run_tests`` / ``run_lint`` / ``git``
directly on its per-task worktree. These Tools exist so that:

1. The C subagent's tool-use loop can call them by name (a generic tool
   interface — not a hardcoded subprocess call inside agent code).
2. The MCP server can expose them as MCP ``tools/call`` targets for
   external clients (Claude Desktop, Cline, etc.).
3. Future plugins (custom tools shipped as separate packages) get the
   same interface.

Custom allowlists: each Tool constructs its own SandboxExecutor with a
superset of the global :data:`ALLOWED` that adds the command it needs
(``rg`` for GrepTool). The structural rules (no path traversal, no shell
metachars) still apply globally via :func:`validate_structure`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from gigaforge.firewall import estimate_tokens
from gigaforge.sandbox.allowlist import ALLOWED, CommandRejected, validate_command
from gigaforge.sandbox.executor import SandboxExecutor

__all__ = [
    "BuiltinPlugin",
    "GitTool",
    "GrepTool",
    "PytestTool",
    "RuffTool",
]


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _BaseTool:
    """Shared scaffolding for the four built-in tools.

    Subclasses set ``name``, ``description``, ``schema`` (validated flat
    JSON-schema), and implement :meth:`_build_argv` (the per-command
    argv passed to the executor's :meth:`run`) plus :meth:`_kind`
    (the Firewall compressor kind).

    Subclasses can also override :meth:`_executor_kind` to override the
    kind detection (default behaviour: each tool uses one fixed kind).
    """

    name: str = ""
    description: str = ""
    schema: dict[str, Any] = {}

    def __init__(
        self,
        *,
        worktree: Path | None = None,
        timeout_s: int = 120,
        firewall_limit_tokens: int = 4000,
        allowlist: frozenset[str] | None = None,
    ) -> None:
        self._worktree = Path(worktree) if worktree is not None else Path(".")
        self._timeout_s = timeout_s
        self._firewall_limit_tokens = firewall_limit_tokens
        self._allowlist = allowlist or self._default_allowlist()

    # --- subclass hooks ----------------------------------------------------

    @classmethod
    def _default_allowlist(cls) -> frozenset[str]:
        """The custom allowlist for this tool (always a SUPERSET of ALLOWED)."""
        return ALLOWED

    def _build_argv(self, args: dict[str, Any]) -> list[str]:
        raise NotImplementedError

    def _kind(self, args: dict[str, Any]) -> str:
        raise NotImplementedError

    # --- public Tool surface ----------------------------------------------

    async def run(self, args: dict[str, Any]) -> Any:
        """Validate args, build argv, execute via SandboxExecutor, return ToolResult.

        NEVER raises — every error surfaces as
        ``ToolResult(ok=False, error=..., output={...})``.
        """
        from gigaforge.plugins.base import ToolResult

        # 1. Validate the worktree exists (lazy resolve — matches executor).
        try:
            argv = self._build_argv(args)
            kind = self._kind(args)
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult(
                ok=False,
                output={"error": f"invalid args: {exc}"},
                error=str(exc),
            )

        # 2. Allowlist pre-check — produce a structured ToolResult error
        # (rather than raising CommandRejected) so callers can present the
        # rejection to the LLM as a tool error.
        ok, reason = validate_command(argv, allowed=self._allowlist)
        if not ok:
            return ToolResult(
                ok=False,
                output={"error": f"command rejected: {reason}", "argv_head": argv[0] if argv else ""},
                error=f"command rejected: {reason}",
            )

        executor = SandboxExecutor(
            worktree=self._worktree,
            allowlist=self._allowlist,
            timeout_s=self._timeout_s,
            firewall_limit_tokens=self._firewall_limit_tokens,
        )

        try:
            result = await executor.run(argv, kind=kind)
        except CommandRejected as exc:
            return ToolResult(
                ok=False,
                output={"error": exc.reason, "argv_head": exc.argv[0] if exc.argv else ""},
                error=exc.reason,
            )
        except Exception as exc:  # noqa: BLE001 — never raise out of a Tool
            return ToolResult(
                ok=False,
                output={"error": f"unexpected: {type(exc).__name__}: {exc}"},
                error=str(exc),
            )

        return ToolResult(
            ok=result.ok,
            output=dict(result.compressed),
            error=None if result.ok else (
                f"{argv[0] if argv else '?'} exit={result.returncode}"
                + (" (timeout)" if result.timed_out else "")
            ),
        )


# ---------------------------------------------------------------------------
# GrepTool — ripgrep over the repo (output through Firewall grep compressor)
# ---------------------------------------------------------------------------


class GrepTool(_BaseTool):
    """Search the repo for *pattern* with ripgrep.

    Args (flat schema):
    - pattern: str (required) — regex/substring to search for.
    - path: str (optional) — sub-path to scope; defaults to ".".

    Output: the ``grep`` Firewall compressor's structural JSON
    (``matches``, ``failed_count``, ``error``, etc.).
    """

    name = "grep"
    description = "Search the repository for a pattern using ripgrep."
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "regex/substring to search for"},
            "path": {"type": "string", "description": "sub-path to scope (default: .)"},
        },
        "required": ["pattern"],
    }

    @classmethod
    def _default_allowlist(cls) -> frozenset[str]:
        # Add `rg` to the baseline — operators extend the GLOBAL via env
        # GIGAFORGE_SANDBOX_ALLOWLIST; here we extend per-instance.
        return frozenset(ALLOWED) | frozenset({"rg"})

    def _build_argv(self, args: dict[str, Any]) -> list[str]:
        pattern = args["pattern"]
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a non-empty string")
        path = args.get("path") or "."
        if not isinstance(path, str):
            raise ValueError("path must be a string when given")
        # rg args: --line-number (show line numbers), --no-heading, --color=never,
        # pattern, then path. We avoid --json because the grep compressor parses
        # the default "path:line:content" text format.
        return ["rg", "--line-number", "--no-heading", "--color=never", pattern, path]

    def _kind(self, args: dict[str, Any]) -> str:
        return "grep"


# ---------------------------------------------------------------------------
# GitTool — allowlisted git ops (status, log, diff)
# ---------------------------------------------------------------------------


class GitTool(_BaseTool):
    """Run an allowlisted git subcommand in the worktree.

    Args (flat schema):
    - subcommand: str (required) — one of {"status","log","diff"}.
    - args: list[str] (optional) — extra args after the subcommand
      (e.g. ["--name-only"] for ``git status --name-only``).
    - path: str (optional) — pathspec; default ".".

    Output: the ``gitdiff`` Firewall compressor for diff (structural JSON
    with per-file added/removed + best-effort symbol); the ``builderr``
    compressor's bounded raw-truncated form for status/log.
    """

    name = "git"
    description = "Run an allowlisted git subcommand (status, log, diff)."
    schema = {
        "type": "object",
        "properties": {
            "subcommand": {
                "type": "string",
                "description": "one of: status, log, diff",
                "enum": ["status", "log", "diff"],
            },
            "args": {
                "type": "array",
                "description": "extra args after the subcommand",
                "items": {"type": "string"},
            },
            "path": {"type": "string", "description": "pathspec (default: .)"},
        },
        "required": ["subcommand"],
    }

    _ALLOWED_SUBCOMMANDS = frozenset({"status", "log", "diff"})

    def _build_argv(self, args: dict[str, Any]) -> list[str]:
        sub = args["subcommand"]
        if sub not in self._ALLOWED_SUBCOMMANDS:
            raise ValueError(
                f"git subcommand must be one of {sorted(self._ALLOWED_SUBCOMMANDS)}, got {sub!r}"
            )
        argv: list[str] = ["git", sub]
        extra = args.get("args") or []
        if not isinstance(extra, list):
            raise ValueError("args must be a list of strings")
        for tok in extra:
            if not isinstance(tok, str):
                raise ValueError("args must contain only strings")
            argv.append(tok)
        path = args.get("path")
        if path:
            if not isinstance(path, str):
                raise ValueError("path must be a string when given")
            argv.append("--")
            argv.append(path)
        return argv

    def _kind(self, args: dict[str, Any]) -> str:
        return "gitdiff" if args.get("subcommand") == "diff" else "builderr"


# ---------------------------------------------------------------------------
# RuffTool — lint/format
# ---------------------------------------------------------------------------


class RuffTool(_BaseTool):
    """Run ``ruff check`` (lint) or ``ruff format`` (format) on a path.

    Args (flat schema):
    - action: str (required) — "lint" or "format".
    - path: str (required) — file or directory to lint/format.

    Output: the ``builderr`` Firewall compressor's structural JSON
    (error + file:line for the first diagnostic).
    """

    name = "ruff"
    description = "Lint (ruff check) or format (ruff format) Python code."
    schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "one of: lint, format",
                "enum": ["lint", "format"],
            },
            "path": {"type": "string", "description": "file or directory"},
        },
        "required": ["action", "path"],
    }

    _ACTIONS = frozenset({"lint", "format"})

    def _build_argv(self, args: dict[str, Any]) -> list[str]:
        action = args["action"]
        if action not in self._ACTIONS:
            raise ValueError(f"action must be one of {sorted(self._ACTIONS)}, got {action!r}")
        path = args["path"]
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        if action == "lint":
            return ["ruff", "check", "--output-format=concise", path]
        return ["ruff", "format", "--check", path]

    def _kind(self, args: dict[str, Any]) -> str:
        return "builderr"


# ---------------------------------------------------------------------------
# PytestTool — run tests (output through Firewall pytest compressor)
# ---------------------------------------------------------------------------


class PytestTool(_BaseTool):
    """Run pytest in the worktree.

    Args (flat schema):
    - test_args: list[str] (optional) — extra pytest args (e.g. ["-x",
      "tests/test_math.py"]).
    - path: str (optional) — path to run pytest on (default: "." — pytest
      discovers tests itself).

    Output: the ``pytest`` Firewall compressor's structural JSON
    (``passed_count``, ``failed_count``, ``failed`` list with
    ``{test, error}`` entries, etc.).
    """

    name = "pytest"
    description = "Run pytest in the worktree (output compressed by the Firewall)."
    schema = {
        "type": "object",
        "properties": {
            "test_args": {
                "type": "array",
                "description": "extra pytest args (e.g. -x tests/test_x.py)",
                "items": {"type": "string"},
            },
            "path": {"type": "string", "description": "path to test (default: .)"},
        },
        "required": [],
    }

    def _build_argv(self, args: dict[str, Any]) -> list[str]:
        argv: list[str] = ["pytest"]
        extra = args.get("test_args") or []
        if not isinstance(extra, list):
            raise ValueError("test_args must be a list of strings")
        for tok in extra:
            if not isinstance(tok, str):
                raise ValueError("test_args must contain only strings")
            argv.append(tok)
        path = args.get("path")
        if path:
            if not isinstance(path, str):
                raise ValueError("path must be a string when given")
            argv.append(path)
        return argv

    def _kind(self, args: dict[str, Any]) -> str:
        return "pytest"


# ---------------------------------------------------------------------------
# BuiltinPlugin — bundles the four tools above
# ---------------------------------------------------------------------------


class BuiltinPlugin:
    """Built-in plugin bundling GrepTool, GitTool, RuffTool, PytestTool.

    Constructed once by :func:`gigaforge.plugins.registry.default_registry`.
    The plugin's tools are SHARED instances — calling code SHOULD construct
    fresh tools (with their own worktree) per task, not reuse the plugin's
    default instances. The plugin exists so callers iterating
    ``registry.all_tools()`` discover the four built-in tools; per-task
    construction is the caller's responsibility (so each task gets its own
    worktree-bound tool).
    """

    name = "builtin"

    def __init__(self) -> None:
        # Default tools point at cwd="."; callers that need a specific
        # worktree construct their own tool instances directly.
        self._tools: list[_BaseTool] = [
            GrepTool(),
            GitTool(),
            RuffTool(),
            PytestTool(),
        ]

    def tools(self) -> list[_BaseTool]:
        return list(self._tools)

    def on_load(self) -> None:
        # Nothing to set up; the global ALLOWED was already read at import.
        return None


# ---------------------------------------------------------------------------
# Convenience helper: build a Tool bound to a specific worktree.
# ---------------------------------------------------------------------------


def make_tool(name: str, *, worktree: Path, **kwargs: Any) -> _BaseTool:
    """Construct a fresh built-in tool instance bound to *worktree*.

    Used by the pipeline / MCP server / agents when they need a per-task
    tool instance (with its own worktree cwd and timeout). Raises
    ``KeyError`` if *name* is not a built-in tool name.
    """
    cls_map: dict[str, type[_BaseTool]] = {
        "grep": GrepTool,
        "git": GitTool,
        "ruff": RuffTool,
        "pytest": PytestTool,
    }
    cls = cls_map.get(name)
    if cls is None:
        raise KeyError(f"unknown built-in tool: {name!r} (known: {sorted(cls_map)})")
    return cls(worktree=worktree, **kwargs)


# ---------------------------------------------------------------------------
# Schema-invariants module-level check: ruff lint-safe & no unused imports.
# ---------------------------------------------------------------------------


class _ToolSummary(BaseModel):
    """Tiny dataclass for introspection: lists tools the plugin exposes."""

    name: str
    description: str
    schema_keys: list[str]


def tool_summaries(plugin: BuiltinPlugin) -> list[_ToolSummary]:
    return [
        _ToolSummary(
            name=t.name,
            description=t.description,
            schema_keys=list(t.schema.keys()),
        )
        for t in plugin.tools()
    ]


# Quick assertion that the grep compressor output stays bounded — used by
# callers (agents / MCP) to double-check the Firewall contract before
# forwarding to the LLM.
def output_token_count(output: dict[str, Any]) -> int:
    import json

    return estimate_tokens(json.dumps(output, ensure_ascii=False))

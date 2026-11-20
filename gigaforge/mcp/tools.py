"""MCP tool descriptors — map MCP names to internal Tool / harness calls.

The MCP server exposes a fixed catalog of GigaForge capabilities under
``gigaforge.*`` names so external MCP clients (Claude Desktop, Cline, etc.)
can call them with structured args.

Each descriptor is a flat JSON-schema object (no ``oneOf`` / ``anyOf`` /
``allOf``) — compatible with GigaChat's function-calling contract and
the MCP tools/list schema.

The dispatch layer is intentionally simple: each descriptor carries a
``handler`` coroutine that takes the args dict and returns a
:class:`gigaforge.plugins.base.ToolResult`. For the built-in tools (grep,
git, ruff, pytest) the handler is the corresponding Tool from the plugin
registry. For the higher-level GigaForge ops (slice, coder.edit, diff.apply,
tests.run) the handler is a function that constructs the right runtime
objects on demand and dispatches.

Why two layers (descriptors + handlers)? So the MCP ``tools/list`` response
is built from the descriptors (cheap, no worktree needed) and the
``tools/call`` response uses the handler (constructs a per-call worktree,
runs the underlying Tool, returns ToolResult.output).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from gigaforge.plugins.base import ToolResult

__all__ = [
    "MCP_TOOLS",
    "MCPToolDescriptor",
    "build_tool_descriptors",
    "default_tool_call_handler",
]

ToolCallHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


class MCPToolDescriptor:
    """One MCP tool descriptor: name + description + flat schema + handler."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: ToolCallHandler,
    ) -> None:
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler

    def to_mcp(self) -> dict[str, Any]:
        """Return the MCP ``tools/list`` entry shape for this descriptor."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


# ---------------------------------------------------------------------------
# Built-in MCP tools — handlers defined as module-level coroutines so they
# can be inspected and unit-tested without an MCPServer instance.
# ---------------------------------------------------------------------------


async def _handle_grep(args: dict[str, Any]) -> ToolResult:
    from gigaforge.plugins.builtin import GrepTool

    worktree = Path(args.pop("_worktree", "."))
    tool = GrepTool(worktree=worktree)
    return await tool.run(args)


async def _handle_git(args: dict[str, Any]) -> ToolResult:
    from gigaforge.plugins.builtin import GitTool

    worktree = Path(args.pop("_worktree", "."))
    tool = GitTool(worktree=worktree)
    return await tool.run(args)


async def _handle_ruff(args: dict[str, Any]) -> ToolResult:
    from gigaforge.plugins.builtin import RuffTool

    worktree = Path(args.pop("_worktree", "."))
    tool = RuffTool(worktree=worktree)
    return await tool.run(args)


async def _handle_pytest(args: dict[str, Any]) -> ToolResult:
    from gigaforge.plugins.builtin import PytestTool

    worktree = Path(args.pop("_worktree", "."))
    tool = PytestTool(worktree=worktree)
    return await tool.run(args)


async def _handle_localize(args: dict[str, Any]) -> ToolResult:
    """Build a Context Slice (0 LLM) from the L0 index.

    Args:
    - target_function: str (required) — qname of the function to slice.
    - file: str (required) — repo-relative path containing the function.
    - repo_root: str (required) — absolute path to the repo.
    - constraints: list[str] (optional) — extra constraints to bundle.
    """
    from gigaforge.firewall.slicer import ContextSlicer
    from gigaforge.l0.index import L0Index

    try:
        target = args["target_function"]
        file = args["file"]
        repo_root = Path(args["repo_root"])
    except KeyError as exc:
        return ToolResult(
            ok=False,
            output={"error": f"missing required arg: {exc}"},
            error=f"missing required arg: {exc}",
        )
    constraints = args.get("constraints") or []
    if not isinstance(constraints, list):
        return ToolResult(
            ok=False,
            output={"error": "constraints must be a list of strings"},
            error="constraints must be a list of strings",
        )
    if not repo_root.is_dir():
        return ToolResult(
            ok=False,
            output={"error": f"repo_root not a directory: {repo_root}"},
            error=f"repo_root not a directory: {repo_root}",
        )
    idx = L0Index()
    try:
        idx.index_repo(repo_root)
        slicer = ContextSlicer()
        slice_obj = slicer.build(
            idx,
            target_function=target,
            file=Path(file),
            constraints=[str(c) for c in constraints],
            test_signals={},
        )
        return ToolResult(
            ok=True,
            output=slice_obj.model_dump(mode="json"),
            error=None,
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of an MCP handler
        return ToolResult(
            ok=False,
            output={"error": f"{type(exc).__name__}: {exc}"},
            error=str(exc),
        )
    finally:
        idx.close()


async def _handle_slice(args: dict[str, Any]) -> ToolResult:
    """Alias for ``gigaforge.localize`` — same handler, MCP name differs."""
    return await _handle_localize(args)


async def _handle_coder_edit(args: dict[str, Any]) -> ToolResult:
    """Invoke the Coder agent to produce a unified diff.

    Args:
    - slice: dict (required) — Context Slice object from ``gigaforge.localize``.
    - constraints: list[str] (optional).
    - prev_rejection: str (optional) — reason for a previous failed apply.

    Returns the raw diff text in ``output["diff"]``. The MCP client is
    expected to follow up with ``gigaforge.diff.apply`` to validate+apply.
    """
    try:
        from gigaforge.agents.coder import CoderAgent
        from gigaforge.client.giga_client import ChatResult  # noqa: F401 — type re-export
        from gigaforge.firewall.slicer import ContextSlice
        from gigaforge.runtime import build_runtime
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            ok=False,
            output={"error": f"runtime unavailable: {type(exc).__name__}: {exc}"},
            error=str(exc),
        )
    slice_dict = args.get("slice")
    if not isinstance(slice_dict, dict):
        return ToolResult(
            ok=False,
            output={"error": "slice arg must be a dict (output of gigaforge.localize)"},
            error="slice arg must be a dict",
        )
    constraints = args.get("constraints") or []
    prev_rejection = args.get("prev_rejection")
    task_id = args.get("task_id") or "mcp-coder"
    try:
        slice_obj = ContextSlice.model_validate(slice_dict)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            ok=False,
            output={"error": f"invalid slice: {exc}"},
            error=str(exc),
        )
    rt = build_runtime()
    try:
        coder = CoderAgent(
            rt.client, rt.router, rt.budgets, rt.tracer, settings=rt.settings
        )
        diff = await coder.edit(
            slice_obj,
            [str(c) for c in constraints],
            task_id=task_id,
            prev_rejection=prev_rejection,
        )
        return ToolResult(
            ok=True,
            output={"diff": diff},
            error=None,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            ok=False,
            output={"error": f"{type(exc).__name__}: {exc}"},
            error=str(exc),
        )
    finally:
        await rt.aclose()


async def _handle_diff_apply(args: dict[str, Any]) -> ToolResult:
    """Validate + apply a unified diff via :class:`DiffValidator`.

    Args:
    - diff: str (required) — unified diff text.
    - repo_root: str (required) — absolute path to the repo.
    - worktree: str (optional) — alternate worktree path; default repo_root.
    """
    from gigaforge.core.diff_validator import DiffValidator
    from gigaforge.l0.index import L0Index

    diff = args.get("diff")
    if not isinstance(diff, str) or not diff:
        return ToolResult(
            ok=False,
            output={"error": "diff arg must be a non-empty string"},
            error="diff arg must be a non-empty string",
        )
    repo_root = args.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root:
        return ToolResult(
            ok=False,
            output={"error": "repo_root arg must be a non-empty string"},
            error="repo_root arg must be a non-empty string",
        )
    worktree = Path(args.get("worktree") or repo_root)
    idx = L0Index()
    try:
        validator = DiffValidator(index=idx)
        result = validator.validate_and_apply(
            diff, Path(repo_root), worktree=worktree
        )
        return ToolResult(
            ok=bool(result.ok),
            output=result.model_dump(mode="json"),
            error=None if result.ok else result.reason,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            ok=False,
            output={"error": f"{type(exc).__name__}: {exc}"},
            error=str(exc),
        )
    finally:
        idx.close()


async def _handle_tests_run(args: dict[str, Any]) -> ToolResult:
    """Run pytest in the worktree; output through the Firewall.

    Args:
    - repo_root: str (required) — worktree to run pytest in.
    - test_args: list[str] (optional).
    - path: str (optional).
    """
    repo_root = args.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root:
        return ToolResult(
            ok=False,
            output={"error": "repo_root arg must be a non-empty string"},
            error="repo_root arg must be a non-empty string",
        )
    forwarded = {k: v for k, v in args.items() if k != "repo_root"}
    return await _handle_pytest({"_worktree": repo_root, **forwarded})


# ---------------------------------------------------------------------------
# The static MCP tool catalog. Order matters: it's the order MCP clients see
# in tools/list.
# ---------------------------------------------------------------------------


MCP_TOOLS: list[MCPToolDescriptor] = [
    MCPToolDescriptor(
        name="gigaforge.localize",
        description=(
            "Build a Context Slice (0 LLM) for a target function via the "
            "L0 tree-sitter index. Returns the slice as a structured dict."
        ),
        schema={
            "type": "object",
            "properties": {
                "target_function": {"type": "string", "description": "qname of the function"},
                "file": {"type": "string", "description": "repo-relative path"},
                "repo_root": {"type": "string", "description": "absolute repo path"},
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "extra constraints (optional)",
                },
            },
            "required": ["target_function", "file", "repo_root"],
        },
        handler=_handle_localize,
    ),
    MCPToolDescriptor(
        name="gigaforge.slice",
        description="Alias for gigaforge.localize.",
        schema={
            "type": "object",
            "properties": {
                "target_function": {"type": "string"},
                "file": {"type": "string"},
                "repo_root": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["target_function", "file", "repo_root"],
        },
        handler=_handle_slice,
    ),
    MCPToolDescriptor(
        name="gigaforge.coder.edit",
        description=(
            "Invoke the Coder agent (GigaChat-2-Pro) to produce a unified "
            "diff for the given slice. Returns the raw diff text."
        ),
        schema={
            "type": "object",
            "properties": {
                "slice": {
                    "type": "object",
                    "description": "Context Slice dict from gigaforge.localize",
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "extra constraints",
                },
                "prev_rejection": {
                    "type": "string",
                    "description": "reason for a previous failed apply (optional)",
                },
                "task_id": {"type": "string", "description": "task id (optional)"},
            },
            "required": ["slice"],
        },
        handler=_handle_coder_edit,
    ),
    MCPToolDescriptor(
        name="gigaforge.diff.apply",
        description=(
            "Validate + apply a unified diff (exact -> fuzzy -> AST surgical). "
            "Returns DiffApplyResult fields: ok, reason, applied_files, method."
        ),
        schema={
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "unified diff text"},
                "repo_root": {"type": "string"},
                "worktree": {
                    "type": "string",
                    "description": "alternate worktree (default: repo_root)",
                },
            },
            "required": ["diff", "repo_root"],
        },
        handler=_handle_diff_apply,
    ),
    MCPToolDescriptor(
        name="gigaforge.tests.run",
        description=(
            "Run pytest in a worktree. Output is compressed by the Firewall; "
            "returns structural JSON (passed_count, failed_count, failed[]."
        ),
        schema={
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "test_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "extra pytest args",
                },
                "path": {"type": "string", "description": "path to test (optional)"},
            },
            "required": ["repo_root"],
        },
        handler=_handle_tests_run,
    ),
    # Pass-through to the built-in sandbox tools (so MCP clients can also
    # use them without going through the higher-level GigaForge ops).
    MCPToolDescriptor(
        name="gigaforge.grep",
        description="ripgrep over the repo (output through the Firewall).",
        schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "_worktree": {"type": "string", "description": "internal: worktree path"},
            },
            "required": ["pattern"],
        },
        handler=_handle_grep,
    ),
    MCPToolDescriptor(
        name="gigaforge.git",
        description="allowlisted git subcommand (status, log, diff).",
        schema={
            "type": "object",
            "properties": {
                "subcommand": {"type": "string", "enum": ["status", "log", "diff"]},
                "args": {"type": "array", "items": {"type": "string"}},
                "path": {"type": "string"},
                "_worktree": {"type": "string", "description": "internal: worktree path"},
            },
            "required": ["subcommand"],
        },
        handler=_handle_git,
    ),
    MCPToolDescriptor(
        name="gigaforge.ruff",
        description="lint (ruff check) or format (ruff format) Python code.",
        schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["lint", "format"]},
                "path": {"type": "string"},
                "_worktree": {"type": "string", "description": "internal: worktree path"},
            },
            "required": ["action", "path"],
        },
        handler=_handle_ruff,
    ),
    MCPToolDescriptor(
        name="gigaforge.pytest",
        description="run pytest in a worktree (raw form; prefer gigaforge.tests.run).",
        schema={
            "type": "object",
            "properties": {
                "test_args": {"type": "array", "items": {"type": "string"}},
                "path": {"type": "string"},
                "_worktree": {"type": "string", "description": "internal: worktree path"},
            },
            "required": [],
        },
        handler=_handle_pytest,
    ),
]


def build_tool_descriptors() -> list[dict[str, Any]]:
    """Return the MCP ``tools/list`` JSON for every descriptor."""
    return [t.to_mcp() for t in MCP_TOOLS]


def default_tool_call_handler() -> dict[str, ToolCallHandler]:
    """Return the ``{name -> handler}`` map for every descriptor.

    Used by :class:`MCPServer` to dispatch ``tools/call`` requests.
    """
    return {t.name: t.handler for t in MCP_TOOLS}

"""Plugin / tool registry for GigaForge.

Exposes GigaForge's capabilities (sandbox commands, repo index, diff
validation) as first-class :class:`Tool` objects that agents — or external
clients via the MCP server — can call. This bridges the C subagent's
tool-use loop: rather than hard-coding `subprocess.run` calls inside agents,
the agents call registered Tools by name.

Built-in plugins (``gigaforge.plugins.builtin``):
- :class:`GrepTool`    — ripgrep over the repo (output through the Firewall).
- :class:`GitTool`     — allowlisted git ops (status, log, diff).
- :class:`RuffTool`    — lint/format.
- :class:`PytestTool`  — run tests (output through the Firewall).

These wrap :class:`gigaforge.sandbox.executor.SandboxExecutor`; every Tool
result is the Firewall-compressed structural JSON that the LLM is allowed to
see (``result.output`` is already bounded — never raw subprocess output).

Function schemas stay FLAT (no ``oneOf`` / ``anyOf`` / ``allOf``) per the
GigaChat contract. :func:`validate_flat_schema` enforces this on
registration.

Public surface
--------------
- :class:`Tool`, :class:`ToolResult`, :class:`Plugin` (Protocols)
- :class:`PluginRegistry`, :func:`default_registry`
- :func:`validate_flat_schema`
"""
from gigaforge.plugins.base import (
    Plugin,
    Tool,
    ToolResult,
    validate_flat_schema,
)
from gigaforge.plugins.builtin import (
    BuiltinPlugin,
    GitTool,
    GrepTool,
    PytestTool,
    RuffTool,
)
from gigaforge.plugins.registry import (
    PluginRegistry,
    default_registry,
    load_extra_plugins,
    reset_default_registry,
)

__all__ = [
    "BuiltinPlugin",
    "GitTool",
    "GrepTool",
    "Plugin",
    "PluginRegistry",
    "PytestTool",
    "RuffTool",
    "Tool",
    "ToolResult",
    "default_registry",
    "load_extra_plugins",
    "reset_default_registry",
    "validate_flat_schema",
]

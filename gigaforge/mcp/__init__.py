"""GigaForge MCP (Model Context Protocol) server package.

Exposes GigaForge's capabilities — context slicing, diff validation, test
running, code edits — as MCP ``tools`` so external MCP clients (Claude
Desktop, Cline, etc.) can invoke them by name.

The server is a HAND-ROLLED minimal JSON-RPC 2.0 over stdio implementation.
No external MCP SDK dependency; ~200 LOC. It is a REFERENCE implementation
suitable for local development and integration testing — not a
production-grade server. Document your own extensions at the bottom of
``server.py``.

Public surface
--------------
- :class:`MCPServer` — JSON-RPC over stdio handler.
- :func:`run_stdio` — entry point for the ``gigaforge mcp`` CLI command.
- :data:`MCP_TOOLS` — static catalog of MCP tool descriptors.
- :func:`build_tool_descriptors` — MCP tool descriptors mapping MCP names
  to internal Tool calls (for introspection / tests).
- :func:`default_tool_call_handler` — the ``{name -> handler}`` map.
"""
from gigaforge.mcp.server import MCPServer, run_stdio
from gigaforge.mcp.tools import (
    MCP_TOOLS,
    build_tool_descriptors,
    default_tool_call_handler,
)

__all__ = [
    "MCP_TOOLS",
    "MCPServer",
    "build_tool_descriptors",
    "default_tool_call_handler",
    "run_stdio",
]

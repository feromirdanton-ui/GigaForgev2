"""Hand-rolled minimal MCP (Model Context Protocol) server over stdio.

This is a REFERENCE implementation suitable for local development and
integration testing — NOT a production-grade MCP server. It implements
JSON-RPC 2.0 over newline-delimited stdio:

- Client writes JSON-RPC requests (one per line) to the server's stdin.
- Server writes JSON-RPC responses (one per line) to its stdout.
- Notifications (requests without an ``id``) get NO response.
- Errors (parse errors, unknown methods, dispatcher exceptions) are
  returned as JSON-RPC ``error`` objects with code + message.

Supported methods:

- ``initialize``  — handshake; returns serverInfo + capabilities.
- ``initialized`` — notification from client (no response).
- ``ping``        — returns ``{}`` (alive check).
- ``tools/list``  — returns the static MCP tool catalog from
                   :mod:`gigaforge.mcp.tools`.
- ``tools/call``  — dispatches to the named handler; returns
                   ``{content: [{type: text, text: <json>}]}`` per the
                   MCP convention.
- ``shutdown``    — notification; server stops reading after handling.

No external MCP SDK dependency. ~200 LOC. Designed so a fake stdin/stdout
pair in tests can drive the server end-to-end without spawning a
subprocess.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from gigaforge.mcp.tools import build_tool_descriptors, default_tool_call_handler
from gigaforge.plugins.base import ToolResult

__all__ = ["MCPServer", "run_stdio"]

log = logging.getLogger(__name__)

# JSON-RPC error codes (per spec section 5.1 + MCP convention).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

_SERVER_NAME = "gigaforge"
_SERVER_VERSION = "0.1.0"
_PROTOCOL_VERSION = "2024-11-05"  # MCP spec version this stub implements.


ToolCallHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


class MCPServer:
    """Minimal JSON-RPC 2.0 over stdio MCP server.

    Construction
    ------------
    Pass an optional ``tool_handlers`` dict to override the default
    ``{name -> handler}`` map (tests do this to inject fakes). The
    default handlers are in :mod:`gigaforge.mcp.tools`.

    Lifecycle
    ---------
    - :meth:`handle(request)` — pure coroutine; takes a parsed JSON-RPC
      request dict, returns the response dict (or ``None`` for
      notifications). Test-friendly: no I/O.
    - :meth:`serve(reader, writer)` — the stdio loop. Reads newline-
      delimited JSON-RPC messages from *reader* (an asyncio.StreamReader),
      dispatches via :meth:`handle`, writes responses to *writer*.
    - :func:`run_stdio()` — entry point for the ``gigaforge mcp`` CLI
      command. Wires asyncio's stdin/stdout pipes to :meth:`serve`.
    """

    def __init__(
        self,
        *,
        tool_handlers: dict[str, ToolCallHandler] | None = None,
        name: str = _SERVER_NAME,
        version: str = _SERVER_VERSION,
    ) -> None:
        self._handlers: dict[str, ToolCallHandler] = (
            tool_handlers if tool_handlers is not None else default_tool_call_handler()
        )
        self._name = name
        self._version = version
        self._initialized = False

    # --- public dispatch --------------------------------------------------

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch ONE JSON-RPC request; return the response dict (or None).

        Returns ``None`` for notifications (requests without an ``id``).
        Returns a JSON-RPC ``error`` object for parse / unknown-method /
        dispatcher failures. Never raises — every error is wrapped.
        """
        if not isinstance(request, dict):
            return _error(None, _INVALID_REQUEST, "request must be a JSON object")

        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        is_notification = "id" not in request

        if not isinstance(method, str) or not method:
            return _error(req_id, _INVALID_REQUEST, "missing or invalid 'method'")

        try:
            if method == "initialize":
                response_result = self._handle_initialize(params)
            elif method == "initialized":
                self._initialized = True
                return None  # notification
            elif method == "ping":
                response_result = {}
            elif method == "tools/list":
                response_result = {"tools": build_tool_descriptors()}
            elif method == "tools/call":
                response_result = await self._handle_tools_call(params)
            elif method == "shutdown":
                # Notification per MCP — client follows with exit.
                return None
            else:
                return _error(
                    req_id, _METHOD_NOT_FOUND, f"unknown method: {method!r}"
                )
        except Exception as exc:  # noqa: BLE001 — defensive JSON-RPC boundary
            return _error(
                req_id, _INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
            )

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": response_result}

    # --- per-method handlers ----------------------------------------------

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # Per MCP spec, returns protocolVersion + serverInfo + capabilities.
        return {
            "protocolVersion": _PROTOCOL_VERSION,
            "serverInfo": {"name": self._name, "version": self._version},
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {},
                "prompts": {},
            },
        }

    async def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise ValueError("tools/call requires a 'name' string")
        if not isinstance(args, dict):
            raise ValueError("tools/call 'arguments' must be an object")
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"unknown tool: {name!r}")
        result: ToolResult = await handler(args)
        # MCP tools/call returns {content: [{type: text, text: <json>}],
        # isError: bool}. We serialize the ToolResult as JSON text.
        body = json.dumps(
            {"ok": result.ok, "output": result.output, "error": result.error},
            ensure_ascii=False,
        )
        return {
            "content": [{"type": "text", "text": body}],
            "isError": not result.ok,
        }

    # --- stdio loop --------------------------------------------------------

    async def serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Read newline-delimited JSON-RPC requests; write responses.

        Exits when the reader hits EOF (client closed stdin) OR when a
        ``shutdown`` notification arrives.
        """
        while True:
            try:
                line = await reader.readline()
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            if not line:
                # EOF — client closed stdin.
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            # Per MCP: shutdown notification ends the loop. Check BEFORE
            # dispatching so we don't emit a response for a notification.
            try:
                parsed_peek = json.loads(text)
                if isinstance(parsed_peek, dict) and parsed_peek.get("method") == "shutdown":
                    break
            except (json.JSONDecodeError, ValueError):
                pass
            response = await self._handle_raw_text(text)
            if response is None:
                continue
            payload = json.dumps(response, ensure_ascii=False) + "\n"
            writer.write(payload.encode("utf-8"))
            await writer.drain()
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — best-effort
            pass

    async def _handle_raw_text(self, text: str) -> dict[str, Any] | None:
        """Parse one line of JSON-RPC; dispatch; return response (or None)."""
        try:
            request = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            return _error(None, _PARSE_ERROR, f"invalid JSON: {exc}")
        if isinstance(request, list):
            # Batched JSON-RPC request — handle each, drop notifications.
            responses: list[dict[str, Any]] = []
            for item in request:
                if not isinstance(item, dict):
                    responses.append(_error(None, _INVALID_REQUEST, "batch item not an object"))
                    continue
                resp = await self.handle(item)
                if resp is not None:
                    responses.append(resp)
            return {"jsonrpc": "2.0", "id": None, "result": responses} if responses else None
        return await self.handle(request)


# ---------------------------------------------------------------------------
# Helpers + entry point
# ---------------------------------------------------------------------------


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


async def run_stdio(server: MCPServer | None = None) -> None:
    """Entry point for the ``gigaforge mcp`` CLI command.

    Wires asyncio's stdin/stdout pipe pair to :meth:`MCPServer.serve`.
    Exits when the client closes stdin OR sends a ``shutdown`` notification.
    """
    server = server or MCPServer()
    loop = asyncio.get_running_loop()

    # asyncio.StreamReader/Writer for stdin/stdout. We use the
    # connect_pipe_* helpers on Windows; on POSIX we use the legacy
    # sys.stdin.buffer / sys.stdout.buffer via asyncio streams.
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)

    if sys.platform == "win32":  # pragma: no cover — non-Linux dev path
        # On Windows we use the asyncio ProactorEventLoop's pipe support.
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        transport, _ = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(transport, _, None, loop)
    else:
        # POSIX: connect to the fd directly.
        try:
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        except Exception:
            # Fallback: read line-by-line from sys.stdin synchronously.
            # This is the safe path when stdin is a tty (e.g. interactive test).
            await _serve_fallback(server)
            return
        transport, _ = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(transport, _, None, loop)

    try:
        await server.serve(reader, writer)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _serve_fallback(server: MCPServer) -> None:
    """Fallback for environments where connect_read_pipe fails.

    Reads line-by-line from ``sys.stdin`` synchronously, dispatches via
    :meth:`MCPServer.handle`, and writes responses to ``sys.stdout``. Used
    when stdin is a TTY / non-pipe (e.g. when ``gigaforge mcp`` is invoked
    interactively for testing — not the normal MCP client path).
    """
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        text = line.strip()
        if not text:
            continue
        response = await server._handle_raw_text(text)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("method") == "shutdown":
                break
        except (json.JSONDecodeError, ValueError):
            pass

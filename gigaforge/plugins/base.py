"""Tool + Plugin abstract types + flat-schema validator.

A Tool is one named capability the harness exposes to agents and external
clients (MCP). Its ``schema`` MUST be a flat JSON object (no
``oneOf`` / ``anyOf`` / ``allOf``) per the GigaChat function-calling
contract — see :func:`validate_flat_schema`.

A Plugin is a bundle of Tools with an optional ``on_load`` hook (for setup,
caching, etc.). Plugins are registered into
:class:`gigaforge.plugins.registry.PluginRegistry` by name; the registry
exposes every plugin's tools via :meth:`all_tools` and looks up individual
tools via :meth:`tool`.

The :class:`ToolResult` shape is intentionally tiny:
``{ok: bool, output: dict, error: str | None}``. ``output`` is the
Firewall-compressed structural JSON suitable for direct LLM consumption
(it's already bounded — never raw subprocess output).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

__all__ = [
    "Plugin",
    "Tool",
    "ToolResult",
    "validate_flat_schema",
]

# The forbidden schema combinators — GigaChat rejects them. We enforce the
# same rule on every tool's schema so the harness stays compatible with the
# GigaChat function-calling path (even if a particular plugin's tool is only
# called via MCP today).
_FORBIDDEN_COMBINATORS = frozenset({"oneOf", "anyOf", "allOf", "not"})


class ToolResult(BaseModel):
    """The result of one :meth:`Tool.run` call.

    Fields
    ------
    ok:
        ``True`` iff the tool succeeded. ``False`` on validation failure,
        non-zero subprocess exit, or timeout.
    output:
        The Firewall-compressed structural JSON. Suitable for direct LLM
        consumption; bounded to ``firewall_limit_tokens`` (default 4000)
        by the underlying :class:`SandboxExecutor`.
    error:
        Human-readable error message when ``ok=False``; ``None`` on
        success. Suitable for feeding back to the LLM as a tool error.
    """

    ok: bool
    output: dict[str, Any]
    error: str | None = None


@runtime_checkable
class Tool(Protocol):
    """A named, schema-validated capability.

    ``name``        — short identifier (snake_case; no whitespace).
    ``description`` — one-line description for the LLM / MCP client.
    ``schema``      — flat JSON-schema object describing ``args``. MUST
        pass :func:`validate_flat_schema`.
    ``run(args)``   — async; returns :class:`ToolResult`. Never raises
        (errors surface as ``ToolResult(ok=False, error=...)``).
    """

    name: str
    description: str
    schema: dict[str, Any]

    async def run(self, args: dict[str, Any]) -> ToolResult: ...


@runtime_checkable
class Plugin(Protocol):
    """A bundle of related :class:`Tool` objects.

    ``name``     — short identifier (snake_case).
    ``tools()``  — returns the plugin's tools (called once on registration).
    ``on_load()`` — optional setup hook (caching, env validation, etc.).
    """

    name: str

    def tools(self) -> list[Tool]: ...

    def on_load(self) -> None: ...


def validate_flat_schema(schema: dict[str, Any]) -> None:
    """Raise :class:`ValueError` if *schema* uses any forbidden combinator.

    GigaChat function-calling rejects ``oneOf`` / ``anyOf`` / ``allOf`` /
    ``not``. The same flat-schema rule applies to every Tool in the
    harness — even tools only exposed via MCP — so we can later bridge
    MCP tool calls into GigaChat function calls without rewriting schemas.

    This checks the top-level schema dict AND every nested ``properties``
    entry AND every ``items`` schema (one level deep — enough to catch
    accidental combinator use in array-of-objects schemas). It is NOT a
    full JSON-Schema validator; it's a contract guard.
    """
    if not isinstance(schema, dict):
        raise ValueError(f"schema must be a dict, got {type(schema).__name__}")

    def _check(node: dict[str, Any], path: str) -> None:
        if not isinstance(node, dict):
            return
        for forbidden in _FORBIDDEN_COMBINATORS:
            if forbidden in node:
                raise ValueError(
                    f"schema at {path or '<root>'} uses forbidden combinator "
                    f"{forbidden!r} (GigaChat contract: flat schemas only)"
                )
        # Recurse into properties (object schemas) and items (array schemas).
        props = node.get("properties")
        if isinstance(props, dict):
            for prop_name, prop_schema in props.items():
                if isinstance(prop_schema, dict):
                    _check(prop_schema, f"{path}.{prop_name}" if path else prop_name)
        items = node.get("items")
        if isinstance(items, dict):
            _check(items, f"{path}[items]" if path else "[items]")

    _check(schema, "")

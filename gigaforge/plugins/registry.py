"""Plugin registry — register, look up, and discover plugins + their tools.

Auto-loads plugins from ``GIGAFORGE_PLUGINS=mod.path,other.mod`` env
(comma-separated module paths). Each module's ``register(registry)`` function
(if present) is called with the registry so it can register itself.

The default registry (``default_registry()``) is a module-level singleton
with the built-in :class:`BuiltinPlugin` pre-registered. Tests construct a
fresh ``PluginRegistry()`` for isolation.
"""
from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Callable

from gigaforge.plugins.base import Plugin, Tool, ToolResult, validate_flat_schema
from gigaforge.plugins.builtin import BuiltinPlugin

__all__ = ["PluginRegistry", "default_registry"]

log = logging.getLogger(__name__)


class PluginRegistry:
    """Register plugins by name; look up tools by name; list all tools.

    The registry calls ``plugin.on_load()`` on registration, then indexes
    every tool by its ``name`` attribute. If two plugins expose a tool with
    the same name, the LAST one registered wins (logged at WARNING).
    """

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._tools: dict[str, Tool] = {}

    # --- registration ------------------------------------------------------

    def register(self, plugin: Plugin) -> None:
        """Register *plugin* and all of its tools.

        Calls ``plugin.on_load()`` first (so the plugin can do setup).
        Validates every tool's schema via :func:`validate_flat_schema`.
        Logs a warning if a tool name collides with an existing one
        (last-writer-wins).
        """
        if not getattr(plugin, "name", ""):
            raise ValueError("plugin must have a non-empty 'name' attribute")
        name = plugin.name
        if name in self._plugins:
            log.warning("plugin %r is already registered; overwriting", name)
        # on_load — best-effort; failures are logged, not raised.
        try:
            plugin.on_load()
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("plugin %r on_load() failed: %s", name, exc)

        self._plugins[name] = plugin
        for tool in plugin.tools():
            # Validate the tool's schema is flat — no oneOf/anyOf/allOf.
            validate_flat_schema(tool.schema)
            if tool.name in self._tools:
                log.warning(
                    "tool name collision: %r from plugin %r overwrites %r",
                    tool.name, name, self._tools[tool.name],
                )
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a plugin and recompute the tool index.

        Tools belonging to the removed plugin are also removed; tools from
        OTHER plugins that previously lost the name collision are NOT
        re-instated (last-writer-wins on registration, last-writer-loses
        on removal would be confusing).
        """
        plugin = self._plugins.pop(name, None)
        if plugin is None:
            return
        # Remove tools owned by this plugin by name match.
        plugin_tool_names = {t.name for t in plugin.tools()}
        for tname in plugin_tool_names:
            existing = self._tools.get(tname)
            if existing is not None and existing in plugin.tools():
                self._tools.pop(tname, None)

    # --- lookup ------------------------------------------------------------

    def tool(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(
                f"unknown tool: {name!r} (registered: {sorted(self._tools)})"
            )
        return self._tools[name]

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def plugins(self) -> list[Plugin]:
        return list(self._plugins.values())

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # --- introspection / dispatch ----------------------------------------

    def describe(self) -> list[dict[str, object]]:
        """Return a list of ``{name, description, schema}`` for every tool."""
        out: list[dict[str, object]] = []
        for t in self.all_tools():
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "schema": t.schema,
                }
            )
        return out

    async def call(self, name: str, args: dict) -> ToolResult:
        """Convenience: look up tool *name* and invoke it with *args*.

        Raises ``KeyError`` if unknown. The tool's own ``run`` is expected
        not to raise (errors surface as ``ToolResult(ok=False, error=...)``)
        but we wrap defensively so a buggy tool doesn't crash the caller.
        """
        tool = self.tool(name)
        try:
            return await tool.run(args)
        except Exception as exc:  # noqa: BLE001 — defensive Tool boundary
            from gigaforge.plugins.base import ToolResult

            return ToolResult(
                ok=False,
                output={"error": f"tool {name!r} raised: {type(exc).__name__}: {exc}"},
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Module-level singleton + auto-loading helpers
# ---------------------------------------------------------------------------


_singleton: PluginRegistry | None = None


def default_registry() -> PluginRegistry:
    """Return the module-level singleton registry.

    Pre-loads the built-in :class:`BuiltinPlugin` (GrepTool, GitTool,
    RuffTool, PytestTool) and any plugins declared via env
    ``GIGAFORGE_PLUGINS=mod1,mod2``.
    """
    global _singleton
    if _singleton is None:
        _singleton = PluginRegistry()
        _singleton.register(BuiltinPlugin())
        load_extra_plugins(_singleton)
    return _singleton


def reset_default_registry() -> None:
    """For tests: clear the singleton so the next call rebuilds it."""
    global _singleton
    _singleton = None


def load_extra_plugins(
    registry: PluginRegistry, env: str | None = None
) -> list[str]:
    """Load plugins from ``GIGAFORGE_PLUGINS=mod.path,other.mod`` env.

    Each module path is imported; if the module exposes a
    ``register(registry)`` callable it is called with *registry*. Errors
    are logged and do NOT raise. Returns the list of successfully loaded
    module paths (so tests can assert).
    """
    raw = env if env is not None else os.environ.get("GIGAFORGE_PLUGINS", "")
    loaded: list[str] = []
    if not raw.strip():
        return loaded
    for tok in raw.split(","):
        path = tok.strip()
        if not path:
            continue
        try:
            mod = importlib.import_module(path)
        except Exception as exc:  # noqa: BLE001 — best-effort discovery
            log.warning("failed to import plugin module %r: %s", path, exc)
            continue
        register_fn: Callable[[PluginRegistry], None] | None = getattr(mod, "register", None)
        if callable(register_fn):
            try:
                register_fn(registry)
            except Exception as exc:  # noqa: BLE001
                log.warning("plugin module %r register() failed: %s", path, exc)
                continue
        loaded.append(path)
    return loaded

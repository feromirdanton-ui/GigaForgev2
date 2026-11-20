"""Provider registry — register, look up, and select providers by name.

Built-in registration:
- ``"giga"``  -> :class:`GigaChatProvider` (DEFAULT).
- ``"openai"`` -> :class:`OpenAICompatProvider` (registered LAZILY when
  ``GIGAFORGE_PROVIDER=openai`` is set AND the OpenAI env vars are present).

Selection:
- ``ProviderRegistry.default()`` returns the provider selected via
  ``GIGAFORGE_PROVIDER`` (default ``"giga"``). Falls back to the first
  registered provider if the env-selected name is not registered.
- ``ProviderRegistry.get(name)`` returns a specific provider; raises
  :class:`KeyError` (typed) if unknown.

The default registry is a module-level singleton (``default_registry()``);
the runtime wires the live GigaClient into it. Tests construct a fresh
registry to avoid global state leakage.
"""
from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Callable
from typing import Any

from gigaforge.providers.base import Provider, ProviderError, ProviderNotConfigured
from gigaforge.providers.gigachat import GigaChatProvider

__all__ = ["ProviderRegistry", "default_registry", "DEFAULT_PROVIDER_NAME"]

log = logging.getLogger(__name__)

DEFAULT_PROVIDER_NAME = "giga"

# Factory signature: takes no args, returns a constructed Provider (or raises
# ProviderNotConfigured). The registry holds factories so the provider isn't
# constructed until first use — keeps test start-up cheap and lets us lazy-load
# the OpenAICompatProvider only when its env vars are set.
ProviderFactory = Callable[[], Provider]


class ProviderRegistry:
    """Register providers by name; look up by name or env-selected default.

    The registry stores FACTORIES (not instances) so a provider is only
    constructed on first use. Once constructed, the instance is cached for
    the lifetime of the registry — callers get the same Provider back on
    subsequent ``get()`` / ``default()`` calls.
    """

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        self._instances: dict[str, Provider] = {}
        self._default_name: str = DEFAULT_PROVIDER_NAME

    # --- registration ------------------------------------------------------

    def register(self, name: str, factory: ProviderFactory) -> None:
        """Register a provider factory under *name*.

        If *name* is already registered, the new factory replaces the old
        and any cached instance under that name is cleared.
        """
        if not name:
            raise ValueError("provider name must be non-empty")
        self._factories[name] = factory
        self._instances.pop(name, None)

    def register_instance(self, name: str, provider: Provider) -> None:
        """Register an already-constructed provider instance under *name*."""
        if not name:
            raise ValueError("provider name must be non-empty")
        self._factories[name] = lambda: provider  # factory returns cached instance
        self._instances[name] = provider

    def names(self) -> list[str]:
        return sorted(self._factories)

    def set_default(self, name: str) -> None:
        if name not in self._factories:
            raise KeyError(f"cannot set default to unknown provider: {name!r}")
        self._default_name = name

    # --- lookup ------------------------------------------------------------

    def get(self, name: str) -> Provider:
        if name not in self._factories:
            raise KeyError(f"unknown provider: {name!r} (registered: {self.names()})")
        if name not in self._instances:
            factory = self._factories[name]
            try:
                self._instances[name] = factory()
            except ProviderNotConfigured:
                raise
        return self._instances[name]

    def default(self) -> Provider:
        """Return the env-selected (or built-in) default provider.

        Reads ``GIGAFORGE_PROVIDER`` lazily so test env mutations take effect
        on first call. If the env-selected provider isn't registered (or its
        factory raises ProviderNotConfigured), falls back to the built-in
        ``"giga"`` provider with a logged warning.
        """
        env_name = os.environ.get("GIGAFORGE_PROVIDER", "").strip() or self._default_name
        if env_name in self._factories:
            try:
                return self.get(env_name)
            except ProviderNotConfigured as exc:
                log.warning(
                    "provider %r not configured (%s); falling back to %r",
                    env_name, exc, self._default_name,
                )
        return self.get(self._default_name)

    def default_name(self) -> str:
        """The name the default selection currently resolves to (env-aware)."""
        env_name = os.environ.get("GIGAFORGE_PROVIDER", "").strip() or self._default_name
        if env_name in self._factories:
            return env_name
        return self._default_name

    # --- introspection -----------------------------------------------------

    def is_registered(self, name: str) -> bool:
        return name in self._factories

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._factories

    def __len__(self) -> int:
        return len(self._factories)

    def describe(self) -> list[dict[str, Any]]:
        """Return a list of ``{name, capabilities}`` for every provider that
        can be constructed without raising."""
        out: list[dict[str, Any]] = []
        for name in self.names():
            try:
                prov = self.get(name)
                caps = prov.capabilities()
                out.append({"name": name, "capabilities": caps.model_dump()})
            except ProviderNotConfigured as exc:
                out.append({"name": name, "error": str(exc)})
            except ProviderError as exc:
                out.append({"name": name, "error": str(exc)})
        return out


# ---------------------------------------------------------------------------
# Module-level singleton registry + helpers
# ---------------------------------------------------------------------------

def _build_default_registry() -> ProviderRegistry:
    """Construct the singleton registry with built-in factories.

    The ``"giga"`` factory is LAZY — it reads the runtime-attached GigaClient
    via :func:`_attach_giga_client`. Until the runtime attaches one, asking
    for ``"giga"`` raises ProviderNotConfigured (so tests that never touch
    the runtime can construct a fresh registry without credentials).
    """
    reg = ProviderRegistry()

    # The giga factory delegates to a holder that the runtime populates.
    reg.register("giga", _giga_factory)
    # The openai factory is opt-in; only raises if env missing AND someone
    # asks for it.
    reg.register("openai", _openai_factory)

    return reg


# Holder for the runtime-attached GigaClient. Set by Runtime.build_runtime().
_giga_client_holder: list[Any] = []  # 0 or 1 items


def attach_giga_client(client: Any) -> None:
    """Wire the live :class:`GigaClient` into the default registry.

    Called once by :func:`gigaforge.runtime.build_runtime` after the client
    is constructed. After this, ``default_registry().get("giga")`` returns a
    :class:`GigaChatProvider` wrapping that client.
    """
    _giga_client_holder.clear()
    _giga_client_holder.append(client)


def _giga_factory() -> Provider:
    """Default ``"giga"`` factory: wrap the runtime-attached GigaClient.

    Raises :class:`ProviderNotConfigured` if no GigaClient has been attached
    (e.g. in tests that never call :func:`build_runtime`).
    """
    if not _giga_client_holder:
        raise ProviderNotConfigured(
            "GigaChatProvider requires the runtime to be built first "
            "(call gigaforge.runtime.build_runtime() or pass an explicit "
            "GigaClient to GigaChatProvider())"
        )
    return GigaChatProvider(_giga_client_holder[0])


def _openai_factory() -> Provider:
    """Default ``"openai"`` factory: read env vars, construct the provider.

    Raises :class:`ProviderNotConfigured` if base_url or api_key is missing.
    """
    # Lazy import so this module doesn't drag httpx into test collection when
    # the test only uses GigaChat.
    from gigaforge.providers.openai_compat import OpenAICompatProvider

    return OpenAICompatProvider()  # raises ProviderNotConfigured if env missing


_singleton: ProviderRegistry | None = None


def default_registry() -> ProviderRegistry:
    """Return the module-level singleton registry.

    The singleton is created on first call. Use ``ProviderRegistry()`` in
    tests that want isolation; use this in production code.
    """
    global _singleton
    if _singleton is None:
        _singleton = _build_default_registry()
    return _singleton


def reset_default_registry() -> None:
    """For tests: clear the singleton so the next call rebuilds it."""
    global _singleton
    _singleton = None


def load_extra_providers(registry: ProviderRegistry, env: str | None = None) -> list[str]:
    """Load extra providers from a comma-separated module path env.

    ``GIGAFORGE_PROVIDERS=custom_providers.my_provider,other_pkg.other``
    imports each module path and calls its ``register(registry)`` function
    if present. Returns the list of successfully loaded module paths (so
    tests can assert). Errors are logged and do NOT raise.
    """
    raw = env if env is not None else os.environ.get("GIGAFORGE_PROVIDERS", "")
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
            log.warning("failed to import provider module %r: %s", path, exc)
            continue
        register_fn = getattr(mod, "register", None)
        if callable(register_fn):
            try:
                register_fn(registry)
            except Exception as exc:  # noqa: BLE001
                log.warning("provider module %r register() failed: %s", path, exc)
                continue
        loaded.append(path)
    return loaded

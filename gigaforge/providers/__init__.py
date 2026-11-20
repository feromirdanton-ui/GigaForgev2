"""Provider abstraction layer for GigaForge.

The harness speaks to LLM back-ends exclusively through the :class:`Provider`
protocol. The default provider is :class:`GigaChatProvider`, a thin facade over
the existing :class:`gigaforge.client.giga_client.GigaClient` — it changes NO
behaviour in the GigaChat path. The :class:`OpenAICompatProvider` is a
SEPARATE, opt-in adapter (selected via ``GIGAFORGE_PROVIDER=openai``) that
proves the abstraction is real without leaving the GigaChat doctrine: it does
NOT wrap GigaChat, it talks the OpenAI HTTP shape directly to a configurable
``base_url``.

This closes reviewer gap #6 (single-model dependency): callers can build /
select alternative providers without touching the pipeline, agents, or
GigaClient.

Public surface
--------------
- :class:`Provider` (Protocol) — the contract every backend must satisfy.
- :class:`ProviderCapabilities` — capability flags exposed by a provider.
- :class:`ProviderError`, :class:`ProviderNotConfigured` — typed errors.
- :class:`GigaChatProvider`, :class:`OpenAICompatProvider` — built-in providers.
- :class:`ProviderRegistry`, :func:`default_registry` — registration + lookup.
"""
from gigaforge.providers.base import (
    Provider,
    ProviderCapabilities,
    ProviderError,
    ProviderNotConfigured,
)
from gigaforge.providers.context_windows import (
    DEFAULT_CONTEXT_WINDOW,
    GIGACHAT_CONTEXT_WINDOWS,
    context_window_for,
    context_window_table,
    normalize_model_id,
)
from gigaforge.providers.gigachat import GigaChatProvider
from gigaforge.providers.gigachat_sdk_adapter import GigaChatSDKProvider, sdk_available
from gigaforge.providers.messages import nudge, normalize_messages
from gigaforge.providers.openai_compat import OpenAICompatProvider
from gigaforge.providers.registry import (
    ProviderRegistry,
    attach_giga_client,
    default_registry,
    load_extra_providers,
    reset_default_registry,
)

__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "GIGACHAT_CONTEXT_WINDOWS",
    "GigaChatProvider",
    "GigaChatSDKProvider",
    "OpenAICompatProvider",
    "context_window_for",
    "context_window_table",
    "normalize_messages",
    "normalize_model_id",
    "nudge",
    "sdk_available",
    "Provider",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderRegistry",
    "attach_giga_client",
    "default_registry",
    "load_extra_providers",
    "reset_default_registry",
]

"""GigaChatProvider — the DEFAULT provider; a thin facade over GigaClient.

This class changes NOTHING about the GigaChat path. It delegates every method
to the wrapped :class:`~gigaforge.client.giga_client.GigaClient` instance. The
existing :class:`~gigaforge.client.auth.TokenProvider`,
:class:`~gigaforge.client.ratelimit.RateLimiter`, and metrics sink are owned
by the wrapped client; this provider is just a typed Protocol-shaped handle.

Why have it? The pipeline / agents / CLI never import a concrete provider;
they go through :class:`ProviderRegistry.default()`. Wrapping GigaClient in
GigaChatProvider lets the rest of the harness stay provider-agnostic — the
OpenAICompatProvider (or any future adapter) is a drop-in replacement when
``GIGAFORGE_PROVIDER=openai`` is set.

Capabilities:
- ``streaming=True``    — GigaClient implements SSE-based stream_chat().
- ``function_calling=True`` — GigaClient parses function_call from BOTH
  ``message.function_call`` AND ``message.additional_kwargs.function_call``.
- ``max_context``       — resolved from the MEASURED table in
  :mod:`gigaforge.providers.context_windows` (261 120 tokens for every public
  chat model), not from the old 8 192 figure. Override per-process with
  ``GIGAFORGE_GIGA_MAX_CONTEXT`` or ``GIGACHAT_CONTEXT_WINDOW``.

Message shape
-------------
Every call is passed through :func:`gigaforge.providers.messages.normalize_messages`
before it reaches the transport, so the provider's hard constraint — *a system
message must be the first message* — holds no matter what a caller assembled.
Mid-conversation instructions (nudges) are built with
:func:`gigaforge.providers.messages.nudge` and travel as human messages.
"""
from __future__ import annotations

from typing import Any

from gigaforge.client.giga_client import ChatResult, GigaClient, StreamAccumulator
from gigaforge.providers.base import ProviderCapabilities
from gigaforge.providers.context_windows import (
    DEFAULT_CONTEXT_WINDOW,
    context_window_for,
    normalize_model_id,
)
from gigaforge.providers.messages import normalize_messages

__all__ = ["GigaChatProvider", "DEFAULT_CONTEXT_WINDOW"]

#: Kept for callers that imported the old constant. It is now the measured
#: default (261 120), not the stale 8 192.
_DEFAULT_MAX_CONTEXT = DEFAULT_CONTEXT_WINDOW


class GigaChatProvider:
    """DEFAULT provider. Wraps an existing :class:`GigaClient`; pure delegation.

    The wrapped client retains ownership of its TokenProvider + RateLimiter +
    metrics sink + httpx transport. ``aclose()`` delegates so callers who
    only hold a Provider reference can still close the underlying resources.

    ``max_context`` is resolved lazily per model: an explicit constructor
    argument wins, then the environment, then the measured table. That means a
    provider built once still reports the right window for a model the router
    switches to later in the run.
    """

    def __init__(
        self,
        client: GigaClient,
        *,
        max_context: int | None = None,
        default_model: str = "GigaChat-2-Pro",
    ) -> None:
        self._client = client
        self._max_context_override = max_context
        self._default_model = default_model

    # --- delegation to GigaClient --------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        stream: bool = False,
        functions: list[dict[str, Any]] | None = None,
        function_name_hint: str | None = None,
        task_id: str = "",
        phase: str = "unknown",
    ) -> ChatResult:
        return await self._client.chat(
            model=model,
            messages=normalize_messages(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            functions=functions,
            function_name_hint=function_name_hint,
            task_id=task_id,
            phase=phase,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        return await self._client.list_models()

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        functions: list[dict[str, Any]] | None = None,
        function_name_hint: str | None = None,
        task_id: str = "",
        phase: str = "unknown",
    ) -> StreamAccumulator:
        return self._client.stream_chat(
            model=model,
            messages=normalize_messages(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            functions=functions,
            function_name_hint=function_name_hint,
            task_id=task_id,
            phase=phase,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- capabilities --------------------------------------------------------

    def max_context_for(self, model: str | None = None) -> int:
        """Context window for *model* (override → env → measured table)."""
        return context_window_for(
            model or self._default_model,
            override=self._max_context_override,
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            function_calling=True,
            max_context=self.max_context_for(self._default_model),
            default_model=self._default_model,
        )

    def capabilities_for(self, model: str) -> ProviderCapabilities:
        """Capabilities with the window of the model actually being called."""
        return ProviderCapabilities(
            streaming=True,
            function_calling=True,
            max_context=self.max_context_for(model),
            default_model=model,
        )

    def context_window_table(self) -> dict[str, int]:
        """The measured model→window table (for UI / doctor output)."""
        from gigaforge.providers.context_windows import context_window_table

        return context_window_table()

    @staticmethod
    def normalize_model(model: str) -> str:
        """Expose the ID normalisation used for table lookups."""
        return normalize_model_id(model)

    @property
    def client(self) -> GigaClient:
        """Escape hatch for code that needs the underlying GigaClient.

        Used by :class:`gigaforge.runtime.Runtime` so the existing fields
        (``Runtime.client``) keep their typed :class:`GigaClient` view.
        """
        return self._client

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
- ``max_context=8192``  — GigaChat-2-Pro's published context window.
"""
from __future__ import annotations

from typing import Any

from gigaforge.client.giga_client import ChatResult, GigaClient, StreamAccumulator
from gigaforge.providers.base import ProviderCapabilities

__all__ = ["GigaChatProvider"]

# GigaChat-2-Pro's documented context window. Used as a conservative hint
# for budget pre-flight; the upstream model_catalog may report a different
# value at runtime (the live list_models() call). Override per-process via
# env GIGAFORGE_GIGA_MAX_CONTEXT if needed.
_DEFAULT_MAX_CONTEXT = 8192


class GigaChatProvider:
    """DEFAULT provider. Wraps an existing :class:`GigaClient`; pure delegation.

    The wrapped client retains ownership of its TokenProvider + RateLimiter +
    metrics sink + httpx transport. ``aclose()`` delegates so callers who
    only hold a Provider reference can still close the underlying resources.
    """

    def __init__(self, client: GigaClient, *, max_context: int = _DEFAULT_MAX_CONTEXT) -> None:
        self._client = client
        self._max_context = max_context

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
            messages=messages,
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
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            functions=functions,
            function_name_hint=function_name_hint,
            task_id=task_id,
            phase=phase,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            function_calling=True,
            max_context=self._max_context,
            default_model="GigaChat-2-Pro",
        )

    @property
    def client(self) -> GigaClient:
        """Escape hatch for code that needs the underlying GigaClient.

        Used by :class:`gigaforge.runtime.Runtime` so the existing fields
        (``Runtime.client``) keep their typed :class:`GigaClient` view.
        """
        return self._client

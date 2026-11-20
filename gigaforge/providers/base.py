"""Provider protocol + capabilities + typed errors.

A Provider is the LLM backend the harness talks to. Every backend — GigaChat
(default), an OpenAI-compatible endpoint (opt-in), a local stub for tests,
a future vLLM/Ollama adapter — implements this Protocol. The pipeline /
agents / CLI never import a concrete provider; they go through
:class:`gigaforge.providers.registry.ProviderRegistry`.

The Protocol deliberately mirrors :class:`~gigaforge.client.giga_client.GigaClient`
so :class:`GigaChatProvider` is a zero-cost facade (it delegates every method
to the wrapped client). New providers can implement the same shape with their
own transport.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from gigaforge.client.errors import GigaForgeError
from gigaforge.client.giga_client import ChatResult, StreamAccumulator

__all__ = [
    "ChatResult",
    "Provider",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderNotConfigured",
    "StreamAccumulator",
]


class ProviderError(GigaForgeError):
    """Generic provider-layer error (transport, config, response shape)."""


class ProviderNotConfigured(ProviderError):
    """The provider cannot be constructed because required config is missing.

    Raised e.g. when ``GIGAFORGE_PROVIDER=openai`` is set but
    ``GIGAFORGE_OPENAI_BASE_URL`` / ``GIGAFORGE_OPENAI_API_KEY`` are absent.
    """


class ProviderCapabilities(BaseModel):
    """Capability flags exposed by a provider so callers can branch.

    ``streaming``    — can the provider emit tokens via ``stream_chat()``?
    ``function_calling`` — does the provider accept a ``functions=[...]`` arg?
    ``max_context``  — upper bound on input tokens the provider accepts.
    ``default_model`` — model name to use when the caller passes ``None``.

    These are hints, NOT enforcement. Callers that need streaming should check
    ``capabilities.streaming`` before calling ``stream_chat``; providers that
    don't support streaming raise :class:`NotImplementedError` from it.
    """

    streaming: bool = False
    function_calling: bool = False
    # Measured GigaChat window (261 120), not the stale 8 192 figure. See
    # gigaforge.providers.context_windows for the per-model table.
    max_context: int = 261_120
    default_model: str = ""


@runtime_checkable
class Provider(Protocol):
    """LLM backend contract.

    Methods mirror :class:`~gigaforge.client.giga_client.GigaClient` so the
    GigaChat path is a zero-cost delegation; new providers implement the same
    surface with their own transport.
    """

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
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult: ...

    async def list_models(self) -> list[dict[str, Any]]: ...

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
        response_format: dict[str, Any] | None = None,
    ) -> StreamAccumulator: ...

    async def aclose(self) -> None: ...

    def capabilities(self) -> ProviderCapabilities: ...

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "",
    ) -> int | None: ...

"""OpenAICompatProvider — opt-in adapter for any OpenAI-compatible endpoint.

This is a STUB adapter proving the provider abstraction is real: GigaForge
can be pointed at any OpenAI-shape HTTP endpoint (a local vLLM/Ollama server,
a self-hosted llama.cpp, an OpenRouter gateway, etc.) WITHOUT wrapping
GigaChat and WITHOUT requiring GigaChat credentials.

Selected via ``GIGAFORGE_PROVIDER=openai`` (NOT the default). Configuration:

    GIGAFORGE_OPENAI_BASE_URL=https://api.openai.com/v1   (or your gateway)
    GIGAFORGE_OPENAI_API_KEY=sk-...                         (Bearer token)
    GIGAFORGE_OPENAI_MODEL=gpt-4o-mini                      (default model)
    GIGAFORGE_OPENAI_MAX_CONTEXT=128000                     (optional)

Capabilities:
- ``streaming=False`` — stub raises NotImplementedError(streaming).
- ``function_calling=False`` — stub raises NotImplementedError(functions).
- ``max_context`` — from env or default 8192.

The non-streaming ``chat()`` and ``list_models()`` use httpx directly, parse
the OpenAI response shape (``choices[0].message`` + ``usage``), and return
the same :class:`~gigaforge.client.giga_client.ChatResult` the GigaChat path
returns — so the agents / pipeline can stay unchanged.

This is the ONLY file in gigaforge/ that talks a non-GigaChat HTTP shape.
Nowhere in the GigaChat path is this class imported at module load — it is
loaded lazily by the registry when ``GIGAFORGE_PROVIDER=openai`` is set.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from gigaforge.client.errors import UpstreamError
from gigaforge.client.giga_client import ChatResult, StreamAccumulator
from gigaforge.providers.base import (
    ProviderCapabilities,
    ProviderError,
    ProviderNotConfigured,
)

__all__ = ["OpenAICompatProvider"]

# Default model — required; there is no portable "default" OpenAI model.
_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_MAX_CONTEXT = 128_000


class OpenAICompatProvider:
    """STUB adapter for any OpenAI-compatible endpoint.

    Construction
    -------------
    The constructor accepts explicit kwargs for tests; the
    :func:`gigaforge.providers.registry.default_registry` factory reads from
    env when ``GIGAFORGE_PROVIDER=openai``. Raises
    :class:`ProviderNotConfigured` if either of base_url or api_key is
    missing.

    Behaviour
    ---------
    - ``chat()``: POST ``{base_url}/chat/completions`` with
      ``Authorization: Bearer {api_key}`` and the OpenAI request shape
      (``model``, ``messages``, ``temperature``, ``max_tokens``). Parses the
      OpenAI response into a :class:`ChatResult` (``content``,
      ``finish_reason``, ``usage``). ``functions`` raises
      :class:`NotImplementedError` (capability gap surfaced explicitly).
    - ``list_models()``: GET ``{base_url}/models``, returns a list of
      ``{"id":..., "owned_by":...}`` dicts (the OpenAI shape).
    - ``stream_chat()``: raises :class:`NotImplementedError`. The
      capability flag is ``False`` so callers SHOULD check before invoking.
    - ``aclose()``: closes the underlying httpx.AsyncClient if we own it.

    Limitations
    -----------
    - No streaming. No function-calling. The capability flags reflect this
      so callers (agents, pipeline) can branch — the GigaChat path is
      still the only fully-featured provider.
    - No rate limiting, no metrics sink, no proactive token refresh. The
      GigaChat path owns those concerns; bringing them to OpenAICompat
      is a follow-up if/when the abstraction graduates from stub.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_context: int | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
    ) -> None:
        base_url = base_url or os.environ.get("GIGAFORGE_OPENAI_BASE_URL", "").strip()
        api_key = api_key or os.environ.get("GIGAFORGE_OPENAI_API_KEY", "").strip()
        if not base_url:
            raise ProviderNotConfigured(
                "GIGAFORGE_OPENAI_BASE_URL is required when GIGAFORGE_PROVIDER=openai "
                "(set it to your endpoint, e.g. https://api.openai.com/v1)"
            )
        if not api_key:
            raise ProviderNotConfigured(
                "GIGAFORGE_OPENAI_API_KEY is required when GIGAFORGE_PROVIDER=openai"
            )
        # Strip trailing slash so {base_url}/chat/completions composes cleanly.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model or os.environ.get("GIGAFORGE_OPENAI_MODEL", _DEFAULT_MODEL)
        self._max_context = max_context or int(
            os.environ.get("GIGAFORGE_OPENAI_MAX_CONTEXT", _DEFAULT_MAX_CONTEXT)
        )
        self._timeout = timeout
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    # --- public Provider surface -------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        stream: bool = False,
        functions: list[dict[str, Any]] | None = None,
        function_name_hint: str | None = None,
        task_id: str = "",
        phase: str = "unknown",
    ) -> ChatResult:
        if stream:
            raise NotImplementedError(
                "OpenAICompatProvider does not support stream=True; use "
                "stream_chat() (also not implemented in this stub). The "
                "GigaChatProvider is the only streaming-capable built-in provider."
            )
        if functions is not None:
            raise NotImplementedError(
                "OpenAICompatProvider does not support function-calling in "
                "this stub. Use the GigaChatProvider for function-calling "
                "or extend this adapter with tools/tool_choice support."
            )
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        try:
            resp = await self._http.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamError(None, f"openai_compat network error: {exc}") from exc

        if resp.status_code != 200:
            raise UpstreamError(resp.status_code, resp.text[:500])

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(f"openai_compat: invalid JSON response: {exc}") from exc

        return self._parse_chat_response(data)

    async def list_models(self) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            resp = await self._http.get(f"{self._base_url}/models", headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamError(None, f"openai_compat network error: {exc}") from exc
        if resp.status_code != 200:
            raise UpstreamError(resp.status_code, resp.text[:500])
        body = resp.json()
        if isinstance(body, dict):
            data = body.get("data", [])
            return [
                {"id": m.get("id", ""), "owned_by": m.get("owned_by", "openai")}
                for m in data
                if isinstance(m, dict)
            ]
        return []

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        functions: list[dict[str, Any]] | None = None,
        function_name_hint: str | None = None,
        task_id: str = "",
        phase: str = "unknown",
    ) -> StreamAccumulator:
        raise NotImplementedError(
            "OpenAICompatProvider.stream_chat is not implemented in this stub. "
            "Use the GigaChatProvider for SSE streaming."
        )

    async def aclose(self) -> None:
        if self._owns_http:
            aclose = getattr(self._http, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 — best-effort
                    pass

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=False,
            function_calling=False,
            max_context=self._max_context,
            default_model=self._model,
        )

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _parse_chat_response(data: dict[str, Any]) -> ChatResult:
        """Parse the OpenAI response shape into a :class:`ChatResult`."""
        choices = data.get("choices") or []
        msg: dict[str, Any] = {}
        finish_reason: str | None = None
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            finish_reason = choices[0].get("finish_reason")
        content = msg.get("content") or ""
        if isinstance(content, list):
            # OpenAI vision models return content as a list of parts.
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = data.get("usage")
        return ChatResult(
            content=content,
            function_call=msg.get("function_call") or None,
            usage=usage,
            finish_reason=finish_reason,
            estimated=False,
        )

    # Convenience for the MCP / runtime wiring: a stable string repr.
    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"OpenAICompatProvider(base_url={self._base_url!r}, model={self._model!r})"


# Silence unused-import lint for time — kept for future latency tracking.
_ = time

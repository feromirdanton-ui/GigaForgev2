"""Thin GigaChatProvider over the OFFICIAL ``gigachat`` SDK.

Why this exists
---------------
GigaForge used to own its whole transport: OAuth (:mod:`gigaforge.client.auth`),
retries, rate limiting, SSE parsing, response parsing.  All of that is a
re-implementation of what ``gigachat`` 0.3.x already does — and the official
SDK does it more completely (``chat_completions``, embeddings, vision, files,
``tokens_count``, ``rerank``, ContextVar tracing headers, ``with_options``).

This module is the replacement transport: a **thin adapter** that implements
GigaForge's existing :class:`~gigaforge.providers.base.Provider` protocol on top
of :class:`gigachat.GigaChatAsyncClient`.  It is deliberately small — it maps
message dicts to SDK models, calls the SDK, and maps the answer back to
:class:`~gigaforge.client.giga_client.ChatResult`.  No retry logic, no token
management, no SSE parser of its own.

What is preserved
-----------------
* :mod:`gigaforge.core.sync_adapter` is untouched.  The DAG is synchronous and
  the SDK is async; ``SyncChatClient`` remains the single bridge, and it works
  with this adapter exactly as it works with the legacy client (both expose
  ``async chat(messages, *, model=...)``).
* :class:`~gigaforge.client.giga_client.ChatResult` / ``StreamAccumulator`` are
  reused, so the router, budgets, metrics and trace code see the same shapes.

Retry policy — the one thing that must not be doubled
-----------------------------------------------------
The official SDK retries internally.  GigaForge also has a retry layer.  Two
layers means a single 429 can turn into N×M attempts.  The adapter therefore
constructs the SDK client with ``max_retries=0`` by default and lets GigaForge
own the policy (``retries="gigaforge"``); pass ``retries="sdk"`` to invert it.

Import safety
-------------
``gigachat`` is an OPTIONAL dependency: importing this module must not fail on a
machine that only uses the legacy client.  The SDK is imported inside
:meth:`GigaChatSDKProvider._sdk`, and a missing package raises
:class:`ProviderNotConfigured` with the install hint.
"""
from __future__ import annotations

import time
from typing import Any, AsyncIterator, Iterable, Mapping, Optional, Sequence

from gigaforge.client.giga_client import ChatResult, StreamAccumulator, StreamChunk
from gigaforge.providers.base import ProviderCapabilities, ProviderNotConfigured
from gigaforge.providers.context_windows import context_window_for
from gigaforge.providers.messages import normalize_messages

__all__ = [
    "GigaChatSDKProvider",
    "SDK_AVAILABLE",
    "sdk_available",
    "to_sdk_messages",
    "from_sdk_completion",
]

try:  # pragma: no cover - exercised by the environment, not by tests
    import gigachat as _gigachat  # noqa: F401

    SDK_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure means "not installed"
    _gigachat = None  # type: ignore[assignment]
    SDK_AVAILABLE = False


def sdk_available() -> bool:
    """True when the official ``gigachat`` package can be imported."""
    return SDK_AVAILABLE


# -- message / response mapping ---------------------------------------------
_ROLE_MAP = {
    "system": "system",
    "user": "user",
    "human": "user",
    "assistant": "assistant",
    "ai": "assistant",
    "model": "assistant",
    "tool": "function",
    "function": "function",
}


def to_sdk_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Map GigaForge message dicts to the SDK's ``Messages`` payload shape.

    Runs :func:`normalize_messages` first, so the system-message-first rule is
    enforced here too — this adapter cannot be used to build an illegal request.
    """
    out: list[dict[str, Any]] = []
    for message in normalize_messages(messages):
        role = _ROLE_MAP.get(str(message.get("role", "user")).lower(), "user")
        entry: dict[str, Any] = {"role": role, "content": message.get("content", "")}
        if message.get("name"):
            entry["name"] = message["name"]
        if message.get("function_call"):
            entry["function_call"] = message["function_call"]
        out.append(entry)
    return out


def _usage_dict(usage: Any) -> Optional[dict[str, int]]:
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        data = dict(usage)
    else:
        data = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        }
        cached = getattr(usage, "precached_prompt_tokens", None)
        if cached:
            data["cached_tokens"] = int(cached)
    return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}


def from_sdk_completion(completion: Any) -> ChatResult:
    """Map a ``ChatCompletion`` to GigaForge's :class:`ChatResult`."""
    choices = getattr(completion, "choices", None) or []
    content = ""
    function_call: Optional[dict[str, Any]] = None
    finish_reason: Optional[str] = None
    if choices:
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is not None:
            content = getattr(message, "content", "") or ""
            raw_call = getattr(message, "function_call", None)
            if raw_call is not None:
                function_call = {
                    "name": getattr(raw_call, "name", "") or "",
                    "arguments": dict(getattr(raw_call, "arguments", None) or {}),
                }
        finish_reason = getattr(choice, "finish_reason", None)
    return ChatResult(
        content=content,
        function_call=function_call,
        usage=_usage_dict(getattr(completion, "usage", None)),
        finish_reason=finish_reason,
        estimated=False,
    )


class GigaChatSDKProvider:
    """Provider protocol implemented over ``GigaChatAsyncClient``.

    ``client`` may be an already-constructed ``GigaChatAsyncClient`` (the normal
    case — the runtime builds it once) or ``None``, in which case one is built
    from ``credentials`` / ``scope`` / ``base_url`` on first use.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        credentials: Optional[str] = None,
        scope: str = "GIGACHAT_API_PERS",
        base_url: Optional[str] = None,
        model: str = "GigaChat-2-Pro",
        max_context: Optional[int] = None,
        retries: str = "gigaforge",
        verify_ssl: bool = True,
        timeout: float = 60.0,
    ) -> None:
        if retries not in {"gigaforge", "sdk"}:
            raise ValueError("retries must be 'gigaforge' or 'sdk'")
        self._client = client
        self._credentials = credentials
        self._scope = scope
        self._base_url = base_url
        self._model = model
        self._max_context = max_context
        self._retries = retries
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._owns_client = client is None

    # -- construction --------------------------------------------------------
    def _sdk(self) -> Any:
        if not SDK_AVAILABLE:
            raise ProviderNotConfigured(
                "the official 'gigachat' SDK is not installed; "
                "run `pip install gigachat>=0.3` or use the legacy GigaClient provider"
            )
        return _gigachat

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        sdk = self._sdk()
        if not self._credentials:
            raise ProviderNotConfigured(
                "GigaChatSDKProvider needs credentials (GIGACHAT_CREDENTIALS) "
                "or an already-constructed GigaChatAsyncClient"
            )
        kwargs: dict[str, Any] = {
            "credentials": self._credentials,
            "scope": self._scope,
            "model": self._model,
            # Never double-retry: GigaForge owns the policy unless told otherwise.
            "max_retries": 0 if self._retries == "gigaforge" else 2,
            "verify_ssl_certs": self._verify_ssl,
            "timeout": self._timeout,
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = sdk.GigaChatAsyncClient(**kwargs)
        return self._client

    # -- provider protocol ---------------------------------------------------
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
        if stream:
            raise ValueError("Use stream_chat() for streaming responses")
        client = self._ensure_client()
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": to_sdk_messages(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if functions:
            payload["functions"] = functions
        completion = await client.achat(payload)
        return from_sdk_completion(completion)

    async def list_models(self) -> list[dict[str, Any]]:
        client = self._ensure_client()
        models = await client.aget_models()
        data = getattr(models, "data", None) or []
        out: list[dict[str, Any]] = []
        for entry in data:
            out.append(
                {
                    "id": getattr(entry, "id_", None) or getattr(entry, "id", ""),
                    "owned_by": getattr(entry, "owned_by", "GigaChat"),
                }
            )
        return out

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
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": to_sdk_messages(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if functions:
            payload["functions"] = functions

        async def _gen() -> AsyncIterator[StreamChunk]:
            client = self._ensure_client()
            async for chunk in client.astream(payload):
                choices = getattr(chunk, "choices", None) or []
                delta = ""
                finish_reason = None
                if choices:
                    message = getattr(choices[0], "delta", None)
                    delta = getattr(message, "content", "") or "" if message else ""
                    finish_reason = getattr(choices[0], "finish_reason", None)
                yield StreamChunk(
                    content_delta=delta,
                    usage_or_none=_usage_dict(getattr(chunk, "usage", None)),
                    finish_reason=finish_reason,
                )

        return StreamAccumulator(_gen, started_at=time.monotonic())

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            closer = getattr(self._client, "aclose", None)
            if callable(closer):
                await closer()
        self._client = None

    # -- capabilities --------------------------------------------------------
    def max_context_for(self, model: str | None = None) -> int:
        return context_window_for(model or self._model, override=self._max_context)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            function_calling=True,
            max_context=self.max_context_for(self._model),
            default_model=self._model,
        )

    def capabilities_for(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            function_calling=True,
            max_context=self.max_context_for(model),
            default_model=model,
        )

    # -- extras the official SDK gives us for free ---------------------------
    async def tokens_count(self, texts: Sequence[str], *, model: str | None = None) -> list[int]:
        """Server-side token counting — replaces the char/4 estimate."""
        client = self._ensure_client()
        result = await client.atokens_count(list(texts), model=model or self._model)
        return [int(getattr(item, "tokens", 0) or 0) for item in result]

    async def embeddings(self, texts: Sequence[str], *, model: str = "Embeddings") -> list[list[float]]:
        client = self._ensure_client()
        response = await client.aembeddings(list(texts), model=model)
        return [list(getattr(item, "embedding", []) or []) for item in getattr(response, "data", [])]

    def describe(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "sdk_available": SDK_AVAILABLE,
            "retries": self._retries,
            "default_model": self._model,
            "max_context": self.max_context_for(self._model),
        }

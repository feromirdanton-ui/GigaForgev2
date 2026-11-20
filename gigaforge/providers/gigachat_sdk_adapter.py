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

import json
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


#: Tokens the chat template adds per message on top of its content: the role
#: token, the separators, and the turn marker.  Measured conservatively — the
#: real figure is model-dependent, and rounding up is the safe direction for a
#: guard that must not let an oversized request through.
MESSAGE_OVERHEAD_TOKENS = 4

#: Tokens the template adds once per request (the leading system marker and the
#: assistant priming suffix).
REQUEST_OVERHEAD_TOKENS = 3


def _message_overhead(messages: Sequence[Mapping[str, Any]]) -> int:
    """Per-message template cost the ``atokens_count`` endpoint does not bill.

    ``atokens_count`` counts content strings only.  A real chat request also
    pays for each message's role token and separators, plus a fixed per-request
    envelope.  Without this the guard under-counts by roughly
    ``4 * len(messages) + 3`` tokens — small per request, but a ReAct loop with
    a long trajectory accumulates it, and under-counting is exactly the failure
    mode that produces a 422 CONTEXT_TOO_LONG.
    """
    return REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS * len(messages)


def _functions_overhead(functions: Sequence[Mapping[str, Any]] | None) -> int:
    """Tokens the ``functions`` definitions add to the request.

    The endpoint cannot see them (it takes strings), but the server bills them
    on every call.  A JSON-serialised function definition costs roughly one
    token per four characters; the estimate rounds up.
    """
    if not functions:
        return 0
    try:
        serialized = json.dumps(list(functions), ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0
    return (len(serialized) + 3) // 4


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


def _running_loop_key() -> Any:
    """Identity of the event loop running right now, or ``None`` outside one.

    Used to key owned SDK clients: ``httpx.AsyncClient`` binds to the loop of
    its first request, so one cached client cannot serve two loops.
    """
    import asyncio

    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


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
        # The SDK's ``Usage`` names the prompt-cache counter
        # ``precached_prompt_tokens`` (verified against gigachat 0.3.0a1);
        # ``cached_tokens`` is accepted too so a future rename does not silently
        # drop the cache accounting and inflate the cost estimate.
        cached = getattr(usage, "precached_prompt_tokens", None)
        if cached is None:
            cached = getattr(usage, "cached_tokens", None)
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
    # A structured-output answer arrives as ``function_call.arguments`` with an
    # empty ``content``.  Every consumer in the harness reads ``result.content``
    # and parses it as JSON, so leaving it empty made a perfectly good answer
    # look like a failed generation.  Serialise the arguments back into
    # ``content`` \u2014 the shape the caller already knows how to parse \u2014 and keep
    # ``function_call`` for callers that want the structured form.
    if function_call and not content:
        try:
            content = json.dumps(function_call.get("arguments") or {}, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - arguments are JSON already
            content = ""
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

    #: This adapter does NOT own a retry loop: it builds the SDK client with
    #: ``max_retries=0`` so the policy is not doubled.  The runtime therefore
    #: wraps it in :class:`~gigaforge.providers.retrying.RetryingProvider`,
    #: which is the single retry owner on this transport.
    owns_retries = False

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
        #: Owned clients, one per event loop.  ``httpx.AsyncClient`` (which the
        #: SDK wraps) binds to the loop of its first request, so a single cached
        #: client fails with "cannot be used from a different event loop" as soon
        #: as a caller drives the provider from a second loop.
        self._clients: dict[Any, Any] = {}

    # -- construction --------------------------------------------------------
    def _sdk(self) -> Any:
        if not SDK_AVAILABLE:
            raise ProviderNotConfigured(
                "the official 'gigachat' SDK is not installed; "
                "run `pip install --pre gigachat==0.3.0a1` (the SDK has no stable "
                "release yet) or use the legacy GigaClient provider"
            )
        return _gigachat

    def _ensure_client(self) -> Any:
        """Return a client bound to the loop that is running right now.

        ``GigaChatAsyncClient`` wraps ``httpx.AsyncClient``, which binds itself
        to the event loop of its first request and then raises

            RuntimeError: ... cannot be used from a different event loop

        on any other loop.  GigaForge has (at least) two loops in play: the
        process-wide adapter loop in ``core/sync_adapter.py`` and the caller's
        own loop when the pipeline is driven from ``asyncio.run``.  Caching a
        single client therefore worked in a one-loop test and broke the moment
        a real caller used both \u2014 the ReAct episode fell back to
        ``react->linear`` every time.

        Keying the owned client by the running loop fixes that without leaking:
        each loop gets exactly one client, and ``aclose`` closes them all.
        """
        loop = _running_loop_key()
        cached = self._clients.get(loop)
        if cached is not None:
            return cached
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
        client = sdk.GigaChatAsyncClient(**kwargs)
        self._clients[loop] = client
        return client

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
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        if stream:
            raise ValueError("Use stream_chat() for streaming responses")
        client = self._ensure_client()
        payload = self._build_chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            functions=functions,
            response_format=response_format,
        )
        try:
            completion = await client.achat(payload)
        except BaseException as exc:  # noqa: BLE001 - re-raised, possibly translated
            # Translate the provider's overflow error into the one typed
            # exception the caller catches, so a 422/413 does not surface as a
            # raw SDK error the harness cannot classify.
            #
            # Imported here, not at module scope: ``context_window_guard``
            # imports ``providers.context_windows``, which imports this package's
            # ``__init__`` — a top-level import would close that cycle.
            from gigaforge.core.context_window_guard import (
                ContextOverflowError,
                is_context_overflow_error,
            )

            if is_context_overflow_error(exc):
                raise ContextOverflowError(
                    "GigaChat rejected the request because its context window "
                    f"was exceeded: {type(exc).__name__}",
                    context_window=self.max_context_for(model),
                    proactive=False,
                ) from exc
            raise
        return from_sdk_completion(completion)

    def _build_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        functions: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        """Build the SDK's typed ``Chat`` request model.

        The SDK is Pydantic-based; sending a raw dict works but skips validation
        and the field names it would catch.  Building ``Chat``/``Messages``
        explicitly is what §7.2 of the merge plan asks for, and it keeps the
        request shape honest when the SDK adds fields.
        """
        try:
            from gigachat.models import Chat, Function, Messages, MessagesRole
        except Exception:  # noqa: BLE001 - older SDK: fall back to the wire dict
            payload: dict[str, Any] = {
                "model": model or self._model,
                "messages": to_sdk_messages(messages),
                "temperature": temperature,
            }
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            if functions:
                payload["functions"] = functions
            if response_format:
                payload["response_format"] = response_format
            return payload

        role_map = {
            "system": MessagesRole.SYSTEM,
            "user": MessagesRole.USER,
            "assistant": MessagesRole.ASSISTANT,
            "tool": MessagesRole.FUNCTION,
            "function": MessagesRole.FUNCTION,
        }
        sdk_messages = []
        for message in normalize_messages(messages):
            role = role_map.get(str(message.get("role", "user")).lower(), MessagesRole.USER)
            sdk_messages.append(
                Messages(role=role, content=message.get("content", "") or "")
            )
        kwargs: dict[str, Any] = {
            "model": model or self._model,
            "messages": sdk_messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        # Structured output.  ``Chat.response_format`` is a real SDK field, but
        # the live API (measured 2026-11-20) accepts it and then IGNORES it: a
        # strict JSON Schema still came back as a fenced ```json block, and once
        # as free-form prose.  The mechanism the server actually enforces is
        # ``functions`` + ``function_call``: it validates the schema (422 with a
        # precise field path when it cannot) and returns the answer in
        # ``message.function_call.arguments`` with an EMPTY ``content``.
        #
        # So a schema is sent as a function.  ``response_format`` is still
        # included for forward compatibility \u2014 if the server starts honouring
        # it, nothing here breaks.
        if response_format:
            from gigaforge.agents.schemas import to_function_spec

            spec, call = to_function_spec(response_format)
            kwargs["functions"] = [Function(**spec)]
            kwargs["function_call"] = call
            kwargs["response_format"] = response_format
        if functions:
            allowed = set(Function.model_fields)
            declared = [
                Function(**{k: v for k, v in fn.items() if k in allowed})
                for fn in functions
                if isinstance(fn, Mapping)
            ]
            # MERGE, do not overwrite.  The structured-output schema above is
            # itself sent as a function, and ``function_call`` still points at
            # it; replacing the list here would leave ``function_call`` naming
            # a function the server cannot find, which is a hard 422.  The
            # latent bug only fires once a ReAct step asks for structured
            # output *and* declares tools — which is exactly what
            # ``react_step_response_format()`` is for.
            existing = list(kwargs.get("functions") or [])
            kwargs["functions"] = existing + declared
        return Chat(**kwargs)

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
        payload = self._build_chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            functions=functions,
        )

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
        """Close every client this provider built, whichever loop it belonged to.

        Clients are keyed by the loop that created them (see
        :meth:`_ensure_client`), so closing only ``self._client`` leaked one
        connection pool per loop the provider had been used from.
        """
        owned = [c for c in self._clients.values() if c is not None]
        self._clients.clear()
        if self._client is not None and self._owns_client:
            owned.append(self._client)
        for client in owned:
            closer = getattr(client, "aclose", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:  # noqa: BLE001 - shutdown is best effort
                    pass
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
    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "",
        functions: list[dict[str, Any]] | None = None,
    ) -> int | None:
        """Server-side token count for a message list.

        This is the real count from ``atokens_count``, not the char/4 estimate
        the context guard falls back to.  Returns ``None`` when the endpoint is
        unavailable so the caller keeps its estimate instead of failing.

        **What the endpoint does and does not count.**  ``atokens_count`` takes
        plain strings, so it counts the *content* of each message and nothing
        else.  A chat request also pays for the role tokens, the per-message
        template overhead, and — in the ReAct loop — the ``functions``
        definitions.  Counting only content therefore *under*-counts, and for a
        guard whose job is to prevent a 422 CONTEXT_TOO_LONG, under-counting is
        the dangerous direction: it lets through a request the server rejects.

        The overhead is added explicitly here: a fixed per-message cost for the
        role/template envelope, plus the serialised size of the function
        definitions.  The constants are deliberately conservative (they round
        up), because a guard that compacts slightly early costs one summariser
        call, while a guard that compacts too late costs a failed request and a
        whole step of the budget.
        """
        client = self._ensure_client()
        normalized = normalize_messages(messages)
        texts = [str(m.get("content", "") or "") for m in normalized]
        if not texts and not functions:
            return 0
        try:
            result = await client.atokens_count(texts, model=model or self._model)
        except Exception:  # noqa: BLE001 - counting is an optimisation
            return None
        content_tokens = sum(int(getattr(item, "tokens", 0) or 0) for item in result)
        return content_tokens + _message_overhead(normalized) + _functions_overhead(functions)

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

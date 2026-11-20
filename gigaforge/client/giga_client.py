"""GigaClient — the SINGLE point of contact with the GigaChat HTTP API.

Direct httpx, no LangChain / LiteLLM / OpenAI-style wrappers.

Public surface:
    ChatResult       — pydantic model returned from chat()
    StreamChunk      — pydantic model yielded by stream_chat() iteration
    StreamAccumulator — async-iterable wrapper exposing .result() after iteration
    GigaClient       — async client (chat, stream_chat, list_models)

Reliability policy:
    - 429  -> read Retry-After; ratelimiter.update_retry_after; retry (max 5)
    - 5xx  -> exponential backoff w/ jitter, max 5 attempts
    - net/timeout -> exponential backoff w/ jitter, max 5 attempts
    - 401  -> token_provider.invalidate(); ONE refresh + ONE retry; second 401 -> AuthError
    - metrics_sink called exactly once per public call (success or failure)
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel

from gigaforge.client.auth import TokenProvider
from gigaforge.client.errors import (
    AuthError,
    FunctionCallMissing,
    RateLimitError,
    UpstreamError,
)
from gigaforge.client.ratelimit import RateLimiter
from gigaforge.client.sse import parse_sse_stream
from gigaforge.config import Settings, get_settings

__all__ = ["ChatResult", "StreamChunk", "StreamAccumulator", "GigaClient"]

MetricsSink = Callable[[dict[str, Any]], None]
SleepFn = Callable[[float], Awaitable[None]]
JitterFn = Callable[[], float]


class ChatResult(BaseModel):
    content: str = ""
    function_call: dict[str, Any] | None = None  # {"name": str, "arguments": dict}
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    estimated: bool = False  # True if usage was estimated from chars


class StreamChunk(BaseModel):
    content_delta: str = ""
    usage_or_none: dict[str, int] | None = None
    function_call: dict[str, Any] | None = None
    finish_reason: str | None = None


def _parse_function_call(fc: dict[str, Any]) -> dict[str, Any]:
    """Normalise a function_call: name + arguments-as-dict.

    `arguments` may arrive as a JSON string OR a parsed object. Defensive:
    on JSON parse failure the raw string is preserved as `{"_raw": ...}`.
    """
    name = fc.get("name")
    args: Any = fc.get("arguments", {})
    if isinstance(args, str):
        if args.strip() == "":
            args = {}
        else:
            try:
                parsed = json.loads(args)
                args = parsed if isinstance(parsed, dict) else {"value": parsed}
            except (json.JSONDecodeError, ValueError):
                args = {"_raw": args}
    elif not isinstance(args, dict):
        args = {"value": args}
    return {"name": name, "arguments": args}


def _extract_message_function_call(message: dict[str, Any]) -> dict[str, Any] | None:
    """Check BOTH top-level message.function_call AND message.additional_kwargs.function_call."""
    fc = message.get("function_call")
    if fc is None:
        kw = message.get("additional_kwargs") or {}
        if isinstance(kw, dict):
            fc = kw.get("function_call")
    if not isinstance(fc, dict):
        return None
    return _parse_function_call(fc)


class StreamAccumulator:
    """Async-iterable wrapper over a stream_chat() session.

    Iterate to receive content deltas; after iteration ends, `.result()`
    returns the assembled ChatResult. If the stream ended without a usage
    block, usage is estimated from `len(content) // 4` and `estimated=True`.
    """

    def __init__(
        self,
        gen_factory: Callable[[], AsyncIterator[StreamChunk]],
        *,
        on_complete: Callable[[ChatResult | None, int, bool, int], None] | None = None,
        started_at: float | None = None,
    ) -> None:
        self._gen_factory = gen_factory
        self._on_complete = on_complete
        self._started = started_at if started_at is not None else time.monotonic()
        self._content_parts: list[str] = []
        self._usage: dict[str, int] | None = None
        self._function_call: dict[str, Any] | None = None
        self._finish_reason: str | None = None
        self._result: ChatResult | None = None
        self._retries = 0

    def set_retries(self, n: int) -> None:
        self._retries = n

    def __aiter__(self) -> AsyncIterator[StreamChunk]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[StreamChunk]:
        ok = False
        try:
            async for chunk in self._gen_factory():
                if chunk.content_delta:
                    self._content_parts.append(chunk.content_delta)
                if chunk.usage_or_none:
                    self._usage = chunk.usage_or_none
                if chunk.function_call:
                    self._function_call = chunk.function_call
                if chunk.finish_reason:
                    self._finish_reason = chunk.finish_reason
                yield chunk
            ok = True
        finally:
            content = "".join(self._content_parts)
            estimated = False
            usage = self._usage
            if usage is None:
                if content:
                    est_tokens = max(1, len(content) // 4)
                    usage = {
                        "prompt_tokens": 0,
                        "completion_tokens": est_tokens,
                        "total_tokens": est_tokens,
                    }
                    estimated = True
                else:
                    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            self._result = ChatResult(
                content=content,
                function_call=self._function_call,
                usage=usage,
                finish_reason=self._finish_reason,
                estimated=estimated,
            )
            if self._on_complete is not None:
                latency_ms = int((time.monotonic() - self._started) * 1000)
                self._on_complete(self._result, latency_ms, ok, self._retries)

    def result(self) -> ChatResult:
        if self._result is None:
            raise RuntimeError(
                "StreamAccumulator.result() requires the async iteration to have completed."
            )
        return self._result


class GigaClient:
    """Async client. The orchestrator holds one instance for the whole run."""

    MAX_ATTEMPTS = 5

    def __init__(
        self,
        token_provider: TokenProvider,
        ratelimiter: RateLimiter,
        *,
        metrics_sink: MetricsSink | None = None,
        http_client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
        sleep_fn: SleepFn | None = None,
        jitter_fn: JitterFn | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._ratelimiter = ratelimiter
        self._metrics_sink: MetricsSink = metrics_sink or (lambda r: None)
        self._settings = settings or get_settings()
        self._owns_http = http_client is None
        if http_client is None:
            http_client = httpx.AsyncClient(verify=self._settings.verify)
        self._http = http_client
        self._sleep = sleep_fn or asyncio.sleep
        self._jitter = jitter_fn or (lambda: random.uniform(0, 0.1))

    async def aclose(self) -> None:
        if self._owns_http:
            aclose = getattr(self._http, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 — best-effort
                    pass

    async def __aenter__(self) -> GigaClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # --- internals -------------------------------------------------------

    def _auth_headers(self, token: str) -> dict[str, str]:
        # Bearer scheme only — NEVER giga-auth-<token>.
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _stream_timeout(self) -> httpx.Timeout:
        """Per-read timeout = idle-between-chunks; connect = timeout_connect."""
        s = self._settings
        return httpx.Timeout(
            connect=s.timeout_connect,
            read=s.timeout_stream_idle,
            write=s.timeout_connect,
            pool=s.timeout_connect,
        )

    def _chat_timeout(self) -> httpx.Timeout:
        """Non-streaming chat: read timeout = whole-response cap = timeout_total."""
        s = self._settings
        return httpx.Timeout(
            connect=s.timeout_connect,
            read=s.timeout_total,
            write=s.timeout_connect,
            pool=s.timeout_connect,
        )

    @staticmethod
    def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
        """Cheap pre-flight token estimate used to feed the RateLimiter.

        Returns ``max(1, total)`` so the limiter always books at least one
        token against the budget — an empty messages list would otherwise
        bypass the limiter entirely, which is the wrong default (the request
        still costs a network round-trip and the model's response consumes
        TPM even if the prompt is empty).

        NOTE (LOW-09): this estimate is for the LIMITER ONLY. The actual
        ``usage`` block GigaChat returns in :class:`ChatResult.usage` may
        legitimately be ``0`` (e.g., a cached/short-circuited response or a
        function-call-only turn). ``_record`` does NOT call this method —
        it copies ``prompt_tokens`` / ``completion_tokens`` straight from
        the response's ``usage`` dict, so a real 0 stays a real 0 in
        metrics; the ``max(1, ...)`` floor here is purely a limiter-side
        guard and never reaches the metrics sink.
        """
        total = 0
        for m in messages:
            content = m.get("content", "") if isinstance(m, dict) else ""
            total += len(str(content)) // 4
        return max(1, total)

    def _backoff_delay(self, attempt: int) -> float:
        # attempt is 0-indexed. base 0.4s -> ~0.4, 0.8, 1.6, 3.2, 6.4 + jitter.
        base = 0.4
        return base * (2 ** attempt) + self._jitter()

    def _record(
        self,
        *,
        task_id: str,
        phase: str,
        model: str,
        usage: dict[str, int] | None,
        estimated: bool,
        latency_ms: int,
        retries: int,
        ok: bool,
    ) -> None:
        self._metrics_sink(
            {
                "ts": time.time(),
                "task_id": task_id,
                "phase": phase,
                "model": model,
                "tokens_in": (usage or {}).get("prompt_tokens", 0),
                "tokens_out": (usage or {}).get("completion_tokens", 0),
                "estimated": estimated,
                "latency_ms": latency_ms,
                "retries": retries,
                "ok": ok,
                "cache_hit": False,
            }
        )

    # --- public API -----------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        token = await self._token_provider.aget_token()
        resp = await self._http.get(
            f"{self._settings.base_url}/models",
            headers=self._auth_headers(token),
            timeout=self._chat_timeout(),
        )
        if resp.status_code != 200:
            raise UpstreamError(resp.status_code, resp.text)
        body = resp.json()
        if isinstance(body, dict):
            return list(body.get("data", []))
        return []

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        functions: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        stream: bool = False,
        function_name_hint: str | None = None,
        task_id: str = "",
        phase: str = "unknown",
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        if stream:
            raise ValueError("Use stream_chat() for streaming responses")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if functions:
            payload["functions"] = functions
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        # Native structured output.  The live API (measured 2026-11-20) accepts
        # ``response_format`` and then IGNORES it: a strict JSON Schema still
        # came back as a fenced ```json block, and once as free-form prose.
        # The mechanism the server actually enforces is ``functions`` +
        # ``function_call`` — it validates the schema (422 with a precise field
        # path when it cannot) and returns the answer in
        # ``message.function_call.arguments`` with an EMPTY ``content``.
        #
        # Translating here rather than in the SDK adapter is what makes
        # structured output real in the DEFAULT configuration: the default
        # provider is the legacy one, so a translation that lived only in the
        # SDK adapter was unreachable unless GIGAFORGE_PROVIDER was set.
        if response_format:
            from gigaforge.agents.schemas import to_function_spec

            spec, call = to_function_spec(response_format)
            payload["functions"] = list(payload.get("functions") or []) + [spec]
            payload["function_call"] = call
            # Kept for forward compatibility: if the server starts honouring
            # it, nothing here breaks.
            payload["response_format"] = response_format

        started = time.monotonic()
        retries = 0
        refreshed_401 = False
        last_exc: Exception | None = None

        for attempt in range(self.MAX_ATTEMPTS):
            await self._ratelimiter.acquire(self._estimate_tokens(messages))
            token = await self._token_provider.aget_token()
            try:
                resp = await self._http.post(
                    f"{self._settings.base_url}/chat/completions",
                    json=payload,
                    headers=self._auth_headers(token),
                    timeout=self._chat_timeout(),
                )
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                if attempt + 1 < self.MAX_ATTEMPTS:
                    retries += 1
                    await self._sleep(self._backoff_delay(attempt))
                    continue
                self._record(
                    task_id=task_id, phase=phase, model=model, usage=None,
                    estimated=False, latency_ms=int((time.monotonic() - started) * 1000),
                    retries=retries, ok=False,
                )
                raise UpstreamError(None, f"network/timeout after {attempt + 1} attempts: {e}") from e

            if resp.status_code == 401:
                if refreshed_401:
                    self._record(
                        task_id=task_id, phase=phase, model=model, usage=None,
                        estimated=False, latency_ms=int((time.monotonic() - started) * 1000),
                        retries=retries, ok=False,
                    )
                    raise AuthError(f"repeated 401 after token refresh: {resp.text[:200]}")
                await self._token_provider.ainvalidate()
                refreshed_401 = True
                retries += 1
                continue

            if resp.status_code == 429:
                retry_after_raw = resp.headers.get("Retry-After", "1")
                try:
                    retry_after = float(retry_after_raw)
                except (TypeError, ValueError):
                    retry_after = 1.0
                self._ratelimiter.update_retry_after(retry_after)
                last_exc = RateLimitError(
                    f"429: {resp.text[:200]}", status=429, retry_after=retry_after
                )
                if attempt + 1 < self.MAX_ATTEMPTS:
                    retries += 1
                    # The next acquire() will block on the retry-after deadline.
                    continue
                self._record(
                    task_id=task_id, phase=phase, model=model, usage=None,
                    estimated=False, latency_ms=int((time.monotonic() - started) * 1000),
                    retries=retries, ok=False,
                )
                raise last_exc

            if resp.status_code >= 500:
                last_exc = UpstreamError(resp.status_code, resp.text[:200])
                if attempt + 1 < self.MAX_ATTEMPTS:
                    retries += 1
                    await self._sleep(self._backoff_delay(attempt))
                    continue
                self._record(
                    task_id=task_id, phase=phase, model=model, usage=None,
                    estimated=False, latency_ms=int((time.monotonic() - started) * 1000),
                    retries=retries, ok=False,
                )
                raise last_exc

            if resp.status_code != 200:
                self._record(
                    task_id=task_id, phase=phase, model=model, usage=None,
                    estimated=False, latency_ms=int((time.monotonic() - started) * 1000),
                    retries=retries, ok=False,
                )
                raise UpstreamError(resp.status_code, resp.text[:200])

            data = resp.json()
            result = self._parse_chat_response(data)

            if function_name_hint and result.function_call is None:
                self._record(
                    task_id=task_id, phase=phase, model=model, usage=result.usage,
                    estimated=result.estimated,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    retries=retries, ok=False,
                )
                raise FunctionCallMissing(
                    f"expected function_call {function_name_hint!r}; model returned text"
                )

            self._record(
                task_id=task_id, phase=phase, model=model, usage=result.usage,
                estimated=result.estimated,
                latency_ms=int((time.monotonic() - started) * 1000),
                retries=retries, ok=True,
            )
            return result

        # Defensive — loop exited without explicit return/raise.
        self._record(
            task_id=task_id, phase=phase, model=model, usage=None,
            estimated=False, latency_ms=int((time.monotonic() - started) * 1000),
            retries=retries, ok=False,
        )
        raise last_exc if last_exc is not None else UpstreamError(None, "exhausted retries")

    def _parse_chat_response(self, data: dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or []
        msg: dict[str, Any] = {}
        finish_reason: str | None = None
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            finish_reason = choices[0].get("finish_reason")
        content = msg.get("content") or ""
        if isinstance(content, list):
            # Some chat APIs return content as a list of parts — join the text ones.
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        function_call = _extract_message_function_call(msg) if msg else None
        usage = data.get("usage")
        # Structured output arrives as a function call with an EMPTY content:
        # the server puts the validated JSON in ``function_call.arguments``.
        # Callers that asked for a schema read ``content``, so surface the
        # arguments there — but ONLY for the synthetic structured-output
        # function, never for a real tool call (the ReAct loop reads
        # ``function_call`` itself and would otherwise see its own arguments
        # echoed as prose).
        if (
            not content
            and function_call
            and function_call.get("name") == "structured_output"
        ):
            content = json.dumps(function_call.get("arguments") or {}, ensure_ascii=False)
        return ChatResult(
            content=content,
            function_call=function_call,
            usage=usage,
            finish_reason=finish_reason,
            estimated=False,
        )

    def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        functions: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        function_name_hint: str | None = None,
        task_id: str = "",
        phase: str = "unknown",
    ) -> StreamAccumulator:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if functions:
            payload["functions"] = functions
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        started = time.monotonic()
        state = {"retries": 0}

        def on_complete(result: ChatResult | None, latency_ms: int, ok: bool, retries: int) -> None:
            self._record(
                task_id=task_id, phase=phase, model=model,
                usage=result.usage if result else None,
                estimated=result.estimated if result else False,
                latency_ms=latency_ms, retries=retries, ok=ok,
            )

        async def gen() -> AsyncIterator[StreamChunk]:
            refreshed_401 = False
            last_exc: Exception | None = None
            for attempt in range(self.MAX_ATTEMPTS):
                await self._ratelimiter.acquire(self._estimate_tokens(messages))
                token = await self._token_provider.aget_token()
                try:
                    async with self._http.stream(
                        "POST",
                        f"{self._settings.base_url}/chat/completions",
                        json=payload,
                        headers=self._auth_headers(token),
                        timeout=self._stream_timeout(),
                    ) as resp:
                        status = resp.status_code
                        if status == 401:
                            if refreshed_401:
                                raise AuthError("repeated 401 after token refresh (stream)")
                            await self._token_provider.ainvalidate()
                            refreshed_401 = True
                            state["retries"] += 1
                            continue
                        if status == 429:
                            retry_after_raw = resp.headers.get("Retry-After", "1")
                            try:
                                retry_after = float(retry_after_raw)
                            except (TypeError, ValueError):
                                retry_after = 1.0
                            self._ratelimiter.update_retry_after(retry_after)
                            state["retries"] += 1
                            last_exc = RateLimitError(
                                f"429 stream: {status}", status=429,
                                retry_after=retry_after,
                            )
                            if attempt + 1 < self.MAX_ATTEMPTS:
                                continue
                            raise last_exc
                        if status >= 500:
                            state["retries"] += 1
                            last_exc = UpstreamError(status, f"5xx stream: {status}")
                            if attempt + 1 < self.MAX_ATTEMPTS:
                                await self._sleep(self._backoff_delay(attempt))
                                continue
                            raise last_exc
                        if status != 200:
                            raise UpstreamError(status, f"stream status {status}")
                        # 200 OK — read SSE chunks.
                        async for event in parse_sse_stream(resp.aiter_bytes()):
                            data_str = event.get("data", "")
                            if data_str == "[DONE]":
                                return
                            if not data_str:
                                continue
                            try:
                                d = json.loads(data_str)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            choices = d.get("choices") or []
                            delta: dict[str, Any] = {}
                            finish_reason: str | None = None
                            if choices and isinstance(choices[0], dict):
                                delta = choices[0].get("delta") or {}
                                finish_reason = choices[0].get("finish_reason")
                            content_delta = delta.get("content") or ""
                            fc = _extract_message_function_call(delta)
                            usage = d.get("usage")
                            yield StreamChunk(
                                content_delta=content_delta,
                                usage_or_none=usage,
                                function_call=fc,
                                finish_reason=finish_reason,
                            )
                        return  # connection closed cleanly (no [DONE])
                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    state["retries"] += 1
                    last_exc = e
                    if attempt + 1 < self.MAX_ATTEMPTS:
                        await self._sleep(self._backoff_delay(attempt))
                        continue
                    raise UpstreamError(None, f"stream network error after retries: {e}") from e
            raise last_exc if last_exc is not None else UpstreamError(None, "stream retries exhausted")

        acc: StreamAccumulator
        if function_name_hint:
            inner_gen = gen

            async def enforcing_gen() -> AsyncIterator[StreamChunk]:
                fc_seen: dict[str, Any] | None = None
                async for chunk in inner_gen():
                    if chunk.function_call:
                        fc_seen = chunk.function_call
                    yield chunk
                if fc_seen is None:
                    raise FunctionCallMissing(
                        f"expected function_call {function_name_hint!r}; model returned text"
                    )

            acc = StreamAccumulator(enforcing_gen, on_complete=on_complete, started_at=started)
        else:
            acc = StreamAccumulator(gen, on_complete=on_complete, started_at=started)

        return acc

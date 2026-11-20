"""The GigaClient facade: one object that owns auth, retry, rate limiting and metrics.

Design rule, enforced by construction: **no other layer of IGAFORGE talks to the
provider directly.** ``l0`` embeds through this client, ``agents`` complete through
it, ``cli`` resolves it from settings. That is why every hardening concern lives
here rather than being repeated at each call site.

What this replaces, from the reference ``app.py``:

===============  ==========================================================
app.py           GigaClient
===============  ==========================================================
key in source    ``auth_key`` from env, validated at construction
``verify=False`` ``tls_verify`` config, default ``False`` for current GigaChat API compatibility
no retries       exponential backoff with jitter, ``Retry-After`` aware
no throttling    token buckets for RPM/TPM, reconciled against real usage
``0`` tokens     ``chars / 4`` fallback, flagged ``estimated=True``
first-line SSE   incremental parser handling comments, multi-line and EOF
``content`` only structured ``function_call`` + ``additional_kwargs`` + text recovery
global session   injected transport, sized pool, explicit ``close()``
===============  ==========================================================
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

from .auth import TokenProvider, get_token_provider
from .config import ClientConfig
from .errors import (
    AuthError,
    ConfigurationError,
    GigaForgeError,
    NetworkError,
    RequestError,
    StreamProtocolError,
    error_for_exception,
    error_for_status,
)
from .functions import (
    detect_text_function_call,
    function_names,
    normalize_functions,
    parse_function_call,
)
from .metrics import CompositeMetrics, InMemoryMetrics, MetricsSink, NullMetrics
from .ratelimit import RateLimiter, Reservation
from .retry import RetryPolicy, parse_retry_after, retry_call
from .sse import SSEParser
from .streaming import StreamAggregator, StreamStats
from .transport import RequestsTransport, Response, Transport
from .types import (
    ChatMessage,
    ChatResult,
    FunctionCall,
    ModelInfo,
    StreamDelta,
    Usage,
    coerce_messages,
    estimate_token_count,
    join_message_text,
)

# Default sampling / generation parameters - named, so they can be overridden per call.
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TOP_P = 0.9

# Message appended when the model answers a tool-shaped prompt with prose.
TOOL_NUDGE = (
    "Your previous answer did not call a function. Respond ONLY with a single "
    "JSON object of the form {\"name\": \"<function>\", \"arguments\": {...}} "
    "and nothing else."
)


@dataclass
class RequestPlan:
    """A fully-resolved request, ready to be sent (or retried) unchanged."""

    url: str
    payload: Dict[str, Any]
    model: str
    stream: bool
    prompt_text: str
    prompt_tokens: int
    function_names: List[str] = field(default_factory=list)
    rquid: str = ""


def _metrics_for(config: ClientConfig, metrics: Optional[MetricsSink]) -> MetricsSink:
    if metrics is not None:
        return metrics
    if not config.metrics_enabled:
        return NullMetrics()
    return InMemoryMetrics()


def _headers(token: str, config: ClientConfig, accept_stream: bool) -> Dict[str, str]:
    headers = {
        "Accept": "text/event-stream" if accept_stream else "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": config.user_agent,
    }
    return headers


class GigaClient:
    """Hardened synchronous client for the GigaChat chat-completions API."""

    def __init__(
        self,
        config: Optional[ClientConfig] = None,
        *,
        transport: Optional[Transport] = None,
        token_provider: Optional[TokenProvider] = None,
        metrics: Optional[MetricsSink] = None,
        limiter: Optional[RateLimiter] = None,
        sleep=time.sleep,
        clock=time.perf_counter,
        validate: bool = True,
    ) -> None:
        self.config = (config or ClientConfig.from_env()).validate(require_auth=validate) \
            if validate else (config or ClientConfig.from_env())
        self.metrics = _metrics_for(self.config, metrics)
        self._clock = clock
        self._sleep = sleep
        self._closed = False

        self._transport = transport or RequestsTransport(self.config)
        self.token_provider = token_provider or get_token_provider(
            self.config, transport=self._transport, metrics=self.metrics
        )
        self.retry_policy = RetryPolicy.from_config(self.config)
        burst = float(self.config.rate_burst)
        self.limiter = limiter or RateLimiter(
            rpm=int(self.config.rpm),
            tpm=int(self.config.tpm),
            burst=burst if burst > 0 else 1.0,
            clock=time.monotonic,
            sleep=self._sleep,
        )
        self._last_result: Optional[ChatResult] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport.close()

    def __enter__(self) -> "GigaClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def last_result(self) -> Optional[ChatResult]:
        return self._last_result

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        """Local health only - never makes a network call."""
        snapshot = self.metrics.snapshot()
        return {
            "ok": not self._closed,
            "config": self.config.redacted(),
            "token": self.token_provider.health(),
            "rate_limit": self.limiter.snapshot(),
            "counters": snapshot.get("counters", {}),
            "observations": snapshot.get("observations", {}),
        }

    def estimate_tokens(self, text: str) -> int:
        """Expose the same ``chars / 4`` heuristic the aggregator uses."""
        return estimate_token_count(text, self.config.estimate_chars_per_token)

    # ------------------------------------------------------------------ #
    # Chat: non-streaming
    # ------------------------------------------------------------------ #
    def complete(
        self,
        messages: Sequence[Any],
        *,
        model: Optional[str] = None,
        functions: Optional[Iterable[Any]] = None,
        function_call: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        extra: Optional[Mapping[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> ChatResult:
        """Run a completion and return the aggregated result.

        When ``functions`` are supplied the method also fights the model's habit of
        answering with prose: if no structured call came back but one is recoverable
        from the text, the request is re-sent once with a corrective instruction.
        """
        plan = self._build_plan(
            messages,
            model=model,
            functions=functions,
            function_call=function_call,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            extra=extra,
            stream=False,
            request_id=request_id,
        )
        result = self._complete_once(plan)

        if plan.function_names and not result.has_function_call and self.config.retry_on_text_call:
            recovered = detect_text_function_call(result.content, plan.function_names)
            attempts = max(0, int(self.config.max_text_call_retries))
            for _ in range(attempts):
                plan = self._with_nudge(plan)
                retry_result = self._complete_once(plan)
                if retry_result.has_function_call:
                    retry_result.attempts = result.attempts + retry_result.attempts
                    retry_result.metrics["text_call_recovered"] = False
                    result = retry_result
                    recovered = None
                    break
                recovered = detect_text_function_call(retry_result.content, plan.function_names) or recovered
                result = retry_result
            if recovered is not None and not result.has_function_call:
                result.function_call = recovered
                result.recovered_text_call = True
                result.metrics["text_call_recovered"] = True
                self.metrics.incr("client.func.text_call_recovered", 1, {"model": plan.model})

        self._last_result = result
        return result

    def _complete_once(self, plan: RequestPlan) -> ChatResult:
        started = self._clock()
        response, reservation, attempts = self._send_with_retry(plan, stream=False)
        elapsed_ms = (self._clock() - started) * 1000.0

        payload = response.json()
        if not isinstance(payload, Mapping):
            raise StreamProtocolError(
                f"chat response was not a JSON object: {response.text()[:200]}"
            )

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise StreamProtocolError(
                "chat response contained no choices", payload=self._slim(payload)
            )

        choice = choices[0] if isinstance(choices[0], Mapping) else {}
        message = choice.get("message")
        if not isinstance(message, Mapping):
            message = {}

        content = message.get("content")
        content = content if isinstance(content, str) else ""

        usage = Usage.from_api(payload.get("usage"), model=plan.model)
        if usage is None:
            usage = Usage.estimate(
                plan.prompt_text,
                content,
                ratio=self.config.estimate_chars_per_token,
                model=plan.model,
            )
            usage.prompt_tokens = plan.prompt_tokens or usage.prompt_tokens
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
            self.metrics.incr("client.usage.estimated", 1, {"model": plan.model})

        result = ChatResult(
            content=content,
            function_call=parse_function_call(message),
            usage=usage,
            finish_reason=choice.get("finish_reason"),
            model=str(payload.get("model") or plan.model),
            streamed=False,
            latency_ms=elapsed_ms,
            attempts=attempts,
            request_id=plan.rquid,
        )
        self._record_result(result, plan, reservation, kind="complete")
        return result

    # ------------------------------------------------------------------ #
    # Chat: streaming
    # ------------------------------------------------------------------ #
    def stream_deltas(
        self,
        messages: Sequence[Any],
        *,
        model: Optional[str] = None,
        functions: Optional[Iterable[Any]] = None,
        function_call: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        extra: Optional[Mapping[str, Any]] = None,
        request_id: Optional[str] = None,
        on_stats: Optional[Any] = None,
    ) -> Iterator[StreamDelta]:
        """Yield deltas as they arrive. Also records :attr:`last_result` at the end.

        Retries happen only for failures *before the first frame*: once tokens have
        been streamed to a caller, silently restarting would duplicate output.
        """
        plan = self._build_plan(
            messages,
            model=model,
            functions=functions,
            function_call=function_call,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            extra=extra,
            stream=True,
            request_id=request_id,
        )
        started = self._clock()
        response, reservation, attempts = self._send_stream_with_retry(plan)

        parser = SSEParser()
        aggregator = StreamAggregator(
            model=plan.model,
            estimate_ratio=self.config.estimate_chars_per_token,
            prompt_text=plan.prompt_text,
            prompt_tokens=plan.prompt_tokens,
            request_id=plan.rquid,
        )

        try:
            for event in parser.feed_lines(response.iter_lines()):
                delta = aggregator.accept(event)
                if delta is not None:
                    yield delta
            # EOF: the stream may simply stop, with or without [DONE].
            for event in parser.flush():
                delta = aggregator.accept(event)
                if delta is not None:
                    yield delta
        finally:
            response.close()
            elapsed_ms = (self._clock() - started) * 1000.0
            result = aggregator.finalize(latency_ms=elapsed_ms, attempts=attempts)
            if not aggregator.stats.saw_done:
                self.metrics.incr("client.stream.no_done", 1, {"model": plan.model})
            if aggregator.stats.empty_content_frames:
                self.metrics.incr(
                    "client.stream.empty_delta",
                    aggregator.stats.empty_content_frames,
                    {"model": plan.model},
                )
            self._record_result(result, plan, reservation, kind="stream")
            # Publish the aggregate even when the caller abandons the generator.
            self._last_result = result
            if on_stats is not None:
                try:
                    on_stats(aggregator.stats)
                except Exception:  # pragma: no cover - observer must not break the stream
                    pass

    def stream(
        self,
        messages: Sequence[Any],
        model: Optional[str] = None,
        functions: Optional[Iterable[Any]] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Consume the whole stream and return the aggregated result."""
        for _ in self.stream_deltas(messages, model=model, functions=functions, **kwargs):
            pass
        if self._last_result is None:  # pragma: no cover - defensive
            raise StreamProtocolError("stream produced no result")
        return self._last_result

    def iter_content(
        self, messages: Sequence[Any], *, model: Optional[str] = None, **kwargs: Any
    ) -> Iterator[str]:
        """Convenience: yield only the text fragments, skipping empty frames."""
        for delta in self.stream_deltas(messages, model=model, **kwargs):
            if delta.content:
                yield delta.content

    def generate(
        self, messages: Sequence[Any], *, model: Optional[str] = None, **kwargs: Any
    ) -> str:
        """Convenience: stream and return the final text."""
        return self.stream(messages, model=model, **kwargs).content

    # ------------------------------------------------------------------ #
    # Models
    # ------------------------------------------------------------------ #
    def list_models(self) -> List[ModelInfo]:
        """Fetch the model catalogue (used by ``gigaforge doctor``)."""
        plan = RequestPlan(
            url=self.config.models_url,
            payload={},
            model=self.config.default_model,
            stream=False,
            prompt_text="",
            prompt_tokens=0,
            rquid=str(uuid.uuid4()),
        )
        started = self._clock()
        response, _, _ = self._send_with_retry(plan, stream=False, method="GET")
        self.metrics.observe("client.models.latency_ms", (self._clock() - started) * 1000.0)
        payload = response.json()
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            data = []
        models: List[ModelInfo] = []
        for entry in data:
            if isinstance(entry, Mapping) and entry.get("id"):
                models.append(
                    ModelInfo(
                        id=str(entry["id"]),
                        created=entry.get("created"),
                        owned_by=str(entry.get("owned_by") or "GigaChat"),
                    )
                )
        return models

    # ------------------------------------------------------------------ #
    # Request construction
    # ------------------------------------------------------------------ #
    def _build_plan(
        self,
        messages: Sequence[Any],
        *,
        model: Optional[str],
        functions: Optional[Iterable[Any]],
        function_call: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
        top_p: Optional[float],
        extra: Optional[Mapping[str, Any]],
        stream: bool,
        request_id: Optional[str],
    ) -> RequestPlan:
        if self._closed:
            raise ConfigurationError("client is closed")
        resolved_model = (model or self.config.default_model or "").strip()
        if not resolved_model:
            raise ConfigurationError("a model is required (set default_model or pass model=)")

        coerced = coerce_messages(messages)
        payload: Dict[str, Any] = {
            "model": resolved_model,
            "messages": [m.to_payload() for m in coerced],
            "temperature": DEFAULT_TEMPERATURE if temperature is None else float(temperature),
            "max_tokens": DEFAULT_MAX_TOKENS if max_tokens is None else int(max_tokens),
        }
        if top_p is not None:
            payload["top_p"] = float(top_p)
        elif temperature is None:
            payload["top_p"] = DEFAULT_TOP_P
        if stream:
            payload["stream"] = True

        flat = normalize_functions(functions)
        if flat:
            # Flat declarations, GigaChat style - never the OpenAI envelope.
            payload["functions"] = flat
            if function_call:
                payload["function_call"] = function_call

        if extra:
            for key, value in extra.items():
                if value is not None:
                    payload[key] = value

        prompt_text = join_message_text(coerced)
        return RequestPlan(
            url=self.config.chat_url,
            payload=payload,
            model=resolved_model,
            stream=stream,
            prompt_text=prompt_text,
            prompt_tokens=estimate_token_count(prompt_text, self.config.estimate_chars_per_token),
            function_names=function_names(functions),
            rquid=request_id or str(uuid.uuid4()),
        )

    def _with_nudge(self, plan: RequestPlan) -> RequestPlan:
        """Return a copy of the plan with a corrective instruction appended."""
        messages = list(plan.payload.get("messages") or [])
        # GigaChat rejects a system message that is not first in the
        # conversation (hard 400), so a corrective instruction appended to an
        # existing history must travel as a HUMAN message. The model still
        # follows it; the request shape stays legal.
        messages.append({"role": "user", "content": TOOL_NUDGE})
        payload = dict(plan.payload)
        payload["messages"] = messages
        return RequestPlan(
            url=plan.url,
            payload=payload,
            model=plan.model,
            stream=False,
            prompt_text=plan.prompt_text + "\n" + TOOL_NUDGE,
            prompt_tokens=plan.prompt_tokens + estimate_token_count(
                TOOL_NUDGE, self.config.estimate_chars_per_token
            ),
            function_names=plan.function_names,
            rquid=str(uuid.uuid4()),
        )

    # ------------------------------------------------------------------ #
    # Transport, auth, retry, rate limiting
    # ------------------------------------------------------------------ #
    def _send_with_retry(
        self, plan: RequestPlan, *, stream: bool, method: str = "POST"
    ) -> tuple:
        """Send a request, handling auth, retries and 429s. Returns response + ticket."""
        attempt = 0
        auth_retried = False
        while True:
            attempt += 1
            reservation = self.limiter.reserve(self._reserve_tokens(plan, stream))
            token = self.token_provider.get_token()
            if method == "GET":
                self.metrics.incr("client.request", 1, {"model": plan.model, "kind": "models"})
            else:
                self.metrics.incr("client.request", 1, {"model": plan.model, "stream": stream})
            try:
                response = self._transport.request(
                    method,
                    plan.url,
                    headers={**_headers(token, self.config, stream), "X-Request-ID": plan.rquid},
                    json_body=None if method == "GET" else plan.payload,
                    timeout=self.config.stream_timeouts if stream else self.config.timeouts,
                    stream=stream,
                )
            except Exception as exc:  # noqa: BLE001 - classified into the GF-* taxonomy
                error = exc if isinstance(exc, GigaForgeError) else error_for_exception(exc)
                self.metrics.incr("client.error." + error.code, 1, {"model": plan.model})
                if attempt > self.retry_policy.max_retries or not error.retryable:
                    raise error
                self._sleep(self.retry_policy.compute_delay(attempt, error.retry_after))
                continue

            status = response.status
            if status == 200:
                return response, reservation, attempt

            if status == 401:
                response.close()
                self.metrics.incr("client.error.GF-AUTH-401", 1, {"model": plan.model})
                # Only the thread holding the failing token may drop it: a late 401
                # must not throw away a token another thread already refreshed.
                replaced = self.token_provider.invalidate_if(token)
                if replaced and not auth_retried and attempt <= self.retry_policy.max_retries:
                    auth_retried = True
                    continue
                fresh = self.token_provider.get_token()
                if not replaced and fresh != token and attempt <= self.retry_policy.max_retries:
                    auth_retried = True
                    continue
                raise AuthError(
                    f"unauthorized after re-authentication: {response.text()[:300]}",
                    status=401,
                    payload=self._slim(_safe_json(response)),
                )

            retry_after = parse_retry_after(response.header("Retry-After"))
            error = error_for_status(
                status,
                f"chat/completions failed: {status}",
                payload=self._slim(_safe_json(response)),
                retry_after=retry_after,
            )
            response.close()
            self.metrics.incr("client.error." + error.code, 1, {"model": plan.model})

            if status == 429:
                # Block *all* callers, not just this one - the quota is process-wide.
                applied = self.limiter.penalize(retry_after or self.config.backoff_base)
                self.metrics.incr("client.ratelimit.429", 1, {"model": plan.model})
                self.metrics.observe("client.ratelimit.429_wait_s", applied)

            if attempt > self.retry_policy.max_retries or not error.retryable:
                raise error
            self._sleep(self.retry_policy.compute_delay(attempt, error.retry_after))

    def _send_stream_with_retry(self, plan: RequestPlan) -> tuple:
        """Open a stream, but only retry while *no deltas* have been consumed."""
        return self._send_with_retry(plan, stream=True)

    def _reserve_tokens(self, plan: RequestPlan, stream: bool) -> int:
        """Reserve tokens before the call so the TPM bucket stays honest.

        The prompt is known exactly; for the completion we reserve half of the
        remaining budget. The difference is reconciled from real ``usage`` afterwards.
        """
        budget = int(plan.payload.get("max_tokens") or DEFAULT_MAX_TOKENS)
        return int(plan.prompt_tokens + budget * 0.5)

    # ------------------------------------------------------------------ #
    # Accounting
    # ------------------------------------------------------------------ #
    def _record_result(
        self,
        result: ChatResult,
        plan: RequestPlan,
        reservation: Reservation,
        *,
        kind: str,
    ) -> None:
        usage = result.usage
        self.limiter.reconcile(reservation, usage.total_tokens)
        labels = {"model": plan.model, "source": usage.source}
        self.metrics.incr("client.usage.total_tokens", usage.total_tokens, labels)
        if usage.prompt_tokens:
            self.metrics.incr("client.usage.prompt_tokens", usage.prompt_tokens, {"model": plan.model})
        if usage.completion_tokens:
            self.metrics.incr(
                "client.usage.completion_tokens", usage.completion_tokens, {"model": plan.model}
            )
        self.metrics.observe("client.latency_ms", result.latency_ms, {"model": plan.model, "kind": kind})
        self.metrics.incr("client.response", 1, {"model": plan.model, "kind": kind, "finish": result.finish_reason or "none"})
        if kind == "stream":
            self.metrics.incr("client.response.stream_chars", len(result.content), {"model": plan.model})

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _slim(payload: Any, limit: int = 600) -> Any:
        """Truncate a payload before it lands in a log line or an exception."""
        if payload is None:
            return None
        text = payload if isinstance(payload, str) else str(payload)
        return text[:limit]



def _safe_json(response: Response) -> Any:
    try:
        return response.json()
    except Exception:  # pragma: no cover - defensive
        return response.text()[:400]


__all__ = ["GigaClient", "RequestPlan", "DEFAULT_MAX_TOKENS", "DEFAULT_TEMPERATURE"]

"""RetryingProvider — the single retry owner for transports that have none.

The merge plan's §7.2 rule is "turn retries off in one layer, not two".  The
official ``gigachat`` SDK retries internally, and GigaForge has its own
:class:`~gigaforge.client.retry.RetryPolicy`; running both turns one 429 into
N×M attempts.  The adapter therefore builds the SDK client with
``max_retries=0`` — but that left the SDK path with **no** retry layer at all,
because the only retry loop in the project lived inside the legacy
``GigaClient``, which the SDK path does not use.

This module closes that hole.  It is a transparent :class:`Provider` decorator
that applies GigaForge's own policy around ``chat``:

* the policy is the project's :class:`RetryPolicy` (exponential backoff with
  jitter, ``Retry-After`` honoured as both floor and ceiling, hard cap);
* retryability comes from the shared error taxonomy
  (:func:`gigaforge.client.errors.error_for_exception`), which now also maps the
  official SDK's exception classes;
* the **original** exception is re-raised once retries are exhausted, so type
  information survives — :meth:`ContextWindowGuard.translate` still recognises a
  provider overflow by class name.

Providers that already own a retry loop (the legacy ``GigaChatProvider`` over
``GigaClient``) declare ``owns_retries = True`` and are never wrapped, so the
"one owner" rule holds in both directions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from gigaforge.client.errors import error_for_exception
from gigaforge.client.retry import RetryPolicy

__all__ = ["RetryingProvider"]

log = logging.getLogger(__name__)


class RetryingProvider:
    """Wrap a :class:`~gigaforge.providers.base.Provider` with GigaForge retries.

    ``settings`` is any object exposing the retry knobs (``max_retries``,
    ``backoff_base``, ``backoff_max``, ``backoff_jitter``, ``max_retry_after``);
    missing attributes fall back to :class:`RetryPolicy`'s own defaults, so a
    bare ``Settings`` works.
    """

    #: This wrapper IS the retry owner for the provider it wraps.
    owns_retries = True

    def __init__(
        self,
        inner: Any,
        *,
        settings: Any = None,
        policy: Optional[RetryPolicy] = None,
        sleep: Any = None,
    ) -> None:
        if inner is None:
            raise ValueError("RetryingProvider needs a provider to wrap")
        self._inner = inner
        if policy is not None:
            self._policy = policy
        elif settings is not None:
            self._policy = RetryPolicy.from_config(settings)
        else:
            self._policy = RetryPolicy()
        self._sleep = sleep or asyncio.sleep
        self.retries = 0

    # -- identity ------------------------------------------------------------
    @property
    def inner(self) -> Any:
        return self._inner

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

    def __getattr__(self, name: str) -> Any:
        """Delegate anything not defined here (``tokens_count``, ``embeddings``…)."""
        # ``__getattr__`` is only consulted for attributes not found normally,
        # so this cannot shadow the real methods above.
        return getattr(self._inner, name)

    # -- the retried call ----------------------------------------------------
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
    ) -> Any:
        """Call the wrapped provider, retrying retryable failures."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._inner.chat(
                    messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=stream,
                    functions=functions,
                    function_name_hint=function_name_hint,
                    task_id=task_id,
                    phase=phase,
                    response_format=response_format,
                )
            except BaseException as exc:  # noqa: BLE001 - classified below
                error = error_for_exception(exc)
                if attempt > self._policy.max_retries or not error.retryable:
                    # Re-raise the ORIGINAL exception: the context guard and the
                    # caller's own handlers key off the provider's class name.
                    raise
                delay = self._policy.compute_delay(attempt, error.retry_after)
                self.retries += 1
                log.warning(
                    "provider call failed (%s, attempt %d/%d); retrying in %.2fs",
                    error.code,
                    attempt,
                    self._policy.max_retries,
                    delay,
                )
                await self._sleep(delay)

    async def list_models(self) -> list[dict[str, Any]]:
        return await self._inner.list_models()

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
    ) -> Any:
        """Streaming is not retried: a partially consumed stream cannot restart."""
        return self._inner.stream_chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            functions=functions,
            function_name_hint=function_name_hint,
            task_id=task_id,
            phase=phase,
            response_format=response_format,
        )

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if callable(closer):
            await closer()

    def capabilities(self) -> Any:
        return self._inner.capabilities()

    def describe(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "inner": type(self._inner).__name__,
            "max_retries": self._policy.max_retries,
            "retries_used": self.retries,
        }

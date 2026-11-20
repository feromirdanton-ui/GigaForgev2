"""Blocking facade over an async chat provider — the one place loops live.

The orchestration layer is *synchronous*: DAG node functions are plain
callables, the fix loop is a plain ``while``, the CLI and the desktop shell call
``pipeline.run()`` and read the result.  Every transport underneath
(:class:`~gigaforge.client.giga_client.GigaClient`,
:class:`~gigaforge.providers.openai_compat.OpenAICompatProvider`) is *async*.

``SyncChatClient`` is the single bridge between the two.  It is constructed once
in the composition root (:mod:`gigaforge.runtime`) and injected; nothing below
it knows that an event loop exists.

Why a dedicated loop thread instead of ``asyncio.run`` per call
----------------------------------------------------------------
``asyncio.run`` builds and tears down a loop — and with it the HTTP connection
pool — on every call.  A task run makes dozens of calls, so that would throw
away every keep-alive connection and re-do the TLS handshake each time.  A
single long-lived loop in a daemon thread also makes the adapter safe to call
from *any* thread, including one that already has a running loop (``Tkinter``'s
main thread when it hosts an async plugin, or a caller doing
``asyncio.run(pipeline_task())``): a thread may always block on a future owned by
another thread's loop, whereas ``run_until_complete`` on a busy loop may not.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Dict, List, Optional, Sequence, TypeVar

__all__ = ["SyncChatClient", "await_blocking"]

log = logging.getLogger("gigaforge.core.sync_adapter")

T = TypeVar("T")

_SENTINEL_TIMEOUT = 600.0


class _LoopThread:
    """A daemon thread that owns one asyncio loop for the adapter's lifetime."""

    __slots__ = ("_loop", "_thread", "_ready", "_closed", "_lock")

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._closed = False
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                raise RuntimeError("the sync adapter's event loop has already been closed")
            if self._loop is None:
                self._thread = threading.Thread(
                    target=self._run, name="gigaforge-asyncio", daemon=True
                )
                self._thread.start()
                if not self._ready.wait(timeout=10.0):  # pragma: no cover - defensive
                    raise RuntimeError("timed out starting the async loop thread")
            assert self._loop is not None
            return self._loop

    def _run(self) -> None:  # pragma: no cover - trivial thread body
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
            loop.close()

    def submit(self, coro: Awaitable[T], *, timeout: float = _SENTINEL_TIMEOUT) -> T:
        loop = self.loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]
        return future.result(timeout=timeout)

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)


_shared = _LoopThread()
_shared_lock = threading.Lock()


def _shared_runner() -> _LoopThread:
    return _shared


def await_blocking(coro: Awaitable[T], *, timeout: float = _SENTINEL_TIMEOUT) -> T:
    """Run *coro* to completion on the process-wide adapter loop and return it.

    Safe from a thread with or without a running loop.  Used by the adapter and
    by the handful of call sites (provider smoke checks, model listing) that
    need one blocking async call without owning a client.
    """
    return _shared_runner().submit(coro, timeout=timeout)


class SyncChatClient:
    """Blocking ``complete(messages, ...)`` over any :class:`Provider`.

    The call contract is the one :class:`~gigaforge.core.router.ModelRouter`
    expects::

        result = client.complete(messages, model="GigaChat-2-Pro",
                                 temperature=0.2, max_tokens=1000)
        result.content   # str
        result.usage     # dict[str, int] | None

    ``provider`` may be a :class:`~gigaforge.providers.base.Provider` (the
    normal case), or a raw ``GigaClient`` — it is wrapped into
    :class:`~gigaforge.providers.gigachat.GigaChatProvider` on the way in so the
    argument order (``messages, *, model``) is normalised exactly once.
    """

    def __init__(self, provider: Any, *, timeout: float = _SENTINEL_TIMEOUT) -> None:
        self._provider = _coerce_provider(provider)
        self._timeout = float(timeout)
        self._closed = False
        self._calls = 0

    # -- identity ----------------------------------------------------------
    @property
    def provider(self) -> Any:
        return self._provider

    @property
    def calls(self) -> int:
        return self._calls

    def capabilities(self) -> Any:
        getter = getattr(self._provider, "capabilities", None)
        return getter() if callable(getter) else None

    def describe(self) -> Dict[str, Any]:
        caps = self.capabilities()
        return {
            "type": type(self._provider).__name__,
            "calls": self._calls,
            "capabilities": caps.model_dump() if hasattr(caps, "model_dump") else None,
        }

    # -- the router-facing call -------------------------------------------
    def complete(
        self,
        messages: Sequence[Any],
        *,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """Blocking chat completion. Mirrors ``Provider.chat`` minus the await."""
        if self._closed:
            raise RuntimeError("this SyncChatClient has been closed")
        payload: List[Dict[str, Any]] = [_as_message(m) for m in messages]
        self._calls += 1
        return await_blocking(
            self._provider.chat(
                payload,
                model=model or "",
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            ),
            timeout=self._timeout,
        )

    def list_models(self) -> List[Dict[str, Any]]:
        return await_blocking(self._provider.list_models(), timeout=self._timeout)

    # -- closing -----------------------------------------------------------
    def aclose(self) -> None:
        """Close the underlying provider, then retire the shared loop."""
        if self._closed:
            return
        self._closed = True
        closer = getattr(self._provider, "aclose", None)
        if callable(closer):
            try:
                await_blocking(closer(), timeout=15.0)
            except Exception:  # noqa: BLE001 - closing must never raise
                log.debug("provider aclose() failed", exc_info=True)

    close = aclose

    def __enter__(self) -> "SyncChatClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.aclose()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"SyncChatClient({type(self._provider).__name__}, calls={self._calls})"


# -- helpers ---------------------------------------------------------------
def _coerce_provider(provider: Any) -> Any:
    """Normalise a raw ``GigaClient`` (or a callable returning one) to a Provider."""
    if provider is None:
        raise ValueError("SyncChatClient needs a provider, got None")
    if isinstance(provider, type):  # a factory was passed by mistake
        provider = provider()
    if callable(provider) and not hasattr(provider, "chat"):
        provider = provider()
    if hasattr(provider, "chat"):
        return provider
    raise TypeError(
        "provider must expose an async chat(messages, *, model=...) coroutine; "
        f"got {type(provider).__name__}"
    )


def _as_message(message: Any) -> Dict[str, Any]:
    """Coerce one message to the ``{"role", "content"}`` dict the API expects."""
    if isinstance(message, dict):
        return dict(message)
    role = getattr(message, "role", None)
    content = getattr(message, "content", None)
    if role is not None:
        return {"role": str(role), "content": "" if content is None else str(content)}
    return {"role": "user", "content": str(message)}


def shutdown_shared_loop() -> None:
    """Stop the process-wide adapter loop (used by tests and app shutdown)."""
    _shared.stop()

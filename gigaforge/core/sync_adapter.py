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
        # Re-entrancy: a coroutine already running on THIS loop must not block
        # on a future owned by the same loop — that is a guaranteed deadlock
        # (the loop thread waits for itself).  This happens for real: the agent
        # layer is async (``ReActRunner.run``) but its client is this blocking
        # adapter, so ``run_react`` submits the runner to the adapter loop and
        # the runner's first ``client.chat()`` submits again from inside it.
        # Run the nested coroutine on a private loop in a throwaway thread.
        if threading.current_thread() is self._thread:
            return _run_on_private_loop(coro, timeout=timeout)
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


def _run_on_private_loop(coro: Awaitable[T], *, timeout: float = _SENTINEL_TIMEOUT) -> T:
    """Run *coro* on a fresh loop in a throwaway thread and return its result.

    Used only for the re-entrant case (see :meth:`_LoopThread.submit`): the
    caller is already inside the adapter loop, so it cannot block on that loop.
    A private loop costs one thread per nested call, which is acceptable because
    nesting is one level deep (runner → client) and the call is I/O bound.
    """
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    thread = threading.Thread(target=_target, name="gigaforge-nested-async", daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"nested async call did not finish within {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[return-value]


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
        #: Accumulated usage across every call.  The bench reads these to report
        #: tokens for a live run: the real client accounts per call and exposes
        #: no running total, so a live bench reported ``0 / 0`` tokens for work
        #: that had really been paid for.
        self._usage_in = 0
        self._usage_out = 0

    # -- identity ----------------------------------------------------------
    @property
    def provider(self) -> Any:
        return self._provider

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def usage_in(self) -> int:
        return self._usage_in

    @property
    def usage_out(self) -> int:
        return self._usage_out

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
        response_format: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """Blocking chat completion. Mirrors ``Provider.chat`` minus the await."""
        if self._closed:
            raise RuntimeError("this SyncChatClient has been closed")
        payload: List[Dict[str, Any]] = [_as_message(m) for m in messages]
        self._calls += 1
        if response_format is not None:
            kwargs["response_format"] = response_format
        result = await_blocking(
            self._provider.chat(
                payload,
                model=model or "",
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            ),
            timeout=self._timeout,
        )
        usage = getattr(result, "usage", None)
        # ``usage`` is a ``Usage`` pydantic model on the real client and a plain
        # dict on the fake.  Checking only for ``dict`` left the live bench
        # reporting 0/0 tokens for work that had really been paid for.
        if usage is not None:
            prompt = getattr(usage, "prompt_tokens", None)
            completion = getattr(usage, "completion_tokens", None)
            if prompt is None and isinstance(usage, dict):
                prompt = usage.get("prompt_tokens")
                completion = usage.get("completion_tokens")
            self._usage_in += int(prompt or 0)
            self._usage_out += int(completion or 0)
        return result

    async def chat(
        self,
        model: str,
        messages: Sequence[Any],
        *,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        functions: Optional[List[Dict[str, Any]]] = None,
        function_name_hint: Optional[str] = None,
        task_id: str = "",
        phase: str = "unknown",
        response_format: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """``GigaClient.chat``-shaped entry point over the provider.

        The agent layer (``ReActRunner``, ``PlannerAgent``, ``ReviewerAgent``)
        was written against ``GigaClient``, whose signature is
        ``chat(model, messages, ...)`` and which is *async*.  The pipeline's
        client is this adapter, whose router-facing call is the blocking
        ``complete(messages, model=...)``.  Without this method the whole agent
        layer raised ``AttributeError`` the first time it touched the pipeline's
        client — the defect that made the ReAct layer unreachable even after the
        import was fixed.

        It is deliberately ``async`` and awaits the provider directly: every
        caller in ``gigaforge.agents`` does ``await client.chat(...)``, and a
        blocking implementation would deadlock the adapter loop it is called
        from.  The result is the provider's own ``ChatResult`` (``.content``,
        ``.usage``, ``.estimated``), which is exactly what the agents read.
        """
        if self._closed:
            raise RuntimeError("this SyncChatClient has been closed")
        payload: List[Dict[str, Any]] = [_as_message(m) for m in messages]
        self._calls += 1
        if response_format is not None:
            kwargs["response_format"] = response_format
        if functions is not None:
            kwargs["functions"] = functions
        if function_name_hint is not None:
            kwargs["function_name_hint"] = function_name_hint
        if task_id:
            kwargs["task_id"] = task_id
        if phase:
            kwargs["phase"] = phase
        return await self._provider.chat(
            payload,
            model=model or "",
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    def list_models(self) -> List[Dict[str, Any]]:
        return await_blocking(self._provider.list_models(), timeout=self._timeout)

    def count_tokens(
        self,
        messages: Sequence[Any],
        *,
        model: str = "",
    ) -> Optional[int]:
        """Server-side token count, or ``None`` when the transport cannot count.

        The context guard calls this to replace its char/4 estimate with the
        real number.  A provider without ``count_tokens`` (the legacy client)
        returns ``None`` and the guard keeps its estimate.
        """
        counter = getattr(self._provider, "count_tokens", None)
        if not callable(counter):
            return None
        payload: List[Dict[str, Any]] = [_as_message(m) for m in messages]
        try:
            return await_blocking(
                counter(payload, model=model), timeout=self._timeout
            )
        except Exception:  # noqa: BLE001 - counting is an optimisation
            return None

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
    """Normalise a raw ``GigaClient`` (or a callable returning one) to a Provider.

    **The defect this fixes.**  The class docstring promises that a raw
    ``GigaClient`` is "wrapped into GigaChatProvider on the way in", and it was
    not: the function checked only for ``chat`` and raised otherwise.  Measured
    live: ``bench --live`` failed all five tasks with "provider must expose an
    async chat(messages, *, model=...) coroutine; got GigaClient" — the one code
    path that talks to the real provider was the one that could not be given it.
    The offline bench passed because its fake client happens to expose ``chat``,
    so the difference between the two providers was invisible to every test.

    The synchronous client is accepted as well as the async one, because both
    exist in this codebase and a caller has no reason to know which shape the
    adapter wants.
    """
    if provider is None:
        raise ValueError("SyncChatClient needs a provider, got None")
    if isinstance(provider, type):  # a factory was passed by mistake
        provider = provider()
    if callable(provider) and not (
        hasattr(provider, "chat") or hasattr(provider, "complete")
    ):
        provider = provider()
    if hasattr(provider, "chat"):
        return provider
    # A synchronous client (``complete(messages, model=...)``) is a legitimate
    # provider: adapt it rather than refusing it.
    #
    # The shim is used directly, not ``GigaChatProvider``: the provider wrapper
    # assumes the client also exposes an async ``chat`` and calls it, so wrapping
    # a client that has only ``complete`` moved the failure one layer down
    # (measured: ``AttributeError: '_SyncClient' object has no attribute
    # 'chat'``).  The adapter needs one method and the shim supplies exactly that
    # one, from the method the client really has.
    if hasattr(provider, "complete"):
        return _SyncProviderShim(provider)
    raise TypeError(
        "provider must expose an async chat(messages, *, model=...) coroutine, "
        f"or a synchronous complete(...); got {type(provider).__name__}"
    )


class _SyncProviderShim:
    """A blocking ``complete``-only client presented with the provider protocol.

    Used when :class:`GigaChatProvider` cannot be constructed around the client
    (a client that is not a GigaChat client at all).  The adapter's only need is
    ``chat``; this forwards to ``complete`` and returns the same value, so the
    adapter's own normalisation still applies.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        # ``task_id`` and ``phase`` are the adapter's own bookkeeping — the
        # router uses them to attribute spend — and ``GigaClient.complete``
        # does not accept them.  Forwarding them raised
        # ``TypeError: GigaClient.complete() got an unexpected keyword argument
        # 'task_id'`` on the very first model call of every run, and the
        # exception was swallowed into "the model produced no plan".  Measured
        # on SWE-bench: every instance reported a planning defect while the
        # provider had never been reached.
        kwargs.pop("task_id", None)
        kwargs.pop("phase", None)
        return self._client.complete(messages, **kwargs)

    def capabilities(self) -> Any:
        getter = getattr(self._client, "capabilities", None)
        return getter() if callable(getter) else None


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

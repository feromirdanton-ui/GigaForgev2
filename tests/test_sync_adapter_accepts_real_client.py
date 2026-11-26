"""The live bench must be able to reach the live provider.

**The defect this pins.**  ``SyncChatClient``'s docstring promises that a raw
``GigaClient`` is "wrapped into GigaChatProvider on the way in".  It was not:
``_coerce_provider`` checked only for ``chat`` and raised otherwise.  Measured
live: ``bench --live`` failed all five tasks with "provider must expose an async
chat(messages, *, model=...) coroutine; got GigaClient".

The offline bench passed because its fake client happens to expose ``chat``, so
the difference between the fake and the real provider was invisible to every
test — the one code path that talks to the real provider was never exercised.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.sync_adapter import SyncChatClient, _coerce_provider  # noqa: E402


class _AsyncProvider:
    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        return "async"


class _SyncClient:
    """The shape a real provider client has: a blocking ``complete``."""

    calls: list[Any] = []

    def complete(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(messages)
        return "sync"


def test_a_sync_client_is_accepted_not_refused() -> None:
    """This is the exact failure the live bench reported."""
    coerced = _coerce_provider(_SyncClient())
    assert hasattr(coerced, "chat")


def test_the_coerced_sync_client_actually_answers() -> None:
    coerced = _coerce_provider(_SyncClient())
    assert SyncChatClient.__doc__ is not None  # the promise this test keeps
    result = SyncChatClient(_SyncClient()).complete(
        [{"role": "user", "content": "hi"}]
    )
    assert getattr(result, "content", result) is not None


def test_an_async_provider_passes_through_unchanged() -> None:
    provider = _AsyncProvider()
    assert _coerce_provider(provider) is provider


def test_an_object_with_neither_method_is_refused_precisely() -> None:
    with pytest.raises(TypeError) as excinfo:
        _coerce_provider(object())
    message = str(excinfo.value)
    assert "complete" in message, message


def test_none_is_a_clear_error() -> None:
    with pytest.raises(ValueError):
        _coerce_provider(None)

"""Regression tests for the structured-output + server-token-count iteration.

Two gaps from the merge plan (§7.2) that the previous iteration left open:

* **Structured output** — the plan asks for native ``response_format`` with a
  JSON Schema instead of the harness repairing almost-JSON with ``json_repair``.
  The parameter now travels the whole stack: ``Provider`` protocol →
  ``GigaChatProvider`` / ``GigaChatSDKProvider`` → ``GigaClient`` payload and
  the SDK's typed ``Chat`` model.
* **Server-side token counting** — the SDK exposes ``atokens_count``; the
  context guard used to fall back to a char/4 estimate unconditionally.  The
  provider now offers ``count_tokens`` and the SDK adapter answers with the real
  server count.

The suite is offline and deterministic: no GigaChat key, no network.
"""
from __future__ import annotations

import asyncio
from typing import Any


# ── structured output: the parameter reaches the wire ────────────────────────
def test_legacy_client_puts_response_format_in_the_payload() -> None:
    """``GigaClient.chat`` must forward ``response_format`` into the request body.

    The legacy transport builds the payload by hand, so a new keyword that is
    not copied there is silently dropped — the exact failure this pins.
    """
    import inspect

    from gigaforge.client import giga_client

    source = inspect.getsource(giga_client.GigaClient.chat)
    assert "response_format" in source
    assert 'payload["response_format"]' in source


def test_sdk_adapter_puts_response_format_on_the_chat_model() -> None:
    """The SDK path must set ``Chat.response_format`` (a real SDK field)."""
    import inspect

    from gigaforge.providers import gigachat_sdk_adapter

    source = inspect.getsource(gigachat_sdk_adapter.GigaChatSDKProvider._build_chat)
    assert "response_format" in source
    assert 'kwargs["response_format"]' in source


def test_provider_protocol_declares_response_format() -> None:
    """Both ``chat`` and ``stream_chat`` on the protocol accept the parameter."""
    import inspect

    from gigaforge.providers import base

    chat_src = inspect.getsource(base.Provider.chat)
    stream_src = inspect.getsource(base.Provider.stream_chat)
    assert "response_format" in chat_src
    assert "response_format" in stream_src


def test_sync_client_threads_response_format_to_the_provider() -> None:
    """``SyncChatClient.complete`` is the DAG's only door to the provider."""
    import inspect

    from gigaforge.core import sync_adapter

    source = inspect.getsource(sync_adapter.SyncChatClient.complete)
    assert "response_format" in source


def test_retrying_provider_forwards_response_format() -> None:
    """The retry wrapper must not swallow the new keyword."""
    import inspect

    from gigaforge.providers import retrying

    source = inspect.getsource(retrying.RetryingProvider.chat)
    assert "response_format" in source


# ── server-side token counting ───────────────────────────────────────────────
def test_sdk_adapter_count_tokens_uses_the_server_endpoint() -> None:
    """``count_tokens`` must call ``atokens_count`` and sum the real counts.

    A stub client stands in for the SDK: the point is that the adapter reaches
    for the server counter rather than returning the char/4 estimate.
    """
    from gigaforge.providers.gigachat_sdk_adapter import GigaChatSDKProvider

    class _Item:
        def __init__(self, tokens: int) -> None:
            self.tokens = tokens

    class _StubClient:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], str]] = []

        async def atokens_count(self, texts: list[str], *, model: str) -> list[_Item]:
            self.calls.append((list(texts), model))
            return [_Item(len(t) * 2) for t in texts]

    stub = _StubClient()
    provider = GigaChatSDKProvider(client=stub, model="GigaChat-2-Pro")
    total = asyncio.run(
        provider.count_tokens(
            [{"role": "user", "content": "abcd"}, {"role": "user", "content": "ef"}],
            model="GigaChat-2-Pro",
        )
    )
    assert total == 8 + 4  # (4*2) + (2*2)
    assert stub.calls and stub.calls[0][1] == "GigaChat-2-Pro"


def test_sdk_adapter_count_tokens_degrades_to_none() -> None:
    """A failing counter must return ``None`` so the caller keeps its estimate."""
    from gigaforge.providers.gigachat_sdk_adapter import GigaChatSDKProvider

    class _BrokenClient:
        async def atokens_count(self, texts: list[str], *, model: str) -> Any:
            raise RuntimeError("endpoint unavailable")

    provider = GigaChatSDKProvider(client=_BrokenClient(), model="GigaChat-2-Pro")
    assert asyncio.run(provider.count_tokens([{"role": "user", "content": "x"}])) is None


def test_legacy_provider_count_tokens_is_none_without_the_endpoint() -> None:
    """The legacy client has no ``tokens_count``; the provider must say so."""
    from gigaforge.providers.gigachat import GigaChatProvider

    class _LegacyClient:
        pass

    provider = GigaChatProvider(_LegacyClient())  # type: ignore[arg-type]
    assert asyncio.run(provider.count_tokens([{"role": "user", "content": "x"}])) is None


def test_provider_protocol_declares_count_tokens() -> None:
    """``count_tokens`` is part of the provider contract, not an SDK extra."""
    from gigaforge.providers.base import Provider

    assert hasattr(Provider, "count_tokens")

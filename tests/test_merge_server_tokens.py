"""Regression tests for V7: the context guard must use the server token count.

The review found that ``ContextWindowGuard.estimate_message_tokens`` always
estimated ``len(text)/4`` even though the official SDK exposes
``atokens_count``.  The guard now accepts a ``token_counter`` callable and
prefers its answer, falling back to the estimate when the transport cannot
count (the legacy client) or the counter fails.

Offline and deterministic: no GigaChat key, no network.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def _messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "s" * 40},
        {"role": "user", "content": "u" * 40},
    ]


def test_guard_prefers_the_server_count() -> None:
    """A working counter wins over the char/4 estimate."""
    from gigaforge.core.context_window_guard import ContextWindowGuard

    seen: list[Any] = []

    def _counter(messages: Sequence[Any], model: Any) -> int | None:
        seen.append(model)
        return 1234

    guard = ContextWindowGuard(token_counter=_counter)
    assert guard.count_tokens(_messages(), "GigaChat-2-Pro") == 1234
    assert seen == ["GigaChat-2-Pro"]


def test_guard_falls_back_when_the_counter_returns_none() -> None:
    """The legacy transport cannot count; the estimate must still be produced."""
    from gigaforge.core.context_window_guard import (
        ContextWindowGuard,
        estimate_message_tokens,
    )

    guard = ContextWindowGuard(token_counter=lambda m, model: None)
    expected = estimate_message_tokens(_messages())
    assert guard.count_tokens(_messages()) == expected


def test_guard_falls_back_when_the_counter_raises() -> None:
    """A broken counter must not break the guard."""
    from gigaforge.core.context_window_guard import (
        ContextWindowGuard,
        estimate_message_tokens,
    )

    def _boom(messages: Sequence[Any], model: Any) -> int | None:
        raise RuntimeError("endpoint down")

    guard = ContextWindowGuard(token_counter=_boom)
    assert guard.count_tokens(_messages()) == estimate_message_tokens(_messages())


def test_guard_without_a_counter_estimates() -> None:
    """The default guard is unchanged: no counter, char/4 estimate."""
    from gigaforge.core.context_window_guard import (
        ContextWindowGuard,
        estimate_message_tokens,
    )

    guard = ContextWindowGuard()
    assert guard.count_tokens(_messages()) == estimate_message_tokens(_messages())


def test_check_uses_the_server_count_for_the_trigger() -> None:
    """The proactive trigger must fire on the real count, not the estimate.

    A tiny window plus a counter that reports an over-trigger count must raise
    even though the text is short enough to pass the char/4 estimate.
    """
    import pytest

    from gigaforge.core.context_window_guard import ContextOverflowError, ContextWindowGuard

    guard = ContextWindowGuard(
        context_window=1000,
        trigger_fraction=0.5,
        minimum_messages=2,
        token_counter=lambda m, model: 900,
    )
    with pytest.raises(ContextOverflowError) as excinfo:
        guard.check(_messages())
    assert excinfo.value.estimated_tokens == 900
    assert excinfo.value.proactive is True


def test_sync_client_exposes_count_tokens() -> None:
    """The guard reaches the provider through ``SyncChatClient.count_tokens``."""
    from gigaforge.core.sync_adapter import SyncChatClient

    assert hasattr(SyncChatClient, "count_tokens")


def test_react_graph_wires_the_server_counter() -> None:
    """The ReAct graph must build its guard with the provider counter."""
    import inspect

    from gigaforge.agents import langgraph_react

    source = inspect.getsource(langgraph_react.build_react_graph)
    assert "_server_token_counter" in source

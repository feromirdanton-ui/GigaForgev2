"""Shared test configuration for required runtime and optional SDK tests."""
from __future__ import annotations

import importlib.util

import pytest


def _installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


#: True when the optional LangGraph extra is importable.
HAS_LANGGRAPH = _installed("langgraph")

#: True when the official GigaChat SDK is importable.
HAS_GIGACHAT_SDK = _installed("gigachat")

#: Test modules that need the official ``gigachat`` SDK.
_GIGACHAT_SDK_MODULES = frozenset(
    {
        "test_merge_stage0.py",
        "test_merge_structured_output.py",
        "test_structured_output_live.py",
    }
)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail test startup when a required LangGraph runtime is missing."""
    if not HAS_LANGGRAPH:
        raise pytest.UsageError(
            "LangGraph is a required runtime dependency. Install project dependencies "
            "with `python -m pip install -e .` before running tests."
        )


def _module_of(item: pytest.Item) -> str:
    """Return a collected test's file name."""
    path = getattr(item, "path", None) or getattr(item, "fspath", None)
    return path.name if path is not None else ""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests requiring only the optional official GigaChat SDK."""
    if not HAS_GIGACHAT_SDK:
        skip = pytest.mark.skip(reason="optional dependency 'gigachat' is not installed")
        for item in items:
            if _module_of(item) in _GIGACHAT_SDK_MODULES:
                item.add_marker(skip)

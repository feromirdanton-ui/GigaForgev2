"""Shared test configuration.

**Optional dependencies must skip, not fail.**  ``langgraph`` and the official
``gigachat`` SDK are optional extras: the project is designed to run on a bare
install (the classic loop, the legacy transport), and the merge plan says so
explicitly.  A test that imports either one at module scope turns "this extra
is not installed" into a red suite, which is the wrong signal — the product is
fine, the environment is minimal.

The fixtures below are the single place that decides.  A test module that needs
LangGraph calls ``pytest.importorskip("langgraph")`` itself (the idiomatic
form, and it keeps the skip reason next to the test); these helpers exist for
the modules that need a *built* graph rather than the bare package.
"""
from __future__ import annotations

import importlib.util

import pytest


def _installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


#: True when the optional LangGraph extra is importable.
HAS_LANGGRAPH = _installed("langgraph")

#: True when the official GigaChat SDK is importable.
HAS_GIGACHAT_SDK = _installed("gigachat")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the LangGraph- and SDK-dependent tests on a bare install.

    Marking is by module path rather than by a decorator on every test: the
    dependency is a property of the module, and repeating the skip in twenty
    places is how one of them ends up missing.
    """
    if not HAS_LANGGRAPH:
        skip = pytest.mark.skip(reason="optional dependency 'langgraph' is not installed")
        for item in items:
            if "langgraph" in item.nodeid:
                item.add_marker(skip)
    if not HAS_GIGACHAT_SDK:
        skip = pytest.mark.skip(reason="optional dependency 'gigachat' is not installed")
        for item in items:
            if "gigachat_sdk" in item.nodeid or "sdk_adapter" in item.nodeid:
                item.add_marker(skip)

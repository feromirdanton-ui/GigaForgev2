"""Shared test configuration.

**Optional dependencies must skip, not fail.**  ``langgraph`` and the official
``gigachat`` SDK are optional extras: the project is designed to run on a bare
install (the classic loop, the legacy transport), and the merge plan says so
explicitly.  A test that imports either one at module scope turns "this extra
is not installed" into a red suite, which is the wrong signal — the product is
fine, the environment is minimal.

The skip is decided here, once, by **module**, not by a substring of the test
id.  The previous version matched ``"langgraph" in item.nodeid``, which only
caught tests whose *name* happened to contain the word: a bare install then
reported 28 failures instead of 28 skips, because
``test_merge_agent_layer.py::test_goal_graph_completes_and_publishes`` needs
LangGraph but never says so in its name.  A dependency is a property of the
module, so the module is what is matched.
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

#: Test modules that cannot even be imported without LangGraph.
#:
#: Listed explicitly rather than inferred: a module that imports
#: ``gigaforge.core.langgraph_runtime`` at module scope fails at *collection*
#: time, so the skip has to be applied before the import is attempted.
_LANGGRAPH_MODULES = frozenset(
    {
        "test_merge_agent_layer.py",
        "test_merge_server_tokens.py",
    }
)

#: Test modules that need the official ``gigachat`` SDK.
_GIGACHAT_SDK_MODULES = frozenset(
    {
        "test_merge_stage0.py",
        "test_merge_structured_output.py",
        "test_structured_output_live.py",
    }
)


def _module_of(item: pytest.Item) -> str:
    """The test module's file name, e.g. ``test_merge_agent_layer.py``."""
    path = getattr(item, "path", None) or getattr(item, "fspath", None)
    return path.name if path is not None else ""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the LangGraph- and SDK-dependent tests on a bare install."""
    if not HAS_LANGGRAPH:
        skip = pytest.mark.skip(reason="optional dependency 'langgraph' is not installed")
        for item in items:
            if _module_of(item) in _LANGGRAPH_MODULES:
                item.add_marker(skip)
    if not HAS_GIGACHAT_SDK:
        skip = pytest.mark.skip(reason="optional dependency 'gigachat' is not installed")
        for item in items:
            if _module_of(item) in _GIGACHAT_SDK_MODULES:
                item.add_marker(skip)

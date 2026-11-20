"""LangGraph plumbing shared by the agent layer (level 2 of the stack).

The merge plan splits GigaForge in two explicit levels:

* **Level 1 — deterministic core** (``core/dag.py`` with ``requires``/``provides``
  contracts, ``l0/``, ``firewall/``, ``router.py`` + ``budgets.py`` +
  ``breaker.py``, ``bench/``).  It stays exactly as it is and is invoked *as a
  tool* from graph nodes.
* **Level 2 — agent runtime** (goal mode, chat, ReAct, sub-agents) on LangGraph:
  checkpointers, ``interrupt()`` for approval, event streaming instead of the
  hand-rolled loops in ``goal.py``.

This module holds the parts both levels need to agree on: whether LangGraph is
installed at all, how a checkpointer is opened, how a graph chunk becomes a
GigaForge event, and how a thread is addressed.

**Graceful degradation.**  ``langgraph`` is an optional dependency.  Importing
this module never fails: when the package is missing,
:data:`LANGGRAPH_AVAILABLE` is ``False`` and the classic ``GoalRunner`` /
``ReActRunner`` paths keep working on a bare install.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence, Tuple

__all__ = [
    "LANGGRAPH_AVAILABLE",
    "LANGGRAPH_IMPORT_ERROR",
    "langgraph_available",
    "open_checkpointer",
    "thread_config",
    "iter_graph_events",
    "graph_state_summary",
]

try:  # pragma: no cover - exercised by the import guard test
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command, interrupt

    LANGGRAPH_AVAILABLE = True
    LANGGRAPH_IMPORT_ERROR = ""
except Exception as _exc:  # noqa: BLE001 - an optional dependency must not break import
    InMemorySaver = None  # type: ignore[assignment]
    SqliteSaver = None  # type: ignore[assignment]
    Command = None  # type: ignore[assignment]
    interrupt = None  # type: ignore[assignment]
    LANGGRAPH_AVAILABLE = False
    LANGGRAPH_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


def langgraph_available() -> bool:
    """True when the LangGraph agent runtime can be used."""
    return LANGGRAPH_AVAILABLE


def open_checkpointer(
    path: Optional[str | Path] = None,
) -> Tuple[Any, Optional[sqlite3.Connection]]:
    """Open a checkpointer for a goal/chat thread.

    ``path`` given  -> a SQLite file (durable across processes, which is what
    makes ``interrupt()`` resumable after a restart).
    ``path`` None   -> an in-memory saver (tests, one-shot runs).

    Returns ``(saver, connection)``; the caller must keep the connection alive
    for as long as the saver is used and close it afterwards.
    """
    if not LANGGRAPH_AVAILABLE:  # pragma: no cover - guarded by callers
        raise RuntimeError(
            "langgraph is not installed: pip install 'gigaforge[langgraph]' "
            f"({LANGGRAPH_IMPORT_ERROR})"
        )
    if path is None:
        return InMemorySaver(), None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # ``check_same_thread=False`` because the graph is driven from the caller's
    # thread while the UI may read state from another one.
    conn = sqlite3.connect(str(target), check_same_thread=False)
    return SqliteSaver(conn), conn


def thread_config(thread_id: str, **configurable: Any) -> dict[str, Any]:
    """The ``config`` a graph is invoked with.  ``thread_id`` is the identity."""
    payload: dict[str, Any] = {"thread_id": str(thread_id)}
    payload.update(configurable)
    return {"configurable": payload}


def iter_graph_events(
    graph: Any,
    graph_input: Any,
    config: Mapping[str, Any],
    *,
    stream_mode: Sequence[str] = ("custom", "updates"),
) -> Iterator[tuple[str, Any]]:
    """Yield ``(mode, chunk)`` pairs from ``graph.stream``.

    ``custom`` chunks are what nodes write with ``get_stream_writer()`` — the
    replacement for polling the JSONL trace file.  ``updates`` chunks carry the
    per-node state delta, which is how a caller learns a node finished without
    the node having to emit anything.
    """
    for chunk in graph.stream(graph_input, config, stream_mode=list(stream_mode)):
        if isinstance(chunk, tuple) and len(chunk) == 2 and isinstance(chunk[0], str):
            yield chunk[0], chunk[1]
        else:  # single-mode stream returns bare chunks
            yield "updates", chunk


def graph_state_summary(graph: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    """A small, JSON-safe view of a thread: next nodes, interrupts, values."""
    snapshot = graph.get_state(config)
    interrupts = []
    for item in getattr(snapshot, "interrupts", ()) or ():
        interrupts.append({"id": getattr(item, "id", ""), "value": getattr(item, "value", None)})
    return {
        "next": list(getattr(snapshot, "next", ()) or ()),
        "interrupts": interrupts,
        "values": dict(getattr(snapshot, "values", {}) or {}),
    }

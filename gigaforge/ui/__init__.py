"""GigaForge V2 UI layer — terminal, web and agentic front ends.

The interface is layered so each layer only knows the one below it::

    commands (typer)      argument parsing, exit codes
      presentation        tui.py / webui.py / chat.py — rendering only
      renderers           console / markdown / json
      snapshot            one UI-ready dict, assembled once
      analytics           pure KPI maths, no I/O
      store (READ-ONLY)   the only door to SQLite + JSONL

Nothing above ``store`` opens a database, and nothing in the read path writes.
That is what lets a dashboard be opened from another process, tomorrow, against
the same run, and show the same numbers the TUI showed at the time.
"""

from __future__ import annotations

from .analytics import (
    human_bytes,
    human_duration,
    human_usd,
    percentile,
    rate,
)
from .snapshot import NODE_ORDER, SNAPSHOT_VERSION, build_series, build_snapshot
from .store import UI_METRICS, UiStore, canonical, read_jsonl, stored_variants

__all__ = [
    "UiStore",
    "UI_METRICS",
    "canonical",
    "stored_variants",
    "read_jsonl",
    "build_snapshot",
    "build_series",
    "NODE_ORDER",
    "SNAPSHOT_VERSION",
    "human_bytes",
    "human_duration",
    "human_usd",
    "percentile",
    "rate",
]

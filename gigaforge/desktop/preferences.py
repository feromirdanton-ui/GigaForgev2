"""Small user-level desktop preferences, independent from the open project."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

__all__ = ["load_preferences", "save_preferences"]

_LOCK = threading.RLock()
_DASHBOARD_BLOCKS = ("hero", "actions", "workspace")
_DEFAULTS = {
    "schema_version": 1,
    "approval_mode": "ask",
    "dashboard_order": list(_DASHBOARD_BLOCKS),
    "chat_sidebar_width": 224,
}


def load_preferences() -> dict[str, Any]:
    try:
        payload = json.loads(_preferences_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_preferences()
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return _default_preferences()
    mode = str(payload.get("approval_mode") or "ask").strip().lower()
    try:
        sidebar_width = int(payload.get("chat_sidebar_width", 224))
    except (TypeError, ValueError):
        sidebar_width = 224
    sidebar_width = max(184, min(420, sidebar_width))
    order = payload.get("dashboard_order")
    valid_order = (
        [str(item) for item in order]
        if isinstance(order, list) and len(order) == len(_DASHBOARD_BLOCKS)
        else list(_DASHBOARD_BLOCKS)
    )
    if set(valid_order) != set(_DASHBOARD_BLOCKS):
        valid_order = list(_DASHBOARD_BLOCKS)
    return {
        **_DEFAULTS,
        "approval_mode": mode if mode in {"ask", "always_allow"} else "ask",
        "dashboard_order": valid_order,
        "chat_sidebar_width": sidebar_width,
    }


def _default_preferences() -> dict[str, Any]:
    return {**_DEFAULTS, "dashboard_order": list(_DASHBOARD_BLOCKS)}


def save_preferences(
    *,
    approval_mode: str | None = None,
    dashboard_order: list[str] | None = None,
    chat_sidebar_width: int | None = None,
) -> None:
    with _LOCK:
        current = load_preferences()
        mode = str(
            current["approval_mode"] if approval_mode is None else approval_mode
        ).strip().lower()
        if mode not in {"ask", "always_allow"}:
            raise ValueError("approval_mode must be 'ask' or 'always_allow'")
        order = list(
            current["dashboard_order"] if dashboard_order is None else dashboard_order
        )
        if len(order) != len(_DASHBOARD_BLOCKS) or set(order) != set(_DASHBOARD_BLOCKS):
            raise ValueError("dashboard_order must contain hero, actions, and workspace once each")
        sidebar_width = max(184, min(420, int(
            current.get("chat_sidebar_width", 224)
            if chat_sidebar_width is None else chat_sidebar_width
        )))
        path = _preferences_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            {
                **_DEFAULTS,
                "approval_mode": mode,
                "dashboard_order": order,
                "chat_sidebar_width": sidebar_width,
            }, indent=2
        ) + "\n"
        fd, name = tempfile.mkstemp(prefix=".preferences-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _preferences_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (
            Path(base) / "GigaForge" / "desktop.json"
            if base
            else Path.home() / "AppData/Local/GigaForge/desktop.json"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/GigaForge/desktop.json"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "gigaforge" / "desktop.json"

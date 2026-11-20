"""ThinkTool — a local scratchpad the model can call to structure reasoning.

Ported from ``ai-forever/deepagents-gigachat``'s ``ThinkToolMiddleware``.  The
tool does nothing but echo its argument back: its value is that it forces the
model to *externalise* a plan (output fields, ordering rules, edge cases)
before it starts editing, which measurably reduces the "wrote the wrong shape"
class of failure on multi-rule tasks.

It is a plain callable plus a JSON-schema declaration, so it can be registered
in GigaForge's tool registry or handed to a provider's ``functions=[...]``
argument without any framework dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "THINK_TOOL_NAME",
    "THINK_TOOL_SCHEMA",
    "ThinkTool",
    "think",
    "ThinkLog",
]

THINK_TOOL_NAME = "think"

#: GigaChat-style function declaration (flat, not the OpenAI envelope).
THINK_TOOL_SCHEMA: dict[str, Any] = {
    "name": THINK_TOOL_NAME,
    "description": (
        "Use this tool as a scratchpad to structure intermediate reasoning. "
        "Call it before a multi-step change to enumerate the exact output fields "
        "and types, ordering/tie-break rules, missing-value conventions, and every "
        "edge-case branch from the user's request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thought": {
                "type": "string",
                "description": "A thought to think about.",
            }
        },
        "required": ["thought"],
    },
}


def think(thought: str = "") -> str:
    """Return *thought* unchanged — the tool's only job is to make it explicit."""
    return thought if isinstance(thought, str) else str(thought)


@dataclass
class ThinkLog:
    """Bounded record of what the model thought, for the trace."""

    max_entries: int = 50
    entries: list[str] = field(default_factory=list)

    def record(self, thought: str) -> str:
        text = think(thought)
        if text.strip():
            self.entries.append(text)
            if len(self.entries) > self.max_entries:
                del self.entries[: len(self.entries) - self.max_entries]
        return text

    def to_dict(self) -> dict[str, Any]:
        return {"count": len(self.entries), "entries": list(self.entries)}


class ThinkTool:
    """Callable tool object with the schema and a bounded log attached."""

    name = THINK_TOOL_NAME
    schema = THINK_TOOL_SCHEMA

    def __init__(self, *, max_entries: int = 50) -> None:
        self.log = ThinkLog(max_entries=max_entries)

    def __call__(self, thought: str = "", **_: Any) -> str:
        return self.log.record(thought)

    def invoke(self, args: Optional[dict[str, Any]] = None) -> str:
        """Registry-shaped entry point: ``invoke({"thought": "..."})``."""
        payload = args or {}
        return self(payload.get("thought", ""))

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "schema": self.schema, "log": self.log.to_dict()}

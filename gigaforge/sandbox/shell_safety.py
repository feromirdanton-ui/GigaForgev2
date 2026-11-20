"""ShellSafety — block the shell-serialisation patterns GigaChat gets wrong.

Ported from ``ai-forever/deepagents-gigachat``'s ``ShellSafetyMiddleware``.
Two shapes account for most of the wasted rounds:

1. **Multi-line content embedded in a quoted shell string.**  The model writes
   ``sh -c "line1\nline2"`` or a double-quoted ``echo`` with newlines; the shell
   mangles it, or the quoting breaks outright.  The fix is a structured
   write/edit tool or a single-quoted heredoc.
2. **A ``python -c`` one-liner with a compound statement after ``;``.**  This is
   a guaranteed ``SyntaxError`` — ``python -c "x=1; for v in xs: ..."`` cannot
   parse.  The fix is a script file or a heredoc.

The check is a pure function over the command string, so it can run *before*
the command reaches the sandbox allowlist and before any process is spawned.
It is advisory in the sense that it returns a reason, not an exception: the
caller decides whether to block (the sandbox does) or to warn.

Note the deliberate non-goal: this is not a security boundary.  The sandbox
allowlist and ``shell=False`` are.  This module exists to stop a *correct but
unparseable* command from burning a round.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "ShellSafety",
    "ShellSafetyVerdict",
    "unsafe_reason",
    "is_safe",
    "SHELL_SAFETY_MARKER",
]

SHELL_SAFETY_MARKER = "[SHELL-SAFETY]"

#: ``python -c "..."`` where a compound statement follows a ``;``.
_PY_C_COMPOUND = re.compile(
    r"\bpython3?\s+-c\s+(['\"]).*;\s*(for|if|while|def|class|with|try)\b",
    re.S,
)

#: A heredoc is the sanctioned way to pass multi-line content.
_HEREDOC = re.compile(r"<<-?\s*['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?")


@dataclass(frozen=True)
class ShellSafetyVerdict:
    """The outcome of a safety check."""

    safe: bool
    reason: str = ""
    pattern: str = ""

    def message(self) -> str:
        """The text to hand back to the model when the command was blocked."""
        if self.safe:
            return ""
        return (
            f"{SHELL_SAFETY_MARKER} blocked unsafe execute command: {self.reason} "
            "Do not retry the same command shape."
        )

    def to_dict(self) -> dict[str, Any]:
        return {"safe": self.safe, "reason": self.reason, "pattern": self.pattern}


def unsafe_reason(command: str) -> Optional[tuple[str, str]]:
    """Return ``(pattern, reason)`` when *command* is unsafe, else ``None``."""
    if not command:
        return None
    text = str(command)

    # 1. Multi-line content inside a quoted shell string, with no heredoc.
    if (
        "\n" in text
        and not _HEREDOC.search(text)
        and any(token in text for token in ('"', "`", "$("))
    ):
        return (
            "multiline-in-quoted-string",
            "multi-line content is embedded in a shell string. Use a structured "
            "write/edit tool or a single-quoted heredoc instead.",
        )

    # 2. python -c with a compound statement after ';'.
    if _PY_C_COMPOUND.search(text):
        return (
            "python-c-compound",
            "python -c one-liner contains a statement after ';'. Write a script "
            "file or use a heredoc instead.",
        )

    return None


def is_safe(command: str) -> bool:
    """True when *command* passes both checks."""
    return unsafe_reason(command) is None


class ShellSafety:
    """Object form of the check, for wiring into a tool-call pipeline."""

    def __init__(self, *, tool_names: tuple[str, ...] = ("execute", "shell", "bash")) -> None:
        self.tool_names = tuple(tool_names)

    def check(self, command: str) -> ShellSafetyVerdict:
        found = unsafe_reason(command)
        if found is None:
            return ShellSafetyVerdict(safe=True)
        pattern, reason = found
        return ShellSafetyVerdict(safe=False, reason=reason, pattern=pattern)

    def check_tool_call(self, tool_name: str, args: Any) -> ShellSafetyVerdict:
        """Check one tool call; non-shell tools always pass."""
        if tool_name not in self.tool_names:
            return ShellSafetyVerdict(safe=True)
        command = ""
        if isinstance(args, dict):
            command = str(args.get("command") or args.get("cmd") or "")
        elif args is not None:
            command = str(args)
        return self.check(command)

    def describe(self) -> dict[str, Any]:
        return {
            "tool_names": list(self.tool_names),
            "patterns": ["multiline-in-quoted-string", "python-c-compound"],
        }

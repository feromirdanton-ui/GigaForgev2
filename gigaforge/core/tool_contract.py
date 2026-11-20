"""ToolContract — state the tools that are actually usable in this run.

Ported from ``ai-forever/deepagents-gigachat``'s ``ToolContractMiddleware``.
The failure it prevents is specific: a generic harness prompt advertises tools
(``ls``, ``read_file``, ``glob``, ``write_file``, ``edit_file``, ``grep``,
``execute``) that a particular run does not expose, and the model then invents
flags and subcommands for tools that are not there — burning rounds on
``invalid choice`` / ``unrecognized arguments``.

The contract is a short, explicit block appended **once** to the conversation
as a human message (never a system message: GigaChat rejects a system message
that is not first).  It is idempotent — the marker is checked across the whole
history, so a long run does not accumulate copies.

The default contract text is written for GigaForge's own tool surface, and the
per-tool descriptions carry the two hints that measurably reduce failures:
strip the ``<line_no>\\t`` prefix from ``read_file`` output, and strip the
leading ``/`` from ``glob``/``grep`` paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from gigaforge.providers.messages import count_nudges, nudge

__all__ = [
    "ToolContract",
    "TOOL_CONTRACT_MARKER",
    "DEFAULT_TOOL_DESCRIPTIONS",
    "build_contract_text",
]

TOOL_CONTRACT_MARKER = "[TOOL-CONTRACT]"

#: Descriptions tuned to GigaChat's tool-calling behaviour.
DEFAULT_TOOL_DESCRIPTIONS: dict[str, str] = {
    "ls": (
        "List files in a directory. Use relative paths: `ls .` or `ls src`. "
        "Do NOT use absolute paths."
    ),
    "read_file": (
        "Read a file. Use a relative path like 'foo.py' or 'src/foo.py'. "
        "Output is prefixed with '<line_no>\\t' for display — strip that prefix "
        "before reusing the text in edit_file/write_file."
    ),
    "glob": (
        "Find files by pattern (e.g. '**/*.py'). Returned paths may start with "
        "'/'; strip the leading '/' before writing them to any output file."
    ),
    "write_file": (
        "Create a file or overwrite it completely. Use a relative path. The "
        "content is the file body verbatim — do NOT include line-number prefixes "
        "from read_file output."
    ),
    "edit_file": (
        "Replace one exact occurrence of old_string with new_string in an "
        "existing file. STRIP the leading '<line_no>\\t' prefix from read_file "
        "output before putting text into old_string or new_string."
    ),
    "grep": (
        "Search for a literal substring (NOT a regex) across files. Pass exactly "
        "ONE phrase per call and always pass `path` to scope the search. If 0 "
        "matches, use `execute` with a Python 3 heredoc and pathlib.rglob instead "
        "of retrying grep."
    ),
    "execute": (
        "Run one short shell command in the workspace directory. Use RELATIVE "
        "paths. Never embed multi-line content in a double-quoted shell string; "
        "use a single-quoted heredoc if you must run a multi-line snippet."
    ),
    "think": (
        "Use this tool as a scratchpad to structure intermediate reasoning before "
        "a multi-step change."
    ),
}


def build_contract_text(
    tools: Iterable[str],
    *,
    descriptions: Optional[Mapping[str, str]] = None,
    extra: str = "",
) -> str:
    """Render the contract block for *tools*.

    Only the tools actually available are listed — that is the whole point.
    An unknown tool name is listed with a generic line rather than dropped, so
    a custom tool is still declared.
    """
    table = dict(DEFAULT_TOOL_DESCRIPTIONS)
    if descriptions:
        table.update(descriptions)
    lines = ["Available tools in this run (use ONLY these; do not invent flags):"]
    for name in tools:
        description = table.get(name, "Available tool.")
        lines.append(f"- `{name}`: {description}")
    if extra.strip():
        lines.append("")
        lines.append(extra.strip())
    return "\n".join(lines)


@dataclass
class ToolContract:
    """The contract for one run, and the logic that injects it exactly once."""

    tools: tuple[str, ...] = ()
    text: str = ""
    descriptions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text and self.tools:
            self.text = build_contract_text(self.tools, descriptions=self.descriptions)

    @classmethod
    def for_tools(cls, tools: Sequence[str], **kwargs: Any) -> "ToolContract":
        return cls(tools=tuple(tools), **kwargs)

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    def already_injected(self, messages: Sequence[Any]) -> bool:
        return count_nudges(messages, TOOL_CONTRACT_MARKER) > 0

    def message(self) -> dict[str, Any]:
        """The contract as a wire-legal HUMAN message."""
        return nudge(self.text, marker=TOOL_CONTRACT_MARKER)

    def maybe_inject(self, messages: Sequence[Any]) -> Optional[dict[str, Any]]:
        """Return the contract message when it has not been sent yet."""
        if self.empty or not messages or self.already_injected(messages):
            return None
        return self.message()

    def describe(self) -> dict[str, Any]:
        return {"tools": list(self.tools), "chars": len(self.text)}

"""LoopBreaker — detect an agent stuck in a loop and nudge it out.

Ported from ``ai-forever/deepagents-gigachat``'s ``LoopBreakerMiddleware``.
The four signatures are the ones that were empirically found to burn a whole
step budget on GigaChat-3-Ultra:

1. **Same call three times.**  The identical ``(tool, args)`` repeated across
   three consecutive tool rounds — the model committed to a broken pattern.
2. **Same error family three times.**  The arguments drift but the failure does
   not (``python-syntax``, ``edit-miss``, ``missing-path``, ``invalid-cli-args``,
   ``shell-safety``, ``traceback``).  This is the more common shape: the model
   keeps "fixing" the wrong thing.
3. **Two empty greps in a row.**  Typical on counting tasks, where grep will
   never return a match and the model keeps trying.
4. **Round-budget overrun.**  At 12 tool rounds: switch to a bounded batch
   strategy.  At 24: finish now.

Two properties matter as much as the detection:

* **Every nudge is a HUMAN message.**  GigaChat rejects a system message that
  is not first in the conversation with a hard 400, so a mid-run instruction
  cannot be a system message.  :func:`gigaforge.providers.messages.nudge`
  enforces the shape.
* **Every nudge is bounded.**  Its marker is counted across the whole history,
  so it is not re-injected on every subsequent turn — a loop-breaker that
  itself loops is worse than none.  Markers are separate per signature, so a
  failed-tool loop cannot suppress a later empty-grep loop in the same run.

The breaker is pure: it reads a message list and returns the next nudge (or
``None``).  It never calls a model and never mutates the history.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from gigaforge.providers.messages import HUMAN_ROLE, count_nudges, nudge

__all__ = [
    "LoopBreaker",
    "LoopSignal",
    "ERROR_FAMILIES",
    "BATCH_ROUND_THRESHOLD",
    "FINAL_ROUND_THRESHOLD",
    "MARKER_ERROR",
    "MARKER_GREP",
    "MARKER_BATCH",
    "MARKER_FINAL",
]

MARKER_ERROR = "[LOOP-BREAKER-ERROR]"
MARKER_GREP = "[LOOP-BREAKER-GREP]"
MARKER_BATCH = "[BUDGET-NUDGE-BATCH]"
MARKER_FINAL = "[BUDGET-NUDGE-FINAL]"

#: At this many tool rounds the agent is told to switch to a batch strategy.
BATCH_ROUND_THRESHOLD = 12
#: At this many tool rounds the agent is told to finish.
FINAL_ROUND_THRESHOLD = 24

#: How many times one signature may nudge before it goes quiet.
MAX_INJECTIONS = 2

#: Error family → the substrings that identify it in a tool result.
ERROR_FAMILIES: dict[str, tuple[str, ...]] = {
    "invalid-cli-args": ("unrecognized arguments", "invalid choice", "unknown command"),
    "shell-command-not-found": ("command not found",),
    "python-syntax": ("syntaxerror",),
    "missing-path": ("no such file", "read-only file system"),
    "edit-miss": ("string not found",),
    "shell-safety": ("[shell-safety]",),
    "traceback": ("traceback",),
}

_ERROR_MARKERS = (
    "Error:",
    "error:",
    "Cannot ",
    "cannot ",
    "Traceback",
    "[stderr]",
    "Exit code: 1",
    "Exit code: 2",
    "SyntaxError",
    "FileNotFoundError",
    "No such file",
    "String not found",
    "Read-only file system",
    "unrecognized arguments",
    "invalid choice",
    "unknown command",
    "command not found",
    "[SHELL-SAFETY]",
)

_EMPTY_GREP_MARKERS = ("no matches", "0 matches", "not found", "no results", "0 results")


@dataclass
class LoopSignal:
    """What the breaker saw, and the nudge it produced (if any)."""

    kind: str
    marker: str
    text: str
    detail: dict[str, Any] = field(default_factory=dict)

    def message(self) -> dict[str, Any]:
        """The nudge as a wire-legal HUMAN message."""
        return nudge(self.text, marker=self.marker)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "marker": self.marker, "detail": dict(self.detail)}


# -- message helpers ---------------------------------------------------------
def _role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("role", "")).lower()
    return str(getattr(message, "role", "")).lower()


def _content(message: Any) -> str:
    if isinstance(message, Mapping):
        value = message.get("content", "")
    else:
        value = getattr(message, "content", "")
    return "" if value is None else str(value)


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, Mapping):
        calls = message.get("tool_calls") or []
    else:
        calls = getattr(message, "tool_calls", None) or []
    return [c for c in calls if isinstance(c, Mapping)]


def _tool_name(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("name") or message.get("tool_name") or "")
    return str(getattr(message, "name", "") or "")


def _is_assistant(message: Any) -> bool:
    return _role(message) in {"assistant", "ai", "model"}


def _is_tool(message: Any) -> bool:
    return _role(message) in {"tool", "function"}


def _result_is_error(text: str) -> bool:
    return bool(text) and any(marker in text for marker in _ERROR_MARKERS)


def _error_family(text: str) -> Optional[str]:
    lowered = text.lower()
    for family, markers in ERROR_FAMILIES.items():
        if any(marker in lowered for marker in markers):
            return family
    return None


def _grep_looks_empty(text: str) -> bool:
    if not text or not text.strip():
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _EMPTY_GREP_MARKERS)


def _count_tool_rounds(messages: Sequence[Any]) -> int:
    return sum(1 for m in messages if _is_assistant(m) and _tool_calls(m))


def _last_n_tool_pairs(
    messages: Sequence[Any], n: int
) -> Optional[list[tuple[str, str, str]]]:
    """Last *n* consecutive ``(tool_name, args_json, result_text)`` triples.

    Walks back over assistant→tool pairs.  Returns ``None`` as soon as the chain
    breaks (a human message, an assistant turn with no tool calls, a tool
    message with no matching assistant turn).
    """
    pairs: list[tuple[str, str, str]] = []
    i = len(messages) - 1
    while i >= 0 and len(pairs) < n:
        message = messages[i]
        if _is_tool(message):
            if i == 0:
                return None
            assistant = messages[i - 1]
            if not _is_assistant(assistant):
                return None
            calls = _tool_calls(assistant)
            if not calls:
                return None
            call = calls[0]
            pairs.append(
                (
                    str(call.get("name", "")),
                    json.dumps(call.get("args", {}), sort_keys=True, default=str),
                    _content(message),
                )
            )
            i -= 2
        elif _is_assistant(message):
            # Trailing assistant turn with no result yet — skip it.
            i -= 1
        else:
            break
    return pairs if len(pairs) == n else None


class LoopBreaker:
    """Detect the four loop signatures and produce at most one nudge per turn."""

    def __init__(
        self,
        *,
        batch_rounds: int = BATCH_ROUND_THRESHOLD,
        final_rounds: int = FINAL_ROUND_THRESHOLD,
        max_injections: int = MAX_INJECTIONS,
    ) -> None:
        if batch_rounds < 1 or final_rounds <= batch_rounds:
            raise ValueError("final_rounds must be greater than batch_rounds")
        self.batch_rounds = batch_rounds
        self.final_rounds = final_rounds
        self.max_injections = max_injections

    # -- public API ----------------------------------------------------------
    def inspect(self, messages: Sequence[Any]) -> Optional[LoopSignal]:
        """Return the nudge this turn should carry, or ``None``."""
        if not messages:
            return None

        rounds = _count_tool_rounds(messages)

        # 4. Round budget — checked first: it is the cheapest and the most
        #    urgent signal (the run is about to be cut off).
        if rounds >= self.final_rounds and count_nudges(messages, MARKER_FINAL) == 0:
            return LoopSignal(
                kind="budget-final",
                marker=MARKER_FINAL,
                text=self._budget_text(rounds, final=True),
                detail={"tool_rounds": rounds},
            )
        if rounds >= self.batch_rounds and count_nudges(messages, MARKER_BATCH) == 0:
            return LoopSignal(
                kind="budget-batch",
                marker=MARKER_BATCH,
                text=self._budget_text(rounds, final=False),
                detail={"tool_rounds": rounds},
            )

        # 3. Two empty greps in a row.
        grep_pairs = _last_n_tool_pairs(messages, 2)
        if (
            grep_pairs
            and all(pair[0] == "grep" for pair in grep_pairs)
            and all(_grep_looks_empty(pair[2]) for pair in grep_pairs)
            and count_nudges(messages, MARKER_GREP) < self.max_injections
        ):
            return LoopSignal(
                kind="empty-grep",
                marker=MARKER_GREP,
                text=self._grep_text(),
                detail={"tool_rounds": rounds},
            )

        # 1 + 2. Three identical calls, or three failures of one family.
        pairs = _last_n_tool_pairs(messages, 3)
        if not pairs:
            return None
        names = {pair[0] for pair in pairs}
        all_same_call = pairs[0] == pairs[1] == pairs[2]
        all_same_tool_errors = len(names) == 1 and all(
            _result_is_error(pair[2]) for pair in pairs
        )
        families = [_error_family(pair[2]) for pair in pairs]
        all_same_family = bool(families[0]) and families[0] == families[1] == families[2]
        if not (all_same_call or all_same_tool_errors or all_same_family):
            return None
        if count_nudges(messages, MARKER_ERROR) >= self.max_injections:
            return None
        return LoopSignal(
            kind="repeated-failure",
            marker=MARKER_ERROR,
            text=self._error_text(pairs[0][0], pairs[0][2]),
            detail={
                "tool": pairs[0][0],
                "family": families[0],
                "identical_call": all_same_call,
                "tool_rounds": rounds,
            },
        )

    def nudge_for(self, messages: Sequence[Any]) -> Optional[dict[str, Any]]:
        """Convenience: the nudge message itself, ready to append."""
        signal = self.inspect(messages)
        return signal.message() if signal else None

    # -- nudge texts ---------------------------------------------------------
    @staticmethod
    def _budget_text(rounds: int, *, final: bool) -> str:
        if final:
            return (
                f"You have made {rounds} tool calls. Complete the remaining "
                "deliverables now, run one focused verification, fix any reported "
                "failure, and finish. Do not restart exploration or repeat a failed "
                "call shape."
            )
        return (
            f"You have made {rounds} tool calls. Preserve completed work, but switch "
            "to a bounded batch strategy now.\n"
            "- Use one script/command for repeated edits, parsing, or ordered operations.\n"
            "- Do not retry the same tool/error shape.\n"
            "- Then verify the requested outputs once and finish."
        )

    @staticmethod
    def _grep_text() -> str:
        return (
            "grep returned 0 matches twice. Do NOT grep again.\n"
            "Switch to one `execute` call using `python3 <<'PY' ... PY`, scan the "
            "right directory with pathlib.rglob, and write the requested final output. "
            "Do not create a helper file inside the scanned workspace."
        )

    @staticmethod
    def _error_text(tool_name: str, last_result: str) -> str:
        snippet = (last_result or "")[:300]
        return (
            f"You have called `{tool_name}` 3 times in a row and it keeps failing "
            f"(last error: {snippet!r}). STOP repeating this approach. Change strategy:\n"
            "- If `edit_file` says 'String not found' and the text came from "
            "`read_file`: you are leaking the leading '<line_no>\\t' prefix. Strip the "
            "spaces + number + tab before reusing the text.\n"
            "- If `python3 -c \"...\"` keeps giving SyntaxError: switch to one `execute` "
            "call with a single-quoted Python 3 heredoc.\n"
            "- If a filesystem tool path failed, use a relative path like `foo.py` or "
            "`src/foo.py`. Do NOT use absolute paths.\n"
            "- If `grep`/`glob` returns nothing useful: for count tasks, write a Python 3 "
            "heredoc with pathlib.rglob — do NOT keep grepping.\n"
            "- If `write_file` says 'already exists': the right tool is `edit_file`, NOT "
            "another `write_file` with a new name.\n"
            "- If a CLI/runtime tool says 'invalid choice' or 'unrecognized arguments': "
            "stop inventing flags. Use only the tool contract supplied for this run.\n"
            "- If shell safety blocked a command: use structured file/runtime tools or a "
            "single-quoted heredoc.\n"
            "Do something materially different on the next step."
        )

    def describe(self) -> dict[str, Any]:
        return {
            "batch_rounds": self.batch_rounds,
            "final_rounds": self.final_rounds,
            "max_injections": self.max_injections,
            "signatures": [
                "same-call-x3",
                "same-error-family-x3",
                "empty-grep-x2",
                "round-budget",
            ],
            "nudge_role": HUMAN_ROLE,
        }

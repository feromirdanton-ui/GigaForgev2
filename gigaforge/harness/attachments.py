"""Attachment tracking — the harness notices a re-sent file, the model decides.

**The problem this solves.**  A user attaches ``parser.py``, the agent works on
it, and then the user attaches ``parser.py`` again — edited.  Today the second
attachment is just another blob of text in the prompt: the model has no way to
know it is looking at a *revision* of something it already read, so it re-reads
the whole file, re-derives what it already knew, and spends its budget on
rediscovery.  Worse, when the two versions disagree the model has no signal
about which one is current.

**What the harness does instead.**  It keeps a per-run record of every attached
file, keyed by name, with a content digest.  On a repeat it produces a
:class:`BackCall` — a deterministic, model-free statement of what changed:

* **unchanged** — the same bytes came back.  The harness says so and the caller
  can skip re-sending the body entirely.  This is the token win: a 40 KB file
  re-attached unchanged costs one line instead of 10 000 tokens.
* **changed** — a unified diff against the previous revision, plus the counts.
  The model gets the *delta*, which is what it actually needs to correct its
  plan, instead of the whole file again.
* **new** — first sight of this name; nothing to compare, so the caller sends
  the body as usual.

**Why the harness and not the model.**  Deciding "is this the same file, and
what changed" is a hash and a diff — deterministic work with a right answer.
Asking a model to do it costs tokens and can be wrong.  The model's job is what
to *do* about the change; the harness's job is to hand it the change.

**Why keyed by name, not by path.**  The user's mental model is "the same file",
and a re-attachment routinely arrives from a different directory (a download
folder, a temp path).  Keying by path would report every re-send as new.  The
name is the identity the user means; the path is recorded alongside it for the
report, not used as the key.
"""
from __future__ import annotations

import difflib
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AttachmentRevision",
    "BackCall",
    "AttachmentTracker",
    "UNCHANGED",
    "CHANGED",
    "NEW",
]

#: The three verdicts a re-attachment can produce.
UNCHANGED = "unchanged"
CHANGED = "changed"
NEW = "new"

#: A diff longer than this is summarised rather than inlined.  A model does not
#: need 4 000 changed lines to know a file was rewritten; it needs to know that
#: it was, and to be told to re-read it.
_MAX_DIFF_LINES = 400


@dataclass(frozen=True)
class AttachmentRevision:
    """One sighting of an attached file."""

    name: str
    path: str
    digest: str
    size: int
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "digest": self.digest,
            "size": self.size,
        }


@dataclass
class BackCall:
    """What the harness tells the caller about a re-attached file.

    ``verdict`` is one of :data:`NEW`, :data:`UNCHANGED`, :data:`CHANGED`.
    ``diff`` is a unified diff for ``CHANGED`` and empty otherwise.
    ``note`` is the one-line instruction the caller can put in the prompt.
    """

    name: str
    verdict: str
    previous: AttachmentRevision | None = None
    current: AttachmentRevision | None = None
    diff: str = ""
    added: int = 0
    removed: int = 0
    note: str = ""

    @property
    def is_repeat(self) -> bool:
        return self.verdict in {UNCHANGED, CHANGED}

    @property
    def needs_body(self) -> bool:
        """True when the caller must still send the file's text.

        An unchanged file does not need its body re-sent — that is the whole
        point of tracking it.  A changed file does, because the model has to
        work against the new revision; the diff tells it what moved.
        """
        return self.verdict != UNCHANGED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict,
            "added": self.added,
            "removed": self.removed,
            "needs_body": self.needs_body,
            "note": self.note,
            "previous": self.previous.to_dict() if self.previous else None,
            "current": self.current.to_dict() if self.current else None,
            "diff": self.diff,
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _counts(diff_lines: Sequence[str]) -> tuple[int, int]:
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return added, removed


class AttachmentTracker:
    """Per-run record of attached files, keyed by name.

    Usage::

        tracker = AttachmentTracker()
        for path in attachments:
            call = tracker.observe(path)
            if call.needs_body:
                prompt += read(path)
            if call.is_repeat:
                prompt += call.note

    The tracker is deliberately per-run: a new run is a new conversation, and
    carrying a digest across runs would let a stale "unchanged" verdict suppress
    a file the model has never actually seen.
    """

    def __init__(self, *, max_diff_lines: int = _MAX_DIFF_LINES) -> None:
        self._seen: dict[str, AttachmentRevision] = {}
        self._max_diff_lines = max_diff_lines
        #: Every back call produced, in order — the run's attachment history.
        self.history: list[BackCall] = []

    # -- public API ----------------------------------------------------------
    def observe(self, path: Path | str, *, text: str | None = None) -> BackCall:
        """Record one attachment and return what the caller should do about it.

        ``text`` lets a caller that already decoded the file avoid a second
        read; when omitted the file is read here.  A file that cannot be read
        is reported as :data:`NEW` with an empty body rather than raising — an
        unreadable attachment must not abort a run.
        """
        target = Path(path)
        name = target.name
        if text is None:
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""

        current = AttachmentRevision(
            name=name,
            path=str(target),
            digest=_digest(text),
            size=len(text.encode("utf-8", errors="replace")),
            text=text,
        )
        previous = self._seen.get(name)
        self._seen[name] = current

        if previous is None:
            call = BackCall(
                name=name,
                verdict=NEW,
                current=current,
                note=f"[ATTACHMENT] {name} is new to this run ({current.size} bytes).",
            )
        elif previous.digest == current.digest:
            call = BackCall(
                name=name,
                verdict=UNCHANGED,
                previous=previous,
                current=current,
                note=(
                    f"[ATTACHMENT] {name} is byte-identical to the copy you already "
                    "have — do not re-read it; use what you already know."
                ),
            )
        else:
            diff_lines = list(
                difflib.unified_diff(
                    previous.text.splitlines(),
                    current.text.splitlines(),
                    fromfile=f"{name} (previous)",
                    tofile=f"{name} (current)",
                    lineterm="",
                )
            )
            added, removed = _counts(diff_lines)
            if len(diff_lines) > self._max_diff_lines:
                diff = "\n".join(diff_lines[: self._max_diff_lines]) + "\n[diff truncated]"
            else:
                diff = "\n".join(diff_lines)
            call = BackCall(
                name=name,
                verdict=CHANGED,
                previous=previous,
                current=current,
                diff=diff,
                added=added,
                removed=removed,
                note=(
                    f"[ATTACHMENT] {name} was re-sent with changes "
                    f"(+{added}/-{removed} lines). The diff below is authoritative: "
                    "any plan step that assumed the previous revision must be "
                    "corrected before it is executed."
                ),
            )

        self.history.append(call)
        return call

    def observe_all(self, paths: Iterable[Path | str]) -> list[BackCall]:
        """Observe several attachments in order."""
        return [self.observe(path) for path in paths]

    def prompt_block(self, calls: Sequence[BackCall]) -> str:
        """Render the back calls as one prompt section.

        Only repeats produce text: a first sighting needs no announcement, and
        saying "this is new" for every attachment is noise the model pays for.
        """
        repeats = [call for call in calls if call.is_repeat]
        if not repeats:
            return ""
        parts: list[str] = []
        for call in repeats:
            parts.append(call.note)
            if call.verdict == CHANGED and call.diff:
                parts.append("```diff\n" + call.diff + "\n```")
        return "\n\n".join(parts)

    def known(self) -> list[str]:
        """Names seen so far, in first-seen order."""
        return list(self._seen)

    def describe(self) -> dict[str, Any]:
        return {
            "tracked": len(self._seen),
            "names": self.known(),
            "repeats": sum(1 for call in self.history if call.is_repeat),
            "changed": sum(1 for call in self.history if call.verdict == CHANGED),
            "unchanged": sum(1 for call in self.history if call.verdict == UNCHANGED),
        }

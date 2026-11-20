"""Deterministic fallbacks — what the harness does when the model stops helping.

**The failure this exists for.**  A ReAct step is a JSON object.  When the model
answers with prose, the loop appends a "return only JSON" nudge and tries again.
That is the right first response, but it is the *only* response: a model that
keeps answering in prose burns the entire step budget on reformat requests and
the episode ends with nothing.  Measured on a live run — the model located the
files, then spent its remaining steps failing to wrap the answer in JSON.

**The principle.**  A harness takes on the work the model is failing at, so the
model can go back to the part only it can do.  Reformatting is not a reasoning
task: if the model will not emit the envelope, the harness can still act on what
it *did* say, and it can still explore deterministically.  So after a bounded
number of parse failures the loop stops asking and starts doing:

* **step 1 failure** — nudge once (unchanged behaviour; the model usually
  complies, and a nudge is cheaper than any fallback).
* **step 2 failure** — run a deterministic ``grep`` derived from the goal.  The
  episode gets a real observation instead of a third reformat request, and the
  observation is what a later salvage needs.
* **step 3 failure** — stop exploring and force the finish.  Three failures is
  not a formatting accident; it is the model being unable to answer in this
  shape, and more steps will not change that.

Every fallback is deterministic and model-free, so it cannot itself fail in a
new way, and each one is recorded so a report can say the episode degraded
rather than pretending it ran normally.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FallbackAction",
    "ParseFailureFallback",
    "derive_grep_pattern",
    "NUDGE",
    "EXPLORE",
    "FORCE_FINISH",
]

#: What the fallback decided to do.
NUDGE = "nudge"
EXPLORE = "explore"
FORCE_FINISH = "force_finish"

#: Words that carry no search value.  A grep for "the" over a repository is a
#: wasted tool call, and the model's own prompt is full of them.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
        "these", "those", "is", "are", "was", "were", "be", "been", "being", "to",
        "of", "in", "on", "at", "by", "for", "with", "from", "into", "as", "it",
        "its", "we", "you", "your", "our", "i", "me", "my", "do", "does", "did",
        "can", "could", "should", "would", "will", "shall", "may", "might", "must",
        "not", "no", "yes", "all", "any", "some", "each", "every", "there", "here",
        "what", "which", "who", "when", "where", "why", "how", "please", "need",
        "needs", "want", "make", "made", "use", "using", "used", "get", "got",
        "add", "added", "fix", "fixed", "change", "changed", "update", "updated",
        "file", "files", "code", "repo", "repository", "project", "task", "goal",
    }
)

#: Identifier-shaped tokens are the best grep material: a symbol name is
#: specific, and a specific pattern is what makes a search cheap.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def derive_grep_pattern(goal: str, *, max_terms: int = 3) -> str:
    """Build a grep pattern from a goal, deterministically.

    Prefers identifier-shaped tokens (``validate_email``, ``parse_config``) over
    prose words, because a symbol name is what actually appears in the source.
    Returns an empty string when the goal has nothing searchable, so the caller
    can skip the fallback rather than run a grep that cannot match.
    """
    text = goal or ""
    # A backticked or quoted token is the user pointing at a symbol directly.
    explicit = re.findall(r"`([^`]{2,60})`", text)
    if explicit:
        return "|".join(re.escape(token.strip()) for token in explicit[:max_terms] if token.strip())

    candidates: list[str] = []
    for match in _IDENT_RE.finditer(text):
        token = match.group(0)
        if token.casefold() in _STOPWORDS:
            continue
        if token not in candidates:
            candidates.append(token)
        if len(candidates) >= max_terms:
            break
    if not candidates:
        return ""
    return "|".join(re.escape(token) for token in candidates)


@dataclass
class FallbackAction:
    """What the loop should do after a parse failure."""

    kind: str
    action: str = ""
    action_input: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "action": self.action, "note": self.note}


@dataclass
class ParseFailureFallback:
    """Bounded, deterministic response to a model that will not emit JSON.

    ``explore_after`` and ``finish_after`` are the thresholds, in consecutive
    failures.  They are constructor arguments so a test can pin the behaviour
    without depending on the defaults, and so a profile can tune them.
    """

    explore_after: int = 2
    finish_after: int = 3
    #: Set once the deterministic exploration has been used, so it happens at
    #: most once per episode — a fallback that repeats is just another loop.
    explored: bool = False
    #: Every decision taken, for the run record.
    history: list[FallbackAction] = field(default_factory=list)

    def next_action(self, goal: str, consecutive_failures: int) -> FallbackAction:
        """Decide what to do after *consecutive_failures* parse failures."""
        if consecutive_failures >= self.finish_after:
            action = FallbackAction(
                kind=FORCE_FINISH,
                note=(
                    f"{consecutive_failures} consecutive parse failures: the model "
                    "cannot answer in this shape, so stop asking and finish with "
                    "what the episode already observed."
                ),
            )
        elif consecutive_failures >= self.explore_after and not self.explored:
            pattern = derive_grep_pattern(goal)
            if pattern:
                self.explored = True
                action = FallbackAction(
                    kind=EXPLORE,
                    action="grep",
                    action_input=pattern,
                    note=(
                        "the model is not emitting JSON; the harness runs a "
                        f"deterministic search for {pattern!r} so the episode "
                        "still produces an observation"
                    ),
                )
            else:
                action = FallbackAction(
                    kind=NUDGE,
                    note="no searchable term in the goal; nudging again",
                )
        else:
            action = FallbackAction(
                kind=NUDGE,
                note="ask once more for the JSON envelope",
            )
        self.history.append(action)
        return action

    def describe(self) -> dict[str, Any]:
        return {
            "explored": self.explored,
            "decisions": [action.to_dict() for action in self.history],
        }

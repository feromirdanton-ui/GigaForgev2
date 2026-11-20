"""Harness profiles: named bundles of policy for a class of run.

A single set of defaults cannot serve both "answer a question about this repo"
and "make a change in a 200k-line monorepo".  The first should never be allowed
to write and should stop early; the second needs a bigger step budget and a
tighter compaction trigger.  Encoding that as scattered ``if`` statements is how
a codebase ends up with behaviour nobody can name.

A profile is a *named* policy, resolved from ``GIGAFORGE_HARNESS_PROFILE``, and
``gigaforge stack`` prints the one that will actually be used.  Unknown names
fall back to ``default`` with a warning rather than raising: a typo in an env
var must not stop a run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

__all__ = ["HarnessProfile", "PROFILES", "resolve_profile"]


@dataclass(frozen=True)
class HarnessProfile:
    """A named bundle of run policy."""

    name: str
    #: May the run change the tree?  A read-only profile refuses write tools at
    #: the guard, not at the prompt — a prompt is a request, a guard is a rule.
    allow_writes: bool = True
    #: ReAct step ceiling.
    max_steps: int = 6
    #: Fraction of the context window at which compaction fires.
    compact_at: float = 0.85
    #: How many times the whole episode may be re-run after a compaction.
    max_compactions: int = 2
    #: Observation cache size (0 disables it).
    observation_cache: int = 64
    #: Description shown by ``gigaforge stack``.
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "allow_writes": self.allow_writes,
            "max_steps": self.max_steps,
            "compact_at": self.compact_at,
            "max_compactions": self.max_compactions,
            "observation_cache": self.observation_cache,
            "description": self.description,
        }


PROFILES: dict[str, HarnessProfile] = {
    "default": HarnessProfile(
        name="default",
        description="balanced: writes allowed, 6 steps, compact at 85%",
    ),
    "read_only": HarnessProfile(
        name="read_only",
        allow_writes=False,
        max_steps=4,
        compact_at=0.80,
        description="investigation only: write tools refused by the guard",
    ),
    "large_repo": HarnessProfile(
        name="large_repo",
        max_steps=10,
        compact_at=0.70,
        max_compactions=3,
        observation_cache=128,
        description="big tree: more steps, earlier compaction, bigger cache",
    ),
    "fast": HarnessProfile(
        name="fast",
        max_steps=3,
        compact_at=0.90,
        max_compactions=1,
        observation_cache=32,
        description="cheap: 3 steps, late compaction, small cache",
    ),
}


def resolve_profile(name: str | None = None) -> HarnessProfile:
    """Resolve a profile by name, defaulting to ``GIGAFORGE_HARNESS_PROFILE``.

    An unknown name returns ``default`` — a typo in an environment variable must
    not stop a run, and silently using a *different* profile than the one asked
    for would be worse than using the documented default.
    """
    requested = (name or os.environ.get("GIGAFORGE_HARNESS_PROFILE") or "default").strip()
    profile = PROFILES.get(requested)
    if profile is None:
        return replace(PROFILES["default"], name=f"default (unknown: {requested})")
    return profile

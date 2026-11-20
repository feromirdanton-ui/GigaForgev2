"""Resolve an external binary, and treat "not installed" as "skipped".

**Why this module exists.**  Three separate places in this codebase treated a
missing tool as a failure of the work:

* ``l0/lint.py`` — ``lint()`` reported a missing ``ruff`` as a lint error, so
  every edit failed on a machine without ruff.  That one was fixed in place;
* ``l0/lint.py`` — ``format_code()`` had the identical bug one function below,
  and ``_run_ruff_check`` hard-coded ``"ruff"`` while the ``Linter`` class
  already knew how to resolve it from config;
* ``agents/repl_tool.py`` — the REPL ran ``["python", ...]``.  On a host where
  only ``python3`` exists (which is most Linux distributions, and this
  project's own CI) the tool failed exactly the way ``bench`` did before it was
  fixed.

The rule this module encodes: **no call to an external binary may treat the
binary's absence as a failure of the work.**  A missing tool is a property of
the machine, not of the code under test, and the honest answer is "skipped".

Resolution is by ``shutil.which``, with an explicit override so a caller can
point at a specific interpreter (a virtualenv's ``python``, a pinned ``ruff``).
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ResolvedTool", "resolve_tool", "python_bin"]


@dataclass(frozen=True)
class ResolvedTool:
    """The outcome of resolving a binary: a path, or a reason there is none."""

    name: str
    path: str = ""
    reason: str = ""
    #: The bare name to put in an argv.  A sandbox that resolves commands via
    #: ``PATH`` rejects an absolute path ("command must be a bare name"), so a
    #: caller that runs through one must use this, not ``path``.
    argv_name: str = ""

    @property
    def found(self) -> bool:
        return bool(self.path)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "path": self.path, "reason": self.reason}


def resolve_tool(name: str, *, override: str = "") -> ResolvedTool:
    """Find *name* on ``PATH``, honouring an explicit *override*.

    An override is used as given when it is an existing path, and resolved on
    ``PATH`` when it is a bare name — so ``override="ruff"`` and
    ``override="/opt/ruff"`` both work, and a caller never has to know which
    form it holds.
    """
    wanted = str(override or name or "").strip()
    if not wanted:
        return ResolvedTool(name=str(name or ""), reason="no tool name given")
    found = shutil.which(wanted)
    if found:
        # ``argv_name`` is the bare name when the caller passed one, and the
        # basename when it passed a path: a PATH-resolving sandbox rejects an
        # absolute path in argv, so the caller must not use ``path`` there.
        bare = wanted if not any(ch in wanted for ch in "/\\") else Path(found).name
        return ResolvedTool(name=str(name or wanted), path=found, argv_name=bare)
    return ResolvedTool(
        name=str(name or wanted),
        reason=f"{wanted!r} not found on PATH",
    )


def python_bin(*, override: str = "") -> ResolvedTool:
    """The interpreter to run a scratch program with.

    ``sys.executable`` first: it is the interpreter the project is actually
    installed into, so a REPL step sees the same packages the harness does.
    ``python3`` and ``python`` are the fallbacks, in that order — the reverse
    order is what broke on hosts that ship only ``python3``.
    """
    if override:
        return resolve_tool("python", override=override)
    if sys.executable and shutil.which(sys.executable):
        return ResolvedTool(
            name="python", path=sys.executable, argv_name=Path(sys.executable).name
        )
    for candidate in ("python3", "python"):
        found = shutil.which(candidate)
        if found:
            return ResolvedTool(name="python", path=found, argv_name=candidate)
    return ResolvedTool(name="python", reason="no python interpreter found")

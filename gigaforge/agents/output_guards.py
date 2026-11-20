"""Two guards ported from ``ai-forever/deepagents-gigachat`` that GigaForge lacked.

The merge had already ported four of upstream's middleware (shell safety, tool
contract, specification audit, think tool) and wired them through
:class:`gigaforge.agents.guards.ReActGuards`.  Two were missing, and both fix a
failure that shows up on real GigaChat runs:

:class:`PathNormalizer`
    Tools that walk a tree return paths rooted at the workspace
    (``/src/foo.py``).  The model copies them verbatim into a plan, and the
    executor then looks for ``/src/foo.py`` on the host — which either does not
    exist or, worse, is a real absolute path outside the repository.  Upstream
    strips the leading slash from ``glob``/``grep`` results; here the same
    normalisation is applied to any observation that is a path list.

:class:`DeterministicOutput`
    The "wrote a number it made up instead of computing it" class of failure.
    Upstream blocks a direct write of a derived scalar when the workspace
    already contains source files and no deterministic tool has run.  GigaForge
    does not let the model write files directly — it emits a plan — so the
    equivalent guard is on the *plan*: a step that claims a computed value
    without a preceding computation step is flagged.

Both are pure: they read a value and return a verdict.  Neither calls a model.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PathNormalizer",
    "DeterministicOutput",
    "DeterministicVerdict",
    "normalize_paths",
]

#: A path token that starts at the beginning of a line or after whitespace and
#: looks like ``/dir/file.ext``.  Deliberately conservative: it must contain a
#: dot-suffix or a second slash, so a bare ``/`` or a regex like ``/foo/`` in
#: prose is not rewritten.
_ABS_PATH = re.compile(r"(?<![\w:/])(/(?:[\w.\-]+/)+[\w.\-]+)")

#: Suffixes that make a token a file path rather than a URL fragment.
_PATH_SUFFIXES = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml",
    ".md", ".txt", ".cfg", ".ini", ".sql", ".html", ".css", ".sh", ".bat",
    ".rs", ".go", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
)


def normalize_paths(text: str) -> str:
    """Strip the leading ``/`` from workspace-relative paths in *text*.

    Only tokens that look like a file path are touched, and a URL
    (``https://host/a/b.py``) is left alone because the lookbehind rejects a
    match preceded by ``:`` or ``/``.
    """
    if not text:
        return text

    def _fix(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.lower().endswith(_PATH_SUFFIXES):
            return token.lstrip("/")
        return token

    return _ABS_PATH.sub(_fix, text)


@dataclass
class PathNormalizer:
    """Rewrite absolute-looking workspace paths in tool observations."""

    #: Tool names whose output is a path list.  Empty means "every tool".
    tool_names: tuple[str, ...] = ("grep", "read_structure", "glob", "ls")

    def applies_to(self, tool: str) -> bool:
        return not self.tool_names or tool in self.tool_names

    def normalize(self, tool: str, observation: str) -> str:
        """Return the observation with workspace paths made relative."""
        if not self.applies_to(tool):
            return observation
        return normalize_paths(observation)

    def describe(self) -> dict[str, Any]:
        return {"tool_names": list(self.tool_names)}


@dataclass
class DeterministicVerdict:
    """What the deterministic-output guard decided about one plan."""

    blocked: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocked

    def to_dict(self) -> dict[str, Any]:
        return {"blocked": list(self.blocked), "reason": self.reason}


#: Tools that read or compute rather than assert a value.
_COMPUTE_TOOLS = frozenset(
    {"grep", "read_structure", "run_tests", "think", "tests", "lint", "reviewer"}
)

#: A step whose argument is a bare number is claiming a computed value.
_BARE_NUMBER = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*$")


@dataclass
class DeterministicOutput:
    """Flag a plan that asserts a derived value without computing it.

    Upstream's version blocks a ``write_file`` of a guessed scalar.  GigaForge's
    model does not write files — it emits a plan — so the same rule is applied
    to the plan: a step that carries a bare number as its target, with no
    preceding observation or computation step, is a guess dressed as a result.

    The guard is deliberately narrow.  It fires only when the plan's *first*
    step asserts a number, because a number after a ``grep``/``read_structure``
    may legitimately be a value the model just read.
    """

    compute_tools: frozenset[str] = _COMPUTE_TOOLS

    def check(self, steps: Sequence[Any]) -> DeterministicVerdict:
        verdict = DeterministicVerdict()
        seen_compute = False
        for step in steps:
            tool = str(getattr(step, "tool", "") or "").strip()
            if tool in self.compute_tools:
                seen_compute = True
                continue
            args = getattr(step, "args", None) or {}
            if not isinstance(args, dict):
                continue
            for key, value in args.items():
                if not isinstance(value, str) or not _BARE_NUMBER.match(value):
                    continue
                if seen_compute:
                    # A number after a read may be a value that was read.
                    continue
                verdict.blocked.append(f"{tool}:{key}={value.strip()}")
                verdict.reason = (
                    "a derived value is asserted before anything was read or "
                    "computed; read the source first, then state the value"
                )
        return verdict

    def describe(self) -> dict[str, Any]:
        return {"compute_tools": sorted(self.compute_tools)}


def iter_paths(text: str) -> Iterable[str]:
    """Yield the path-looking tokens in *text* (used by tests and reports)."""
    for match in _ABS_PATH.finditer(text or ""):
        token = match.group(1)
        if token.lower().endswith(_PATH_SUFFIXES):
            yield token

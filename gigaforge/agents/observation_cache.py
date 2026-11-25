"""Observation cache for the ReAct loop.

A ReAct episode re-reads the same file constantly.  In the live probe the model
grepped, read a structure, grepped again with a slightly different pattern, and
read the same file a second time — four tool calls, two of them byte-identical
to earlier ones.  Each repeat costs a tool invocation and, worse, a full
observation in the trajectory, which is what pushes a run into compaction.

The cache is deliberately narrow, because a stale observation is a correctness
bug and a cache that is wrong is worse than no cache:

* only **read-only** tools are cached (``grep``, ``read_file``, ``read_structure``); a tool
  that can change the tree is never cached, so a write is always observed;
* the key includes the tool, its input **and** the mtime/size of every file the
  workspace has touched since the run started — so an edit invalidates the
  entries that could have been affected;
* the cache is per-run and bounded, so a long episode cannot grow it without
  limit.

The invalidation is coarse on purpose: any change to the workspace drops the
whole cache.  A finer scheme would need to know which files a ``grep`` read,
which is exactly the kind of cleverness that produces a stale answer nobody can
reproduce.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["ObservationCache", "CACHEABLE_TOOLS", "READ_ONLY_TOOLS"]

#: Tools whose output is a pure function of the tree.  Anything that can write
#: is excluded by construction, not by a flag someone can forget to set.
CACHEABLE_TOOLS = frozenset({"grep", "read_file", "read_structure"})

#: Tools that cannot change the tree, and so may run in a parallel batch.
#:
#: This is deliberately a *different* set from :data:`CACHEABLE_TOOLS`.
#: ``web_search`` is read-only — it cannot touch the workspace — but it is not
#: cached here, because it has its own cache with a TTL (a search result is a
#: fact about the world, not about the tree, and the tree-stamp invalidation
#: that makes the observation cache correct would be meaningless for it).
#: Conflating the two sets would either let a writer into a parallel batch or
#: make a search result expire whenever a file changed.
READ_ONLY_TOOLS = CACHEABLE_TOOLS | {"web_search"}


@dataclass
class ObservationCache:
    """A bounded, per-run cache of read-only tool observations."""

    root: Path | str | None = None
    max_entries: int = 64
    _entries: dict[str, str] = field(default_factory=dict)
    _stamp: str = ""
    hits: int = 0
    misses: int = 0

    def __post_init__(self) -> None:
        self._stamp = self._workspace_stamp()

    # -- invalidation --------------------------------------------------------
    def _workspace_stamp(self) -> str:
        """A cheap fingerprint of the tree's *content*.

        ``(path, mtime_ns, ctime_ns, size, content-hash)`` for every file,
        hashed.  Walking the tree on every tool call would be too slow for a
        large repository, so the walk is capped and the cap is part of the
        stamp: a tree that grows past the cap changes the stamp once and then
        stays stable.

        The content hash is not belt-and-braces, it is the only thing that
        actually works.  Timestamps are not a reliable change signal at this
        granularity: two writes inside the same filesystem timestamp tick leave
        ``mtime_ns``, ``ctime_ns`` *and* ``size`` identical, so a stamp built
        from metadata alone does not move and a stale observation survives the
        write.  Measured here: a test that rewrote a file with same-length
        content flaked roughly one run in three, and the two writes differed by
        0 ns.  Hashing the bytes is exact, and the walk is already reading the
        directory entries, so the extra cost is one read per file per tool call
        — bounded by the same cap.
        """
        if self.root is None:
            return ""
        root = Path(self.root)
        if not root.is_dir():
            return ""
        digest = hashlib.sha256()
        count = 0
        try:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                if any(part in {".git", "__pycache__", ".venv", "node_modules"}
                       for part in path.parts):
                    continue
                try:
                    stat = path.stat()
                    body = path.read_bytes()
                except OSError:
                    continue
                digest.update(
                    f"{path.relative_to(root)}:{stat.st_size}:"
                    f"{hashlib.sha256(body).hexdigest()}\n".encode()
                )
                count += 1
                if count >= 2000:
                    digest.update(b"<capped>")
                    break
        except OSError:
            return ""
        return digest.hexdigest()

    def _refresh(self) -> None:
        stamp = self._workspace_stamp()
        if stamp != self._stamp:
            # The tree changed: every cached observation may now be wrong.
            self._entries.clear()
            self._stamp = stamp

    # -- public API ----------------------------------------------------------
    @staticmethod
    def _key(tool: str, action_input: Any) -> str:
        try:
            payload = json.dumps(action_input, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = str(action_input)
        return f"{tool}\x00{payload}"

    def get(self, tool: str, action_input: Any) -> str | None:
        """Return a cached observation, or ``None``."""
        if tool not in CACHEABLE_TOOLS:
            return None
        self._refresh()
        value = self._entries.get(self._key(tool, action_input))
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def put(self, tool: str, action_input: Any, observation: str) -> None:
        """Store an observation, evicting the oldest entry when full."""
        if tool not in CACHEABLE_TOOLS:
            return
        if len(self._entries) >= self.max_entries:
            # Insertion-ordered dict: drop the oldest.
            oldest = next(iter(self._entries))
            self._entries.pop(oldest, None)
        self._entries[self._key(tool, action_input)] = observation

    def clear(self) -> None:
        self._entries.clear()
        self._stamp = self._workspace_stamp()

    def describe(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "cacheable_tools": sorted(CACHEABLE_TOOLS),
        }

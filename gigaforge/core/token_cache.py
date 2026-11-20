"""Prefix-cached token counting for the context guard.

``ContextWindowGuard`` asks the provider for the real token count before every
request.  On a ReAct loop that is one extra HTTP round trip per step — a
six-step episode doubles its request count for accounting alone.

The trajectory, however, only ever *grows*: step N's message list is step N-1's
plus a few appended messages.  So the count of the shared prefix never changes,
and re-asking for it is pure waste.  This module memoises the counter by a hash
of the message list, which turns the per-step cost into one call for the first
step and a cache hit for every later one.

Two deliberate limits:

* **Bounded.**  A long run must not grow an unbounded dict; the cache is an
  LRU with a small default capacity (a ReAct episode is tens of steps, not
  thousands).
* **Fail-open.**  A counter that raises returns ``None`` so the guard keeps its
  char/4 estimate.  Counting is an optimisation and must never fail a run.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any

__all__ = ["CachedTokenCounter", "trajectory_key"]

DEFAULT_CAPACITY = 64


def trajectory_key(messages: Sequence[Any], model: Any = None) -> str:
    """A stable key for a message list + model.

    Hashes role and content of every message, in order, plus the model id: the
    same text under a different model is a different count, and a message list
    that differs only in role order is a different request.
    """
    hasher = hashlib.sha256()
    hasher.update(str(model or "").encode("utf-8"))
    hasher.update(b"\x00")
    for message in messages:
        if isinstance(message, dict):
            role = str(message.get("role", ""))
            content = str(message.get("content", "") or "")
            name = str(message.get("name", "") or "")
        else:
            role = str(getattr(message, "role", ""))
            content = str(getattr(message, "content", "") or "")
            name = str(getattr(message, "name", "") or "")
        hasher.update(role.encode("utf-8"))
        hasher.update(b"\x01")
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x01")
        hasher.update(content.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


class CachedTokenCounter:
    """Wrap a ``(messages, model) -> int | None`` counter with an LRU cache.

    The wrapper keeps the guard's call signature exactly, so it can be passed
    as ``ContextWindowGuard(token_counter=...)`` without the guard knowing that
    caching exists.
    """

    def __init__(
        self,
        counter: Callable[[Sequence[Any], Any], int | None],
        *,
        capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        if not callable(counter):
            raise TypeError("CachedTokenCounter needs a callable counter")
        self._counter = counter
        self._capacity = max(1, int(capacity))
        self._cache: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def __call__(self, messages: Sequence[Any], model: Any = None) -> int | None:
        key = trajectory_key(messages, model)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self.hits += 1
                return self._cache[key]
        self.misses += 1
        try:
            counted = self._counter(messages, model)
        except Exception:  # noqa: BLE001 - counting is an optimisation
            return None
        if counted is None:
            return None
        value = int(counted)
        with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "size": len(self._cache)}

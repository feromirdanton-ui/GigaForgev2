"""Cooperative cancellation primitives for runs and individual DAG nodes."""
from __future__ import annotations

import threading
import time
from typing import Optional

from .errors import RunCancelledError


class CancellationToken:
    """Thread-safe cancellation signal with optional parent propagation.

    Child tokens can be cancelled independently (for example when one node hits
    its deadline) while still observing cancellation of the whole run.
    Cancellation is cooperative: long-running operations must check the token at
    their boundaries, and blocking I/O must also have its own finite timeout.
    """

    def __init__(self, parent: Optional["CancellationToken"] = None) -> None:
        self._parent = parent
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    def child(self) -> "CancellationToken":
        return CancellationToken(parent=self)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(self._parent and self._parent.cancelled)

    @property
    def reason(self) -> str:
        if self._event.is_set():
            with self._lock:
                return self._reason
        return self._parent.reason if self._parent is not None else ""

    def cancel(self, reason: str = "cancelled") -> bool:
        """Set this token once; return whether this call changed its state."""
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = str(reason or "cancelled")
            self._event.set()
            return True

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RunCancelledError(self.reason or "operation cancelled")

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait until cancellation, including parent cancellation, or timeout."""
        if self.cancelled:
            return True
        if timeout is not None and timeout <= 0:
            return self.cancelled
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.cancelled:
            interval = 0.05
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.cancelled
                interval = min(interval, remaining)
            self._event.wait(interval)
        return True


__all__ = ["CancellationToken"]

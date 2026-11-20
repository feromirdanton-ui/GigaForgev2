"""Background work for the desktop window.

Tk is single-threaded: every widget touch must happen on the thread that created
the interpreter.  Model calls take seconds and pipeline runs take minutes, so
they cannot happen there.

:class:`Worker` owns exactly one daemon thread plus a job queue, and posts every
completion back through a thread-safe queue that the window drains on a Tk timer.
A single serial worker makes the window's state deterministic — one long job at a
time, everything else queued — whereas a thread per action would let a stuck run
and a fast refresh race each other into the same widgets.

Callbacks travel *with* the result, so there is no global registry to leak and no
label collision to reason about.
"""
from __future__ import annotations

import logging
import queue
import threading
import traceback
from typing import Any, Callable, Optional

__all__ = ["Worker", "Job"]

log = logging.getLogger("gigaforge.desktop.worker")

DoneFn = Callable[[Any], None]
ErrorFn = Callable[[BaseException], None]


class Job:
    """A unit of background work with its completion callbacks."""

    __slots__ = ("label", "fn", "on_done", "on_error")

    def __init__(
        self,
        label: str,
        fn: Callable[[], Any],
        on_done: Optional[DoneFn] = None,
        on_error: Optional[ErrorFn] = None,
    ) -> None:
        self.label = label
        self.fn = fn
        self.on_done = on_done
        self.on_error = on_error


class Worker:
    """One background thread; completions are delivered on the UI thread."""

    def __init__(self, *, poll_ms: int = 60) -> None:
        self.poll_ms = int(poll_ms)
        self._jobs: "queue.Queue[Optional[Job]]" = queue.Queue()
        self._done: "queue.Queue[tuple[Job, Any, Optional[BaseException], Optional[str]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._busy: Optional[str] = None
        self._lock = threading.Lock()
        self._stopped = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self, root: Any) -> None:
        """Start the worker thread and the drain timer bound to *root*."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="gigaforge-desktop-worker", daemon=True
        )
        self._thread.start()
        self._schedule(root)

    def _schedule(self, root: Any) -> None:
        if self._stopped:
            return
        try:
            root.after(self.poll_ms, lambda: self._drain(root))
        except Exception:  # pragma: no cover - the window is already gone
            self._stopped = True

    def stop(self) -> None:
        self._stopped = True
        self._jobs.put(None)

    # ── submission ────────────────────────────────────────────────────────
    def submit(
        self,
        label: str,
        fn: Callable[[], Any],
        *,
        on_done: Optional[DoneFn] = None,
        on_error: Optional[ErrorFn] = None,
    ) -> None:
        """Queue *fn*.  ``on_done`` / ``on_error`` run on the UI thread."""
        self._jobs.put(Job(label, fn, on_done, on_error))

    @property
    def busy(self) -> bool:
        return self._busy is not None

    @property
    def busy_label(self) -> str:
        return self._busy or ""

    # ── the worker thread ─────────────────────────────────────────────────
    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            with self._lock:
                self._busy = job.label
            try:
                value = job.fn()
            except BaseException as exc:  # noqa: BLE001 - every failure is surfaced
                detail = traceback.format_exc(limit=8)
                log.warning("job %r failed: %s", job.label, exc)
                self._done.put((job, None, exc, detail))
            else:
                self._done.put((job, value, None, None))
            finally:
                with self._lock:
                    self._busy = None

    # ── the UI thread drain ───────────────────────────────────────────────
    def _drain(self, root: Any) -> None:
        delivered = 0
        while delivered < 32:  # bound the work done in one timer tick
            try:
                job, value, error, detail = self._done.get_nowait()
            except queue.Empty:
                break
            delivered += 1
            try:
                if error is not None:
                    if job.on_error is not None:
                        job.on_error(error)
                    else:
                        log.error("unhandled job error in %r\n%s", job.label, detail)
                elif job.on_done is not None:
                    job.on_done(value)
            except Exception:  # noqa: BLE001 - a broken callback must not stop the loop
                log.exception("completion callback for %r failed", job.label)
        self._schedule(root)

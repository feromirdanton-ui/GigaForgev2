"""Can this machine actually render a page?

``importlib.util.find_spec("playwright")`` answers "is the wheel on disk", which
is not the question any caller has.  ``pip install playwright`` installs the
client and *not* the browser: ``playwright install chromium`` is a separate
step, and CI ran the first without the second.  Every guard that asked
``find_spec`` therefore said "present" while ``chromium.launch()`` raised
``Executable doesn't exist`` -- so the tests failed instead of skipping, and the
product's own render path reported a crash instead of a missing dependency.

This module answers the real question by launching the browser once and caching
the answer.  It is used by the test guards and by ``runtime_dependencies`` so
there is one definition of "browser available" rather than three.
"""
from __future__ import annotations

import importlib.util
import os
import threading

__all__ = ["chromium_available", "reset_cache", "unavailable_reason"]

_LOCK = threading.Lock()
_CACHED: bool | None = None
_REASON = ""


def chromium_available(*, refresh: bool = False) -> bool:
    """True when Playwright and a launchable Chromium are both present.

    The launch costs about a second, so the result is cached for the process.
    ``GIGAFORGE_SKIP_BROWSER_TESTS`` forces ``False`` -- that is how a CI job
    without a browser, or a developer on a locked-down machine, opts out
    without editing tests.
    """
    global _CACHED, _REASON
    with _LOCK:
        if _CACHED is not None and not refresh:
            return _CACHED
        if os.environ.get("GIGAFORGE_SKIP_BROWSER_TESTS"):
            _CACHED, _REASON = False, "GIGAFORGE_SKIP_BROWSER_TESTS is set"
            return False
        if importlib.util.find_spec("playwright") is None:
            _CACHED, _REASON = False, "the playwright package is not installed"
            return False
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                browser.close()
        except Exception as exc:  # noqa: BLE001 - any launch failure means "not usable"
            _CACHED = False
            _REASON = f"Chromium did not launch: {type(exc).__name__}: {exc}"
            return False
        _CACHED, _REASON = True, ""
        return True


def unavailable_reason() -> str:
    """Why :func:`chromium_available` is false, for a skip message or a report."""
    if _CACHED is None:
        chromium_available()
    return _REASON


def reset_cache() -> None:
    """Forget the cached answer (tests that change the environment)."""
    global _CACHED, _REASON
    with _LOCK:
        _CACHED, _REASON = None, ""

"""Self-healing checks for system runtimes used by the harness."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "RuntimeDependencyStatus",
    "ensure_browser_runtime",
    "ensure_goal_support_packages",
    "ensure_qt_quick_runtime",
]

log = logging.getLogger("gigaforge.runtime_dependencies")
_LOCK = threading.Lock()
_CORE_LOCK = threading.Lock()
_READY: RuntimeDependencyStatus | None = None


@dataclass(frozen=True)
class RuntimeDependencyStatus:
    available: bool
    detail: str
    installed: bool = False


def ensure_goal_support_packages(
    *, auto_install: bool = True, timeout_s: int = 120
) -> tuple[str, ...]:
    """Ensure parser/test packages used by Goal are importable at startup."""
    requirements = (
        ("tree_sitter", "tree-sitter>=0.22"),
        ("tree_sitter_python", "tree-sitter-python>=0.21"),
        ("tree_sitter_javascript", "tree-sitter-javascript>=0.21"),
        ("pytest", "pytest>=8.0"),
    )
    issues: list[str] = []
    with _CORE_LOCK:
        missing: list[tuple[str, str]] = []
        for module, requirement in requirements:
            try:
                present = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                present = False
            if not present:
                missing.append((module, requirement))
        if missing and auto_install:
            requirements_to_install = [requirement for _, requirement in missing]
            failure = _install(
                [_python_executable(), "-m", "pip", "install", *requirements_to_install],
                timeout_s,
            )
            if (
                failure
                and sys.prefix == sys.base_prefix
                and any(
                    marker in failure.lower()
                    for marker in (
                        "externally-managed", "permission denied", "access is denied",
                        "operation not permitted",
                    )
                )
            ):
                failure = _install(
                    [
                        _python_executable(), "-m", "pip", "install", "--user",
                        *requirements_to_install,
                    ],
                    timeout_s,
                )
            if not failure:
                importlib.invalidate_caches()

        for module, requirement in missing:
            try:
                present = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                present = False
            if not present:
                detail = (
                    (failure or "installer completed but package is still unavailable")
                    if auto_install
                    else "package is missing"
                )
                issues.append(f"{requirement}: {detail}")
    return tuple(issues)


def ensure_qt_quick_runtime(
    *, auto_install: bool = True, timeout_s: int = 600
) -> RuntimeDependencyStatus:
    """Ensure a supported Qt Quick Python binding is importable.

    PySide6 is the packaged default. PyQt6 is accepted when already installed,
    which is useful for system Python installations and local development.
    """
    for package in ("PySide6", "PyQt6"):
        if _probe_qt_quick(package):
            return RuntimeDependencyStatus(True, f"{package} and Qt Quick are ready")
    if not auto_install:
        return RuntimeDependencyStatus(False, "PySide6/PyQt6 Qt Quick binding is missing")

    failure = _install(
        [_python_executable(), "-m", "pip", "install", "PySide6>=6.8,<7"], timeout_s
    )
    if failure:
        return RuntimeDependencyStatus(False, failure)
    importlib.invalidate_caches()
    if _probe_qt_quick("PySide6"):
        return RuntimeDependencyStatus(True, "PySide6 and Qt Quick are ready", installed=True)
    if _probe_qt_quick("PyQt6"):
        return RuntimeDependencyStatus(True, "PyQt6 and Qt Quick are ready", installed=True)
    return RuntimeDependencyStatus(
        False, "Qt binding installation completed but QtQml/QtQuick could not be imported",
        installed=True,
    )


def _probe_qt_quick(package: str) -> bool:
    try:
        importlib.import_module(f"{package}.QtCore")
        importlib.import_module(f"{package}.QtGui")
        importlib.import_module(f"{package}.QtQml")
        importlib.import_module(f"{package}.QtQuick")
    except (ImportError, OSError, RuntimeError):
        return False
    return True


def ensure_browser_runtime(
    *, auto_install: bool = True, timeout_s: int = 240
) -> RuntimeDependencyStatus:
    """Check Playwright + Chromium, installing missing pieces when requested.

    Only success is cached, so a failed attempt can be retried later if network
    connectivity or local permissions change.
    """
    global _READY
    if _READY is not None:
        return _READY
    with _LOCK:
        if _READY is not None:
            return _READY
        initial = _probe()
        if initial.available:
            _READY = initial
            return initial
        if not auto_install:
            return initial

        deadline = time.monotonic() + max(10, int(timeout_s))

        def remaining() -> int:
            return max(0, int(deadline - time.monotonic()))

        installed = False
        if importlib.util.find_spec("playwright") is None:
            failure = _install(
                [_python_executable(), "-m", "pip", "install", "playwright>=1.45"],
                remaining(),
            )
            if (
                failure
                and sys.prefix == sys.base_prefix
                and any(
                    marker in failure.lower()
                    for marker in (
                        "externally-managed", "permission denied", "access is denied",
                        "operation not permitted",
                    )
                )
            ):
                failure = _install(
                    [
                        _python_executable(), "-m", "pip", "install", "--user",
                        "playwright>=1.45",
                    ],
                    remaining(),
                )
            if failure:
                return RuntimeDependencyStatus(False, failure)
            importlib.invalidate_caches()
            installed = True

        probe = _probe()
        if probe.available:
            _READY = RuntimeDependencyStatus(True, probe.detail, installed=installed)
            return _READY

        failure = _install(
            [_python_executable(), "-m", "playwright", "install", "chromium"], remaining()
        )
        if failure:
            return RuntimeDependencyStatus(False, failure, installed=installed)
        final = _probe()
        if final.available:
            _READY = RuntimeDependencyStatus(
                True, "Playwright and Chromium are ready", installed=True
            )
            return _READY
        return RuntimeDependencyStatus(
            False,
            "Chromium setup finished but launch failed: " + _excerpt(final.detail),
            installed=True,
        )


def _probe() -> RuntimeDependencyStatus:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return RuntimeDependencyStatus(False, "Playwright Python package is missing")

    browser = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
            browser = None
        return RuntimeDependencyStatus(True, "Playwright and Chromium are ready")
    except Exception as exc:  # platform errors include missing browser binaries/libraries
        return RuntimeDependencyStatus(False, f"Chromium launch probe failed: {_excerpt(str(exc))}")
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                log.debug("Chromium probe cleanup failed", exc_info=True)


def _install(command: list[str], timeout_s: int) -> str | None:
    timeout = max(10, int(timeout_s))
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return f"automatic dependency setup timed out after {timeout} seconds"
    except OSError as exc:
        return f"could not start automatic dependency setup: {type(exc).__name__}: {_excerpt(str(exc))}"
    if result.returncode == 0:
        return None
    detail = (
        result.stderr.strip() or result.stdout.strip() or f"installer exited {result.returncode}"
    )
    return f"automatic dependency setup failed: {_excerpt(detail)}"


def _python_executable() -> str:
    """Use the console interpreter for pip, even when the app runs under pythonw."""
    executable = Path(sys.executable)
    if sys.platform == "win32" and executable.name.casefold() == "pythonw.exe":
        console_python = executable.with_name("python.exe")
        if console_python.is_file():
            return str(console_python)
    return sys.executable


def _excerpt(value: str, limit: int = 700) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[:limit] + ("…" if len(normalized) > limit else "")

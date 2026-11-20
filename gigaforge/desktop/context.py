"""Shared state for the desktop window — the model behind the views.

Every view reads through :class:`AppContext` instead of talking to the runtime,
the store or the HTTP server directly.  Three consequences worth stating:

* the snapshot (the one JSON all renderers agree on) is computed in exactly one
  place, with a small TTL, so eight views cannot issue eight queries per refresh;
* the browser control room is a *feature* of this object: it starts the same
  self-contained panel server the CLI serves, on a background thread, and hands
  back a URL.  That is what makes the browser UI secondary to the window rather
  than a parallel product;
* a long job (a run, an index, a provider probe) is a plain callable, so the
  window can hand it to the :class:`~gigaforge.desktop.worker.Worker` without
  knowing what it does.

The context is deliberately tolerant: a missing database, a missing credential
or a broken server are recorded as state and rendered, never raised into Tk's
callback machinery (where an exception is a stack trace on stderr and a frozen
button).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

__all__ = ["AppContext", "BrowserPanel"]

#: Registry provider ids → the names a human recognises.
_PROVIDER_LABELS = {
    "giga": "GigaChat",
    "gigachat": "GigaChat",
    "openai": "OpenAI-compatible",
}

log = logging.getLogger("gigaforge.desktop.context")


@dataclass
class BrowserPanel:
    """The browser control room, as an optional background feature."""

    host: str = "127.0.0.1"
    port: int = 8787
    _httpd: Any = None
    _thread: Optional[threading.Thread] = None
    url: str = ""
    error: str = ""

    @property
    def running(self) -> bool:
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    def start(self, store: Any, *, port: Optional[int] = None,
              host: Optional[str] = None, chat_handler: Optional[Callable[[str], str]] = None,
              prices: Any = None, default_model: str = "GigaChat") -> str:
        """Start the shared control-room server and return its URL."""
        if self.running:
            return self.url
        from http.server import ThreadingHTTPServer

        from gigaforge.ui import webui as W

        self.host = host or self.host
        if port is not None:
            self.port = int(port)
        self.error = ""

        handler = W.make_handler(store, prices=prices, default_model=default_model,
                                 chat_handler=chat_handler)
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd = httpd
        self.url = f"http://{self.host}:{self.port}/"
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="gigaforge-control-room", daemon=True
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        self._thread = None
        if httpd is None:
            return
        try:
            shutdown = getattr(httpd, "shutdown", None)
            if callable(shutdown):
                shutdown()
            close = getattr(httpd, "server_close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001 - shutting down never raises into the UI
            log.debug("browser panel shutdown failed", exc_info=True)


@dataclass
class AppContext:
    """The window's view of the harness."""

    runtime: Any
    offline: bool = False
    snapshot_ttl: float = 1.5
    browser: BrowserPanel = field(default_factory=BrowserPanel)
    last_error: str = ""
    _snapshot: Optional[Dict[str, Any]] = None
    _snapshot_at: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        from .preferences import load_preferences

        self._approval_mode = load_preferences()["approval_mode"]

    # ── construction ──────────────────────────────────────────────────────
    @classmethod
    def create(
        cls,
        *,
        repo_root: Optional[str] = None,
        require_provider: bool = False,
        probe_models: bool = False,
    ) -> "AppContext":
        """Build a local runtime without making the window wait on provider I/O."""
        from gigaforge.runtime import build_runtime

        try:
            runtime = build_runtime(repo_root=repo_root,
                                    require_provider=require_provider,
                                    probe_models=probe_models)
        except Exception as exc:  # noqa: BLE001 - the window must still open
            log.error("build_runtime failed: %s", exc)
            raise
        return cls(runtime=runtime, offline=bool(getattr(runtime, "offline", True)))

    # ── telemetry ─────────────────────────────────────────────────────────
    @property
    def store(self) -> Any:
        return self.runtime.store

    @property
    def core_config(self) -> Any:
        return self.runtime.core_config

    def snapshot(self, *, force: bool = False) -> Dict[str, Any]:
        """The shared snapshot, recomputed at most once per ``snapshot_ttl``."""
        with self._lock:
            fresh = (time.monotonic() - self._snapshot_at) < self.snapshot_ttl
            if self._snapshot is not None and fresh and not force:
                return self._snapshot
            try:
                self._snapshot = self.runtime.snapshot()
            except Exception as exc:  # noqa: BLE001 - render the failure, do not raise
                self.last_error = f"snapshot failed: {type(exc).__name__}: {exc}"
                log.warning(self.last_error)
                self._snapshot = self._snapshot or _empty_snapshot(self.last_error)
            self._snapshot_at = time.monotonic()
            return self._snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot_at = 0.0

    def runs(self, limit: int = 40) -> List[Dict[str, Any]]:
        try:
            return list(self.store.runs(limit=limit) or [])
        except Exception:  # noqa: BLE001
            return []

    # ── model work ────────────────────────────────────────────────────────
    @property
    def can_run(self) -> bool:
        return bool(getattr(self.runtime, "can_run", False))

    def run_goal(self, goal: str, **kwargs: Any) -> Any:
        result = self.runtime.run_goal(goal, **kwargs)
        self.invalidate()
        return result

    def switch_workspace(self, root: str | Path) -> Path:
        """Rebuild the filesystem-bound runtime for an explicitly selected project.

        The active client settings and model strategy are carried over. A model
        catalogue request is deliberately skipped: switching folders must not
        add a network round-trip or change the user's selected model.
        """
        target = Path(root).expanduser().resolve()
        if not target.is_dir():
            raise ValueError(f"project folder does not exist: {target}")
        with self._lock:
            current = self.runtime
            active = getattr(current, "has_active_work", False)
            if active:
                raise RuntimeError("cannot change project while an agent run is active")
            current_root = Path(str(current.core_config.repo_root or ".")).resolve()
            if target == current_root:
                return target

            from gigaforge.runtime import build_runtime

            replacement = build_runtime(
                settings=current.settings,
                core_config=current.core_config,
                require_provider=False,
                plan_strategy=current.plan_strategy,
                review_strategy=current.review_strategy,
                repo_root=target,
                probe_models=False,
            )
            if getattr(current, "can_run", False) and not replacement.can_run:
                replacement.aclose()
                raise RuntimeError(
                    "the active model provider could not be rebuilt for the selected project"
                )
            # The credentials and account are unchanged, so the existing
            # catalogue remains the right one even though filesystem services
            # (index, firewall, storage and verifier root) are project-scoped.
            replacement.catalog = current.catalog
            self.runtime = replacement
            self.offline = bool(getattr(replacement, "offline", True))
            self._snapshot = None
            self._snapshot_at = 0.0

        try:
            self.browser.stop()
        except Exception:  # noqa: BLE001 - workspace change must remain usable
            log.debug("browser panel stop failed during workspace switch", exc_info=True)
        try:
            current.aclose()
        except Exception:  # noqa: BLE001 - the new runtime is already active
            log.debug("old workspace runtime close failed", exc_info=True)
        return target

    def chat_engine(self, **kwargs: Any) -> Any:
        """A chat engine in the requested verb (``mode="chat"|"agent"``)."""
        return self.runtime.chat_engine(**kwargs)

    def index_repo(self, *, full: bool = False) -> Dict[str, Any]:
        result = (self.runtime.indexer.index_full() if full
                  else self.runtime.indexer.index_incremental())
        payload = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
        self.invalidate()
        return payload

    # ── strategies ────────────────────────────────────────────────────────
    def set_plan_strategy(self, name: str) -> str:
        return self.runtime.set_plan_strategy(name)

    def set_review_strategy(self, name: str) -> str:
        return self.runtime.set_review_strategy(name)

    def set_think_mode(self, enabled: bool) -> Dict[str, str]:
        """Turn the reasoning preset on or off.

        Think mode is a *preset*, not a second engine: a tree-search plan (K
        candidate decompositions, zero-token heuristic scoring, optional judge)
        plus the constitutional review gate.  Both stages keep obeying the same
        router, budget, breaker and trace — the properties the DAG guarantees.
        """
        applied = {
            "plan": self.set_plan_strategy("tot" if enabled else "linear"),
            "review": self.set_review_strategy("constitutional" if enabled else "none"),
        }
        self._think_mode = bool(enabled)
        return applied

    @property
    def think_mode(self) -> bool:
        return bool(getattr(self, "_think_mode", False))

    @property
    def approve_changes(self) -> bool:
        """Compatibility view: whether edits require an explicit approval."""
        return self.approval_mode == "ask"

    def set_approve_changes(self, enabled: bool) -> bool:
        self.set_approval_mode("ask" if enabled else "always_allow")
        return self.approve_changes

    @property
    def approval_mode(self) -> str:
        mode = str(getattr(self, "_approval_mode", "ask") or "ask").strip().lower()
        return mode if mode in {"ask", "always_allow"} else "ask"

    def set_approval_mode(self, mode: str) -> str:
        normalized = str(mode or "").strip().lower()
        if normalized not in {"ask", "always_allow"}:
            raise ValueError("approval mode must be 'ask' or 'always_allow'")
        from .preferences import save_preferences

        save_preferences(approval_mode=normalized)
        self._approval_mode = normalized
        return normalized

    def set_model(self, name: str) -> str:
        """Force every phase to *name* — what the composer's model picker does."""
        selected = self.runtime.set_model(name)
        self._model = selected
        return selected

    @property
    def model(self) -> str:
        return str(getattr(self, "_model", "") or
                   getattr(self.runtime.core_config, "strong_model", ""))

    # ── diagnostics ───────────────────────────────────────────────────────
    def describe(self) -> Dict[str, Any]:
        try:
            info = dict(self.runtime.describe())
        except Exception as exc:  # noqa: BLE001
            info = {"error": f"{type(exc).__name__}: {exc}"}
        info["offline"] = self.offline
        info["can_run"] = self.can_run
        # The registry's internal names are terse ("giga"); show what a human calls it.
        info["provider"] = _PROVIDER_LABELS.get(str(info.get("provider") or ""),
                                                str(info.get("provider") or "none"))
        return info

    def doctor_checks(self) -> List[Dict[str, Any]]:
        """The environment checks the CLI's ``doctor`` prints, as data.

        ``gigaforge.doctor.run_doctor`` renders a rich table and returns an exit
        code, which is the right contract for a terminal and useless for a window.
        The individual checks are pure functions returning a ``Check``, so they are
        called directly: the window renders the same battery without scraping a
        table, and a new check added to the CLI shows up here automatically once
        it is added to :data:`_DOCTOR_CHECKS`.
        """
        try:
            from gigaforge import doctor as D

            cwd = Path(self.core_config.repo_root or ".")
            checks = D.collect_checks(root=cwd, offline=True)
        except Exception as exc:  # noqa: BLE001 - diagnostics never break the window
            return [{
                "name": "doctor", "status": "FAIL",
                "detail": f"{type(exc).__name__}: {exc}", "fix": "",
            }]
        return [
            {
                "name": getattr(check, "name", "?"),
                "status": getattr(check, "status", "?"),
                "detail": getattr(check, "detail", ""),
                "fix": getattr(check, "fix", ""),
            }
            for check in checks
        ]

    def provider_rows(self) -> List[Dict[str, Any]]:
        try:
            from gigaforge.providers.registry import default_registry

            return list(default_registry().describe())
        except Exception as exc:  # noqa: BLE001
            return [{"name": "error", "error": f"{type(exc).__name__}: {exc}"}]

    def agent_rows(self) -> List[Dict[str, str]]:
        try:
            from gigaforge.agents import AGENT_CATALOG

            return list(AGENT_CATALOG)
        except Exception:  # noqa: BLE001
            return []

    def paths(self) -> List[tuple[str, str]]:
        """The file locations, elided from the left so the useful tail survives."""
        cfg = self.core_config
        rows = [
            ("repo root", str(cfg.repo_root)),
            ("core db", str(cfg.db_path)),
            ("trace", str(cfg.trace_path)),
        ]
        l0_path = getattr(self.runtime.l0_store, "db_path", "")
        if l0_path:
            rows.append(("symbol index", str(l0_path)))
        rows.append(("chat transcripts", str(Path(".gigaforge/chat"))))
        return [(label, _elide_path(value)) for label, value in rows]

    # ── browser feature ───────────────────────────────────────────────────
    def open_browser_panel(self, *, port: Optional[int] = None) -> str:
        """Start (or reuse) the shared browser control room."""
        chat_handler = None
        try:
            from gigaforge.ui.chat import build_chat_handler

            chat_handler = build_chat_handler(self.chat_engine())
        except Exception:  # noqa: BLE001 - the panel works without chat
            chat_handler = None
        try:
            from gigaforge.ui.analytics import price_table

            prices = price_table()
        except Exception:  # noqa: BLE001
            prices = None
        return self.browser.start(
            self.store,
            port=port,
            chat_handler=chat_handler,
            prices=prices,
            default_model=getattr(self.core_config, "default_model", "GigaChat"),
        )

    def close(self) -> None:
        self.browser.stop()
        try:
            self.runtime.aclose()
        except Exception:  # noqa: BLE001
            log.debug("runtime close failed", exc_info=True)


def _elide_path(value: str, *, keep: int = 2, width: int = 46) -> str:
    """``C:\\a\\b\\c\\d.db`` → ``…\\c\\d.db`` when it is too wide for the panel."""
    text = str(value or "")
    if len(text) <= width:
        return text
    parts = text.replace("/", "\\").split("\\")
    tail = "\\".join(part for part in parts[-keep:] if part)
    return f"…\\{tail}" if tail else text[-width:]


def _empty_snapshot(reason: str) -> Dict[str, Any]:
    """A renderable snapshot shape used when the real one cannot be built."""
    return {
        "snapshot_version": "fallback",
        "generated_at": "",
        "source": {"db_path": "", "trace_path": "", "db_present": False,
                   "trace_present": False, "missing": [reason]},
        "run": {"run_id": "", "goal": "", "status": "unknown", "accepted": False,
                "started_at": "", "finished_at": "", "duration_ms": 0},
        "kpis": {
            "tokens": {"total": 0, "prompt": 0, "completion": 0, "calls": 0},
            "cost": {"micros": 0, "usd": 0.0, "display": "$0.00", "by_phase_usd": {},
                     "prices": {}},
            "cache": {"entries": 0, "hits": 0, "misses": 0, "hit_rate": 0.0},
            "repair": {"proposals": 0, "rejections": 0, "reject_rate": 0.0,
                       "fixloop_runs": 0, "max_iterations": 0, "avg_iterations": 0.0},
            "latency": {"router_p50_ms": None, "router_p95_ms": None,
                        "client_p95_ms": None, "samples": 0},
            "health": {"router_errors": 0, "breaker_rejected": 0, "ratelimit_429": 0,
                       "subagent_runs": 0},
        },
        "phases": [],
        "models": [],
        "steps": [],
        "progress": {"done": 0, "total": 0, "percent": 0.0},
        "node_order": [],
        "step_status_counts": {},
        "trace": [],
        "trace_counts": {},
        "diff": {"chars": 0, "files": [], "hunks": 0, "added": 0, "removed": 0,
                 "text": "", "truncated": False},
        "errors": [reason],
        "index": {},
        "totals": {"runs_recorded": 0, "cache_entries": 0, "trace_events": 0,
                   "metrics_points": 0},
    }

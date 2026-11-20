"""Composition root — one place that knows how to assemble the whole harness.

Before this module was rebuilt, every front end had its own half-wiring: the CLI
constructed a ``GigaClient`` with no token provider, the chat backend constructed
a ``TaskPipeline`` with no client at all, and the provider registry documented a
``build_runtime()`` that could not run.  The object graph lives here now and
nothing else constructs it.

Two compositions, because the harness has two honest modes:

``build_runtime(require_provider=False)`` — **read-only**.  Telemetry, traces,
the index, the dashboards and the chat commands all work with no credentials.
This is what the desktop window opens with, so the app starts on a machine that
has never seen a GigaChat key.

``build_runtime(require_provider=True)`` — **runnable**.  Adds the async client,
the token provider, the rate limiter, the provider registry entry and the
blocking adapter the DAG calls, so ``runtime.run(goal)`` can spend tokens.

The blocking boundary is :class:`~gigaforge.core.sync_adapter.SyncChatClient`: the
DAG is synchronous, every transport is async, and this is the only bridge.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from gigaforge.client.auth import TokenProvider
from gigaforge.client.config import ClientConfig
from gigaforge.client.giga_client import GigaClient
from gigaforge.client.ratelimit import RateLimiter
from gigaforge.config import Settings, get_settings
from gigaforge.core.config import CoreConfig
from gigaforge.core.metrics import MetricsCollector
from gigaforge.core.pipeline import PipelineResult, TaskPipeline
from gigaforge.core.strategies import (
    PlanStrategy,
    ReviewStrategy,
    build_plan_strategy,
    build_review_strategy,
)
from gigaforge.core.sync_adapter import SyncChatClient
from gigaforge.firewall import ContextFirewall
from gigaforge.firewall.config import FirewallConfig
from gigaforge.l0.config import L0Config
from gigaforge.l0.indexer import Git, Indexer
from gigaforge.l0.store import L0Store
from gigaforge.providers.base import Provider
from gigaforge.providers.registry import attach_giga_client, default_registry
from gigaforge.ui.store import UiStore

log = logging.getLogger("gigaforge.runtime")

__all__ = [
    "Runtime",
    "build_runtime",
    "build_client",
    "build_provider",
    "client_config_from_settings",
    "metrics_sink",
]


# ══════════════════════════════════════════════════════════════════════════
# client layer
# ══════════════════════════════════════════════════════════════════════════
def client_config_from_settings(settings: Settings) -> ClientConfig:
    """Translate the flat :class:`Settings` into the client layer's config.

    The two dataclasses intentionally stay separate: ``Settings`` is the
    deployment surface (env vars, budgets, paths) while ``ClientConfig`` is the
    transport surface (timeouts, retries, buckets).  This function is the single
    translation point, which is what makes ``Settings`` refactors safe.
    """
    return ClientConfig(
        oauth_url=settings.auth_url,
        api_base=settings.base_url,
        scope=settings.scope,
        auth_key=settings.auth_key,
        token_ttl=1500.0,
        token_skew=float(settings.token_refresh_margin),
        tls_verify=settings.verify_tls,
        connect_timeout=settings.timeout_connect,
        read_timeout=settings.timeout_stream_idle,
        stream_timeout=settings.timeout_total,
        rpm=settings.rpm,
        tpm=settings.tpm,
    )


def metrics_sink(collector: Optional[MetricsCollector]) -> Callable[[Dict[str, Any]], None]:
    """Adapt the client's flat call record to the core metric names.

    ``GigaClient`` reports one dict per call; the UI reads
    ``client.usage.prompt_tokens``, ``client.usage.total_tokens``,
    ``client.latency_ms`` and ``client.request``.  Mapping stays here so neither
    side has to know the other's vocabulary.
    """
    if collector is None:
        return lambda record: None

    def sink(record: Dict[str, Any]) -> None:
        try:
            phase = str(record.get("phase") or "")
            model = str(record.get("model") or "")
            labels = {"phase": phase, "model": model}
            tokens_in = int(record.get("tokens_in") or 0)
            tokens_out = int(record.get("tokens_out") or 0)
            collector.incr("client.request", 1, labels)
            if tokens_in:
                collector.incr("client.usage.prompt_tokens", tokens_in, labels)
            if tokens_in or tokens_out:
                collector.incr("client.usage.total_tokens", tokens_in + tokens_out, labels)
            collector.observe("client.latency_ms", float(record.get("latency_ms") or 0.0), labels)
            if not record.get("ok", True):
                collector.incr("client.error", 1, labels)
        except Exception:  # noqa: BLE001 - telemetry must never break a call
            log.debug("metrics sink failed", exc_info=True)

    return sink


def build_provider(
    client: GigaClient,
    settings: Optional[Settings] = None,
    *,
    max_context: Optional[int] = None,
) -> Provider:
    """Return the provider the harness should talk to.

    For the built-in GigaChat path the provider is constructed **from the client
    passed here** and then registered as the ``"giga"`` instance.  That ordering
    matters: the registry caches instances, so returning ``registry.default()``
    after a credential change handed back a provider wrapping the *previous*
    (now closed) client — every call then failed with "Cannot send a request, as
    the client has been closed" right after a successful login.

    A non-GigaChat selection (``GIGAFORGE_PROVIDER=openai``) still comes from the
    registry, and an unusable selection falls back to GigaChat instead of taking
    the process down: a typo in an env var must not make the harness unrunnable.
    """
    import os

    from gigaforge.providers.gigachat import GigaChatProvider

    attach_giga_client(client)
    registry = default_registry()
    selected = (os.environ.get("GIGAFORGE_PROVIDER") or "giga").strip().lower()
    if selected in {"", "giga", "gigachat"}:
        provider = GigaChatProvider(client, max_context=max_context)
        registry.register_instance("giga", provider)
        return provider
    try:
        return registry.default()
    except Exception as exc:  # noqa: BLE001 - fall back to the built-in provider
        log.warning("selected provider unavailable (%s); using GigaChat", exc)
        provider = GigaChatProvider(client, max_context=max_context)
        registry.register_instance("giga", provider)
        return provider


def build_client(
    settings: Optional[Settings] = None,
    *,
    metrics: Optional[MetricsCollector] = None,
) -> tuple[SyncChatClient, GigaClient, TokenProvider, Provider]:
    """Build the blocking client used by the DAG, plus the objects behind it.

    Returns ``(client, raw_client, token_provider, provider)`` so the runtime can
    close the transport and report which provider is live.
    """
    s = settings or get_settings()
    config = client_config_from_settings(s)
    token_provider = TokenProvider(config)
    ratelimiter = RateLimiter(rpm=s.rpm, tpm=s.tpm)
    raw = GigaClient(
        token_provider,
        ratelimiter,
        metrics_sink=metrics_sink(metrics),
        settings=s,
    )
    provider = build_provider(
        raw,
        s,
        # 0 means "resolve per model from the measured table"; a positive
        # value pins one window for every model.
        max_context=config.max_context or None,
    )
    return SyncChatClient(provider), raw, token_provider, provider


# ══════════════════════════════════════════════════════════════════════════
# the runtime
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class Runtime:
    """Every wired component, in one object a front end can hold."""

    pipeline: TaskPipeline
    core_config: CoreConfig
    settings: Settings
    store: UiStore
    l0_store: L0Store
    firewall: ContextFirewall
    indexer: Indexer
    git: Optional[Git] = None
    metrics: Optional[MetricsCollector] = None
    memory: Any = None
    client: Optional[SyncChatClient] = None
    raw_client: Optional[GigaClient] = None
    token_provider: Optional[TokenProvider] = None
    provider: Optional[Provider] = None
    plan_strategy: Optional[PlanStrategy] = None
    review_strategy: Optional[ReviewStrategy] = None
    catalog: Any = None
    notes: list[str] = field(default_factory=list)
    _closed: bool = field(default=False, init=False, repr=False)
    _goal_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _active_goals: int = field(default=0, init=False, repr=False)
    _resources_closed: bool = field(default=False, init=False, repr=False)
    _owns_metrics: bool = field(default=True, repr=False)

    # -- capability -------------------------------------------------------
    @property
    def can_run(self) -> bool:
        """True when a model provider is wired in and a run can spend tokens."""
        return self.client is not None and self.pipeline.client is not None

    @property
    def offline(self) -> bool:
        return not self.can_run

    @property
    def has_active_work(self) -> bool:
        """Whether a direct pipeline run or an isolated long-lived goal is active."""
        with self._goal_lock:
            return self._active_goals > 0 or bool(self.pipeline.has_active_runs)

    @property
    def provider_name(self) -> str:
        """A short, human name for the live provider ("gigachat", "openai", ...)."""
        if self.provider is None:
            return "none"
        registered = ""
        try:
            from gigaforge.providers.registry import default_registry

            registered = default_registry().default_name()
        except Exception:  # noqa: BLE001 - naming must never raise
            registered = ""
        if registered:
            return registered
        raw = getattr(self.provider, "name", "") or type(self.provider).__name__
        return str(raw).replace("Provider", "").lower().replace("gigachat", "gigachat")

    # -- work -------------------------------------------------------------
    def run(self, goal: str, **kwargs: Any) -> PipelineResult:
        """Run one task end to end.  Raises a clear error when offline."""
        if not self.can_run:
            raise RuntimeError(
                "no model provider is configured: set GIGAFORGE_AUTH_KEY "
                "(base64 client_id:client_secret) or GIGAFORGE_PROVIDER=openai with "
                "GIGAFORGE_OPENAI_BASE_URL / GIGAFORGE_OPENAI_API_KEY"
            )
        return self.pipeline.run(goal, **kwargs)

    def run_goal(self, goal: str = "", **kwargs: Any) -> Any:
        """Run, verify and safely publish a resumable isolated project goal."""
        with self._goal_lock:
            if self._closed:
                raise RuntimeError("runtime is closing and cannot start a goal")
            self._active_goals += 1
        try:
            return self._run_isolated_goal(goal, **kwargs)
        finally:
            with self._goal_lock:
                self._active_goals = max(0, self._active_goals - 1)
                close_resources = self._closed and self._active_goals == 0
            if close_resources:
                self.pipeline.when_idle(self._close_shared_resources)

    def _run_isolated_goal(self, goal: str = "", **kwargs: Any) -> Any:
        """Implementation behind the lifecycle guard in :meth:`run_goal`."""
        if not self.can_run:
            raise RuntimeError(
                "no model provider is configured: set GIGAFORGE_AUTH_KEY "
                "or configure an OpenAI-compatible provider"
            )
        from gigaforge.core.goal import GoalRunner, GoalStore
        from gigaforge.core.goal_workspace import GoalWorkspace

        verifier = kwargs.pop("verifier", None)
        destination = Path(self.core_config.repo_root).resolve()
        resume = bool(kwargs.get("resume", False))
        goal_id = str(kwargs.get("goal_id") or "").strip()
        if resume and not goal_id:
            raise ValueError("goal_id is required when resuming a goal")
        if not resume and not str(goal or "").strip():
            raise ValueError("goal cannot be empty")
        if not goal_id:
            goal_id = GoalStore.new_id()
            kwargs["goal_id"] = goal_id

        goal_store = GoalStore(destination)
        if resume:
            saved = goal_store.load(goal_id)
            if saved.get("status") == "completed":
                return GoalRunner(
                    self.pipeline,
                    repo_root=destination,
                    store=goal_store,
                ).run(goal_id=goal_id, resume=True)
        else:
            try:
                goal_store.load(goal_id)
            except ValueError as exc:
                if "does not exist" not in str(exc):
                    raise
            else:
                raise ValueError(f"goal {goal_id!r} already exists; use resume")

        workspace = GoalWorkspace(destination, goal_id)
        workspace_root = workspace.prepare(resume=resume)
        isolated_runtime = build_runtime(
            settings=self.settings,
            core_config=self.core_config,
            require_provider=False,
            plan_strategy=self.plan_strategy,
            review_strategy=self.review_strategy,
            metrics=self.metrics,
            repo_root=workspace_root,
            probe_models=False,
            isolate_storage=True,
        )
        if not isolated_runtime.can_run:
            isolated_runtime.aclose()
            raise RuntimeError("the isolated goal runtime could not reuse the configured model provider")
        try:
            engine = str(kwargs.pop("engine", "") or "").strip().lower()
            if not engine:
                engine = "langgraph" if _langgraph_goal_enabled() else "classic"
            if engine == "langgraph":
                from gigaforge.core.langgraph_goal import run_goal_graph

                return run_goal_graph(
                    isolated_runtime.pipeline,
                    repo_root=workspace_root,
                    goal=goal,
                    verifier=verifier,
                    store=goal_store,
                    workspace=workspace,
                    emit=_trace_emitter(isolated_runtime.pipeline),
                    **kwargs,
                )
            return GoalRunner(
                isolated_runtime.pipeline,
                repo_root=workspace_root,
                verifier=verifier,
                store=goal_store,
                workspace=workspace,
            ).run(goal, **kwargs)
        finally:
            isolated_runtime.aclose()

    def chat_engine(self, **kwargs: Any) -> Any:
        """An engine bound to this runtime, in either verb.

        ``mode="chat"`` answers a message; ``mode="agent"`` performs one
        implementation cycle; ``mode="goal"`` executes, verifies and improves
        across bounded cycles. All modes share this runtime's client and store.
        """
        from gigaforge.ui.chat import default_engine, direct_engine

        mode = str(kwargs.pop("mode", "chat") or "chat").strip().lower()
        if mode in {"agent", "task", "run", "goal"}:
            if mode == "goal":
                kwargs.setdefault("goal_mode", True)
            kwargs.setdefault("offline", self.offline)
            kwargs.setdefault("pipeline_factory", lambda: self.pipeline)
            kwargs.setdefault(
                "goal_runner_factory",
                lambda goal, **goal_kwargs: self.run_goal(goal, **goal_kwargs),
            )
            return default_engine(self.store, config=self.core_config, **kwargs)

        model = str(kwargs.pop("model", "") or self._selected_model())
        return direct_engine(
            self.store,
            client=self.client,
            model=model,
            default_model=getattr(self.core_config, "default_model", "GigaChat"),
            **kwargs,
        )

    def _selected_model(self) -> str:
        """The model the window picked, or the strongest one the account serves."""
        chosen = str(getattr(self, "_model", "") or "").strip()
        if chosen:
            return chosen
        roles = getattr(getattr(self, "catalog", None), "roles", None)
        if roles is not None:
            return str(getattr(roles, "strong", "") or getattr(roles, "default", "") or "")
        return str(getattr(self.core_config, "strong_model", "") or "")

    def update_core_config(self, config: CoreConfig) -> CoreConfig:
        """Apply an immutable config through the pipeline lifecycle boundary."""
        self.pipeline.reconfigure(config=config)
        self.core_config = config
        return config

    def set_plan_strategy(self, name: str) -> str:
        strategy = build_plan_strategy(name)
        self.pipeline.reconfigure(plan_strategy=strategy)
        self.plan_strategy = strategy
        return strategy.name

    def set_review_strategy(self, name: str) -> str:
        strategy = build_review_strategy(name)
        self.pipeline.reconfigure(review_strategy=strategy)
        self.review_strategy = strategy
        return strategy.name

    def cancel(self, run_id: str, reason: str = "cancelled by caller") -> bool:
        """Cancel an active pipeline run without reaching into executor internals."""
        return self.pipeline.cancel(run_id, reason)

    def set_model(self, name: str) -> str:
        """Pin every phase and every chat turn to *name*."""
        selected = (name or "").strip()
        if not selected:
            raise ValueError("empty model name")
        self.update_core_config(self.core_config.with_model(selected))
        self._model = selected
        return selected

    # ── credentials, edited from the window ──────────────────────────────
    def apply_credentials(
        self,
        *,
        auth_key: str,
        base_url: str = "",
        oauth_url: str = "",
        scope: str = "",
        verify_tls: Optional[bool] = None,
        probe: bool = True,
    ) -> Dict[str, Any]:
        """Replace the live credential without restarting the application.

        The window accepts a key, applies it to the process environment, rebuilds
        the client layer, and swaps it into the running pipeline. The settings
        boundary persists the returned values to `.env`; the runtime stays
        independent of UI-specific file handling.
        """
        import os

        from gigaforge.config import get_settings, reset_settings_cache
        from gigaforge.core.models import apply_roles, fetch_models
        from gigaforge.env import looks_like_auth_key, normalise_auth_key

        key = normalise_auth_key(auth_key)
        if not key:
            raise ValueError("an empty credential cannot be applied")
        shape_ok = looks_like_auth_key(key)

        os.environ["GIGAFORGE_AUTH_KEY"] = key
        updates: Dict[str, str] = {"GIGAFORGE_AUTH_KEY": key}
        if base_url.strip():
            os.environ["GIGAFORGE_BASE_URL"] = base_url.strip()
            updates["GIGAFORGE_BASE_URL"] = base_url.strip()
        if oauth_url.strip():
            os.environ["GIGAFORGE_AUTH_URL"] = oauth_url.strip()
            updates["GIGAFORGE_AUTH_URL"] = oauth_url.strip()
        if scope.strip():
            os.environ["GIGAFORGE_SCOPE"] = scope.strip()
            updates["GIGAFORGE_SCOPE"] = scope.strip()
        if verify_tls is not None:
            if verify_tls:
                os.environ.pop("GIGAFORGE_TLS_VERIFY_OFF", None)
                updates["GIGAFORGE_TLS_VERIFY_OFF"] = "0"
            else:
                os.environ["GIGAFORGE_TLS_VERIFY_OFF"] = "1"
                updates["GIGAFORGE_TLS_VERIFY_OFF"] = "1"

        reset_settings_cache()
        settings = get_settings()
        client, raw, token_provider, provider = build_client(settings, metrics=self.metrics)

        previous = self.client
        self.settings = settings
        self.client = client
        self.raw_client = raw
        self.token_provider = token_provider
        self.provider = provider
        self.pipeline.reconfigure(client=client)
        if previous is not None and previous is not client:
            self.pipeline.when_idle(lambda: self._close_client(previous))

        models: list[str] = []
        warning = ""
        if probe:
            try:
                catalog = fetch_models(
                    client, path=Path(self.core_config.db_path).parent / "models.json",
                    force=True,
                )
                models = list(catalog.models)
                self.catalog = catalog
                if catalog.available:
                    self.update_core_config(apply_roles(self.core_config, catalog.roles))
            except Exception as exc:  # noqa: BLE001 - the key may still be valid
                warning = f"{type(exc).__name__}: {exc}"

        return {
            "ok": bool(models) or not warning,
            "shape_ok": shape_ok,
            "models": models,
            "warning": warning,
            "applied": updates,
        }

    def snapshot(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        from gigaforge.ui.analytics import price_table
        from gigaforge.ui.snapshot import build_snapshot

        return build_snapshot(
            self.store,
            run_id=run_id,
            prices=price_table(),
            default_model=self.core_config.default_model,
        )

    def describe(self) -> Dict[str, Any]:
        """A JSON-safe description of the wiring — used by ``doctor`` and the app."""
        return {
            "provider": self.provider_name,
            "provider_class": type(self.provider).__name__ if self.provider else "none",
            "offline": self.offline,
            "plan_strategy": getattr(self.plan_strategy, "name", None),
            "review_strategy": getattr(self.review_strategy, "name", None),
            "db_path": self.core_config.db_path,
            "trace_path": self.core_config.trace_path,
            "l0_db_path": getattr(self.l0_store, "db_path", None),
            "repo_root": self.core_config.repo_root,
            "models": {
                "default": self.core_config.default_model,
                "cheap": self.core_config.cheap_model,
                "strong": self.core_config.strong_model,
            },
            "auth_configured": bool(self.settings.auth_key),
            "models_live": list(getattr(self.catalog, "models", []) or []),
            "model_roles": (self.catalog.roles.to_dict()
                            if self.catalog is not None else {}),
            "notes": list(self.notes),
        }

    # -- lifecycle --------------------------------------------------------
    def aclose(self) -> None:
        with self._goal_lock:
            if self._closed:
                return
            self._closed = True
            active_goals = self._active_goals
        self.pipeline.close()
        if not active_goals:
            self.pipeline.when_idle(self._close_shared_resources)

    def _close_shared_resources(self) -> None:
        with self._goal_lock:
            if self._resources_closed or self._active_goals:
                return
            self._resources_closed = True
        resources = [
            getattr(self.client, "aclose", None),
            getattr(self.l0_store, "close", None),
            getattr(self.memory, "close", None),
            getattr(self.store, "close", None),
        ]
        if self._owns_metrics:
            resources.append(getattr(self.metrics, "close", None))
        for closer in resources:
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - closing is best effort
                    log.debug("close() failed during Runtime.aclose", exc_info=True)

    @staticmethod
    def _close_client(client: Any) -> None:
        closer = getattr(client, "aclose", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - replaced transports are disposable
                log.debug("close() failed for replaced client", exc_info=True)

    close = aclose

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_: Any) -> None:
        self.aclose()


def _langgraph_goal_enabled() -> bool:
    """Whether goal mode should run on the LangGraph engine.

    Default is on when the package is installed; ``GIGAFORGE_GOAL_ENGINE=classic``
    pins the hand-rolled loop (useful for bisecting a regression), and
    ``=langgraph`` forces the graph even if detection is ambiguous.
    """
    import os as _os

    choice = (_os.environ.get("GIGAFORGE_GOAL_ENGINE") or "").strip().lower()
    if choice in {"classic", "legacy", "loop"}:
        return False
    if choice in {"langgraph", "graph"}:
        return True
    try:
        from gigaforge.core.langgraph_runtime import langgraph_available

        return langgraph_available()
    except Exception:  # noqa: BLE001 - a missing optional dep means classic
        return False


def _trace_emitter(pipeline: Any) -> Any:
    """Bridge graph events into the shared JSONL trace.

    The graph streams to the UI directly; this keeps the on-disk trace complete
    so ``gigaforge report`` and the panel see the same history as before.
    """

    def emit(event: str, payload: dict[str, Any]) -> None:
        trace = getattr(getattr(pipeline, "metrics", None), "trace", None)
        fn = getattr(trace, "event", None)
        if fn is None:
            return
        body = {key: value for key, value in payload.items() if key != "event"}
        fn(event, **body)

    return emit


def build_runtime(
    *,
    settings: Optional[Settings] = None,
    core_config: Optional[CoreConfig] = None,
    require_provider: bool = False,
    plan_strategy: Optional[PlanStrategy] = None,
    review_strategy: Optional[ReviewStrategy] = None,
    repo_root: Optional[str | Path] = None,
    metrics: Optional[MetricsCollector] = None,
    probe_models: bool = True,
    isolate_storage: bool = False,
) -> Runtime:
    """Assemble the runtime.  Never touches the network; may build offline.

    ``require_provider=True`` turns a missing credential into a
    :class:`RuntimeError` with the setup instruction.  The default is
    ``False`` so read-only front ends (the desktop window, ``gigaforge report``,
    ``gigaforge web``) start on a machine with no key.
    """
    s = settings or get_settings()
    cfg = core_config or CoreConfig.from_env()
    notes: list[str] = []

    if repo_root is not None:
        # ``CoreConfig`` is frozen: rebind rather than mutate, so a caller's config
        # object is never changed behind its back.  Relative sink paths are
        # re-anchored to the repository, otherwise ``--repo X`` would write X's
        # telemetry into whatever directory happened to be current.
        import dataclasses
        import os as _os

        root_path = Path(repo_root).resolve()
        overrides: Dict[str, Any] = {"repo_root": str(root_path)}
        if isolate_storage or not _os.environ.get("GIGAFORGE_DB_PATH"):
            overrides["db_path"] = str(root_path / ".gigaforge" / "core.sqlite3")
        if isolate_storage or not _os.environ.get("GIGAFORGE_TRACE_DIR"):
            overrides["trace_path"] = str(root_path / ".gigaforge" / "traces" / "core.jsonl")
        if isolate_storage:
            overrides["subagents"] = dataclasses.replace(
                cfg.subagents,
                worktree_root=str(root_path / ".gigaforge" / "worktrees"),
            )
        cfg = dataclasses.replace(cfg, **overrides)
    else:
        # Always absolute.  A relative root (".", or a value from .env) is resolved
        # against the *process* directory when it is first read and against a
        # different one later; the applier then writes into a nested copy of the
        # repository (`_site_demo/_site_demo/`), which looks exactly like "the agent
        # did nothing".
        import dataclasses

        cfg = dataclasses.replace(cfg, repo_root=str(Path(cfg.repo_root or ".").resolve()))
    root = Path(cfg.repo_root or ".")

    # ---- storage --------------------------------------------------------
    for path in (cfg.db_path, cfg.trace_path):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - read-only FS
            notes.append(f"could not create the parent directory of {path}")

    l0_overrides: Dict[str, Any] = {"repo_root": str(root)}
    import os as _os

    configured_l0 = "" if isolate_storage else (
        _os.environ.get("GF_L0__DB_PATH") or _os.environ.get("GIGAFORGE_L0_DB") or ""
    ).strip()
    if not configured_l0:
        # Keep the symbol index next to the repo it describes instead of next to
        # the process's working directory: `gigaforge run --repo X` must not
        # write X's symbols into whatever directory happened to be current.
        l0_overrides["db_path"] = str(root / ".gigaforge" / "l0.sqlite3")
    elif not Path(configured_l0).is_absolute():
        # A relative index path in `.env` must be repo-relative too.  Left as-is it
        # pointed every repository at the *same* index file, so a file's stored hash
        # belonged to another checkout and `index_incremental` reported "unchanged"
        # — the model then received no context and rewrote whole files.
        l0_overrides["db_path"] = str(root / configured_l0)
    l0_config = L0Config.from_env(**l0_overrides)
    l0_store = L0Store(config=l0_config)
    git: Optional[Git] = None
    try:
        candidate = Git(root, config=l0_config)
        git = candidate if candidate.is_repo() else None
    except Exception as exc:  # noqa: BLE001 - a non-repo checkout is a valid mode
        notes.append(f"git unavailable: {type(exc).__name__}: {exc}")
    if git is None:
        notes.append("not a git repository: commit tracking and worktrees are off")
    indexer = Indexer(l0_store, config=l0_config, git=git)

    # ---- telemetry ------------------------------------------------------
    collector = metrics
    if collector is None:
        try:
            collector = MetricsCollector(cfg, db_path=cfg.db_path)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"metrics disabled: {type(exc).__name__}: {exc}")
            collector = None

    # ---- firewall (slicing + validation) --------------------------------
    # ``repo_root`` matters: the validator reads the files it is about to patch
    # and enforces path safety against this root.  Leaving it at the default
    # (".") made every run validate against the *process* directory instead of
    # the repository it was pointed at, which surfaced as "the diff did not
    # apply cleanly" for perfectly good diffs.
    firewall_config = FirewallConfig(
        repo_root=str(root),
        tenant=cfg.tenant,
        log_level=cfg.log_level,
        # Repair what a formatter can repair (unused imports, spacing, line breaks)
        # before judging a change: otherwise a correct implementation is rejected
        # over style and the run pays for another full model attempt.
        auto_fix=True,
    )
    try:
        firewall = ContextFirewall(l0_store, config=firewall_config)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"firewall unavailable: {type(exc).__name__}: {exc}")
        firewall = ContextFirewall(None, config=firewall_config)  # type: ignore[arg-type]

    # ---- model client ---------------------------------------------------
    client: Optional[SyncChatClient] = None
    raw: Optional[GigaClient] = None
    token_provider: Optional[TokenProvider] = None
    provider: Optional[Provider] = None
    if require_provider or s.auth_key or _provider_env_configured():
        try:
            client, raw, token_provider, provider = build_client(s, metrics=collector)
        except Exception as exc:  # noqa: BLE001
            message = f"no model provider available: {type(exc).__name__}: {exc}"
            if require_provider:
                raise RuntimeError(message) from exc
            notes.append(message)

    # ---- model catalog --------------------------------------------------
    # Ask the endpoint which models exist and route to those.  A hardcoded
    # catalogue is a bet that the provider never renames anything; the live run
    # that failed with {"status":404,"message":"No such model"} lost that bet.
    catalog = None
    if client is not None and probe_models:
        try:
            from gigaforge.core.models import (
                apply_roles,
                env_skip_probe,
                fetch_models,
            )

            if env_skip_probe():
                notes.append("model probe skipped (GIGAFORGE_SKIP_MODEL_PROBE)")
            else:
                catalog = fetch_models(client, path=Path(cfg.db_path).parent / "models.json")
                if catalog.available:
                    cfg = apply_roles(cfg, catalog.roles)
                    missing = catalog.missing(
                        [cfg.cheap_model, cfg.strong_model, cfg.default_model]
                    )
                    if missing:
                        notes.append(
                            "configured model(s) not served by this account: "
                            + ", ".join(missing) + " — routed to the closest available"
                        )
                else:
                    notes.append(
                        "could not read the model catalogue; using configured names"
                    )
        except Exception as exc:  # noqa: BLE001 - never block a run on the probe
            notes.append(f"model probe failed: {type(exc).__name__}: {exc}")
            catalog = None
    elif client is not None:
        notes.append("model probe skipped (reusing the current model selection)")

    # ---- strategies -----------------------------------------------------
    plan = plan_strategy or build_plan_strategy()
    review = review_strategy or build_review_strategy()

    # ---- the pipeline ---------------------------------------------------
    # Memory and context trimming are part of the execution contour: recall runs
    # inside ``intake`` (free context), trimming runs inside ``act`` (fewer
    # tokens per call).  Both are optional so a stripped build still works.
    memory = None
    try:
        from gigaforge.core.memory import EpisodicMemory

        memory = EpisodicMemory(db_path=cfg.db_path, settings=s)
    except Exception as exc:  # noqa: BLE001 - memory is an optimisation
        log.warning("episodic memory unavailable: %s", exc)
    optimizer = None
    try:
        from gigaforge.core.optimize import ContextOptimizer

        optimizer = ContextOptimizer()
    except Exception as exc:  # noqa: BLE001
        log.warning("context optimizer unavailable: %s", exc)

    # ---- acceptance gate: the project's own tests -----------------------
    class _Tests:
        """Runs the repository's pytest suite as an acceptance criterion."""

        def __init__(self, root: Path) -> None:
            self.root = root

        def __call__(self) -> Any:
            import subprocess
            import sys

            class _Outcome:
                def __init__(self, passed: bool, summary: str) -> None:
                    self.passed = passed
                    self.summary = summary

            try:
                proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no"],
                                      cwd=str(self.root), capture_output=True,
                                      text=True, timeout=600)
            except Exception as exc:  # noqa: BLE001 - no runner is not a failure
                return _Outcome(True, f"tests not run: {exc}")
            text = (proc.stdout or "") + (proc.stderr or "")
            # 5 = nothing collected: a repo without tests must not fail the gate.
            if proc.returncode == 5:
                return _Outcome(True, "no tests collected")
            lines = [line for line in text.strip().splitlines() if line.strip()]
            return _Outcome(proc.returncode == 0, "\n".join(lines[-12:]))

    test_runner = _Tests(Path(str(cfg.repo_root))) if Path(str(cfg.repo_root)).is_dir() else None

    pipeline = TaskPipeline(
        config=cfg,
        client=client,
        firewall=firewall,
        indexer=indexer,
        git=git,
        metrics=collector,
        plan_strategy=plan,
        review_strategy=review,
        memory=memory,
        optimizer=optimizer,
        test_runner=test_runner,
    )

    store = UiStore.from_config(cfg)
    return Runtime(
        pipeline=pipeline,
        core_config=cfg,
        settings=s,
        store=store,
        l0_store=l0_store,
        firewall=firewall,
        indexer=indexer,
        git=git,
        metrics=collector,
        memory=memory,
        client=client,
        raw_client=raw,
        token_provider=token_provider,
        provider=provider,
        plan_strategy=plan,
        review_strategy=review,
        catalog=catalog,
        notes=notes,
        _owns_metrics=metrics is None,
    )


def _provider_env_configured() -> bool:
    """True when a non-GigaChat provider has enough env to be constructed."""
    import os

    name = (os.environ.get("GIGAFORGE_PROVIDER") or "").strip().lower()
    if not name or name in {"giga", "gigachat"}:
        return False
    if name == "openai":
        return bool(os.environ.get("GIGAFORGE_OPENAI_API_KEY")) and bool(
            os.environ.get("GIGAFORGE_OPENAI_BASE_URL")
        )
    return True

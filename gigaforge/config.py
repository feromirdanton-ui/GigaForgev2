"""Central configuration. All URLs / keys / limits / models come from env. No hardcoding in code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(key: str, default: str) -> str:
    v = os.environ.get(key)
    return v if v is not None else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # OAuth
    auth_url: str
    scope: str

    # Base URL
    base_url: str

    # TLS
    ca_cert: str  # path to CA bundle; empty => system store
    verify_tls: bool

    # Rate limits
    rpm: int
    tpm: int

    # Timeouts
    timeout_connect: float
    timeout_stream_idle: float
    timeout_total: float

    # Token refresh margin (seconds before exp)
    token_refresh_margin: int

    # Budget caps (tokens). Hard stops.
    budget_plan_in: int
    budget_plan_out: int
    budget_code_in: int
    budget_code_out: int
    budget_fixloop_in: int
    budget_fixloop_out: int
    budget_fixloop_iters: int
    budget_task_cap: int

    # Storage
    db_path: Path
    trace_dir: Path
    sandbox_cwd: Path

    # Sandbox (default so manual Settings() construction in tests stays stable)
    sandbox_timeout_s: int = 120

    # A ReAct episode is a multi-step loop, not one planning call.  It used to
    # spend the ``plan`` phase budget (2 000 input tokens), which the harness
    # prompt alone consumes more than half of — so a live episode died on its
    # second step with BudgetExceeded and produced an empty plan.  It gets its
    # own, larger cap.
    budget_react_in: int = 40_000
    budget_react_out: int = 8_000

    # Model routing (names per GigaChat model catalog)
    models: dict[str, str] = field(default_factory=dict)

    # OAuth credential — auth_key is OPTIONAL here: auth is validated at the
    # TokenProvider/GigaClient boundary, not at config load time. Local
    # commands (gigaforge index, gigaforge report, L0Index construction,
    # MetricsCollector construction) MUST work without GIGAFORGE_AUTH_KEY.
    # Placed last because Python dataclasses require all default-valued
    # fields to come AFTER any required-positional field.
    auth_key: str = ""  # base64(client_id:client_secret) — SECRET, never log

    @property
    def default_model(self) -> str:
        """The model a transport uses when the caller names none.

        ``_build_sdk_provider`` read ``settings.default_model`` and the field
        never existed, so every attempt to construct the official-SDK provider
        raised ``AttributeError``, was swallowed by the ``except`` there, and
        the process silently fell back to the legacy transport.  The whole SDK
        path was therefore dead code in production while its unit tests passed
        (they built the provider directly, never through the runtime).
        """
        return self.models.get("code") or self.models.get("explain") or "GigaChat-2-Pro"

    @property
    def verify(self) -> bool | str:
        """Value to pass to httpx `verify`."""
        if self.ca_cert:
            return self.ca_cert
        return self.verify_tls

    @property
    def auth_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Authorization": f"Basic {self.auth_key}",
            "RqUID": "4ef09ce6-35d4-4307-8292-7655c233d801",
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # CRIT-01: NO hardcoded credential in source. GIGAFORGE_AUTH_KEY is NOT
    # required at config-load time — pure-local commands (index, report, L0
    # indexing, metrics reads, traces) must work without it. Auth is enforced
    # at the boundary that actually needs it: see
    # `gigaforge.client.auth.TokenProvider` / `_require_auth_key()`. If you
    # construct a Settings without setting GIGAFORGE_AUTH_KEY, every method
    # that talks to the GigaChat API will raise a clear ``RuntimeError`` on
    # first use.
    auth_key = _env("GIGAFORGE_AUTH_KEY", "")
    s = Settings(
        auth_url=_env("GIGAFORGE_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"),
        scope=_env("GIGAFORGE_SCOPE", "GIGACHAT_API_PERS"),
        auth_key=auth_key,
        base_url=_env("GIGAFORGE_BASE_URL", "https://api.giga.chat/v1"),
        ca_cert=_env("GIGAFORGE_CA_CERT", ""),
        # GigaChat's current certificate chain is unreliable in this deployment.
        # Operators can explicitly enable verification from the Settings dialog.
        verify_tls=not _env_bool("GIGAFORGE_TLS_VERIFY_OFF", True),
        rpm=_env_int("GIGAFORGE_RPM", 200),
        tpm=_env_int("GIGAFORGE_TPM", 500_000),
        timeout_connect=_env_float("GIGAFORGE_TIMEOUT_CONNECT", 10.0),
        timeout_stream_idle=_env_float("GIGAFORGE_TIMEOUT_STREAM_IDLE", 60.0),
        timeout_total=_env_float("GIGAFORGE_TIMEOUT_TOTAL", 300.0),
        token_refresh_margin=_env_int("GIGAFORGE_TOKEN_REFRESH_MARGIN", 60),
        budget_plan_in=_env_int("GIGAFORGE_BUDGET_PLAN_IN", 2000),
        budget_plan_out=_env_int("GIGAFORGE_BUDGET_PLAN_OUT", 1000),
        budget_code_in=_env_int("GIGAFORGE_BUDGET_CODE_IN", 8000),
        budget_code_out=_env_int("GIGAFORGE_BUDGET_CODE_OUT", 2000),
        budget_fixloop_in=_env_int("GIGAFORGE_BUDGET_FIXLOOP_IN", 2000),
        budget_fixloop_out=_env_int("GIGAFORGE_BUDGET_FIXLOOP_OUT", 1000),
        budget_fixloop_iters=_env_int("GIGAFORGE_BUDGET_FIXLOOP_ITERS", 3),
        budget_task_cap=_env_int("GIGAFORGE_BUDGET_TASK_CAP", 60_000),
        budget_react_in=_env_int("GIGAFORGE_BUDGET_REACT_IN", 40_000),
        budget_react_out=_env_int("GIGAFORGE_BUDGET_REACT_OUT", 8_000),
        sandbox_timeout_s=_env_int("GIGAFORGE_SANDBOX_TIMEOUT_S", 120),
        db_path=Path(_env("GIGAFORGE_DB_PATH", ".gigaforge/gigaforge.db")),
        trace_dir=Path(_env("GIGAFORGE_TRACE_DIR", ".gigaforge/traces")),
        sandbox_cwd=Path(_env("GIGAFORGE_SANDBOX_CWD", ".")),
        models={
            # L1 (cheap/fast) — GigaChat-3-Lightning is the "Lite" equivalent in the
            # live catalog (GigaChat-2-Lite is not published). Each role is env-overridable.
            "plan": _env("GIGAFORGE_MODEL_PLAN", "GigaChat-3-Lightning"),
            "review": _env("GIGAFORGE_MODEL_REVIEW", "GigaChat-3-Lightning"),
            "classify": _env("GIGAFORGE_MODEL_CLASSIFY", "GigaChat-3-Lightning"),
            "summarize": _env("GIGAFORGE_MODEL_SUMMARIZE", "GigaChat-3-Lightning"),
            # L2 (main) — Pro for code generation.
            "code": _env("GIGAFORGE_MODEL_CODE", "GigaChat-2-Pro"),
            "explain": _env("GIGAFORGE_MODEL_EXPLAIN", "GigaChat-2"),
            # Max only by explicit flag (env GIGAFORGE_ALLOW_MAX=1).
            "hard_reasoning": _env("GIGAFORGE_MODEL_HARD_REASONING", "GigaChat-2-Max"),
        },
    )
    return s


def model_for(task_type: str) -> str:
    s = get_settings()
    return s.models.get(task_type, s.models["explain"])


def require_auth_key() -> str:
    """Return the configured auth_key or raise with a clear setup message.

    This is the ONE place that enforces CRIT-01 at the OAuth/API boundary.
    Pure-local components (L0Index, MetricsCollector, Tracer, CacheManager,
    the CLI ``index`` and ``report`` commands, plus unit tests) MUST NOT call
    this — they operate entirely on local SQLite / JSONL files and don't need
    GigaChat credentials. Calling this is reserved for components that are
    about to make a network call to the GigaChat API.

    Raises ``RuntimeError`` with a precise message when the credential is
    missing. The error text matches what the historic contract produced at
    ``get_settings()`` time so existing test assertions stay valid.
    """
    s = get_settings()
    if not s.auth_key:
        raise RuntimeError(
            "GIGAFORGE_AUTH_KEY is required (base64(client_id:client_secret)). "
            "Set it in the environment or a .env file. Never commit real credentials."
        )
    return s.auth_key


def reset_settings_cache() -> None:
    """For tests that mutate env then re-read settings."""
    get_settings.cache_clear()

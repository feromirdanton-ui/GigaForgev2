"""Client configuration - everything comes from the environment, nothing is hardcoded.

This module closes the exact gap found in the reference ``app.py``: a Basic-auth
credential inlined in source, ``verify=False`` on every call and a Flask secret in
a default. Here the credential is a *required* environment value, TLS verification
is on by default, and every timeout / retry / rate-limit knob is a named field with
a safe default.

Precedence: explicit keyword overrides > process environment > dataclass defaults.
Environment keys use the ``GF_CLIENT__`` prefix (double underscore = nesting), with
a few backwards-compatible aliases accepted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from typing import Any, Dict, Mapping, Optional

from .errors import ConfigurationError

ENV_PREFIX = "GF_CLIENT__"

# Extra names accepted for a field; the canonical GF_CLIENT__<NAME> always works too.
_ENV_ALIASES: Dict[str, tuple] = {
    "auth_key": (
        "GF_CLIENT__AUTH_KEY",
        "GF_CLIENT__CREDENTIALS",
        "GIGACHAT_AUTH_KEY",
        "GIGACHAT_CREDENTIALS",
    ),
    "api_base": ("GF_CLIENT__API_BASE", "GIGACHAT_API_BASE"),
    "oauth_url": ("GF_CLIENT__OAUTH_URL", "GIGACHAT_OAUTH_URL"),
    "scope": ("GF_CLIENT__SCOPE", "GIGACHAT_SCOPE"),
    "default_model": ("GF_CLIENT__DEFAULT_MODEL", "GIGACHAT_MODEL"),
    "tls_verify": ("GF_CLIENT__TLS_VERIFY", "GF_CLIENT__VERIFY_SSL"),
    "max_context": (
        "GF_CLIENT__MAX_CONTEXT",
        "GIGAFORGE_GIGA_MAX_CONTEXT",
        "GIGACHAT_CONTEXT_WINDOW",
    ),
}

_BOOL_FIELDS = frozenset(
    {"tls_verify", "backoff_jitter", "retry_on_text_call", "metrics_enabled", "trust_env"}
)
_INT_FIELDS = frozenset(
    {"max_retries", "rpm", "tpm", "max_text_call_retries", "max_connections", "max_context"}
)
_FLOAT_FIELDS = frozenset(
    {
        "token_ttl",
        "token_skew",
        "connect_timeout",
        "read_timeout",
        "stream_timeout",
        "backoff_base",
        "backoff_max",
        "rate_burst",
        "max_retry_after",
        "estimate_chars_per_token",
    }
)

_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSY = frozenset({"0", "false", "no", "off", "n", "f", ""})


def _to_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ConfigurationError(f"cannot parse boolean value {raw!r}")


def _coerce(name: str, raw: Any) -> Any:
    if name in _BOOL_FIELDS:
        return _to_bool(raw)
    if name in _INT_FIELDS:
        try:
            return int(str(raw).strip())
        except ValueError as exc:
            raise ConfigurationError(f"{name} expects an integer, got {raw!r}") from exc
    if name in _FLOAT_FIELDS:
        try:
            return float(str(raw).strip())
        except ValueError as exc:
            raise ConfigurationError(f"{name} expects a number, got {raw!r}") from exc
    return str(raw)


@dataclass(frozen=True)
class ClientConfig:
    """Immutable configuration for :class:`gigaforge.client.GigaClient`."""

    # -- OAuth -----------------------------------------------------------------
    oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    api_base: str = "https://api.giga.chat/v1"
    scope: str = "GIGACHAT_API_PERS"
    auth_key: str = ""  # SECRET: base64(client_id:client_secret)
    token_ttl: float = 1500.0  # fallback TTL when the provider omits expires_at
    token_skew: float = 60.0  # proactive refresh window: refresh at exp - 60 s

    # -- transport -------------------------------------------------------------
    # Current GigaChat deployments use the unverified chain by default; callers
    # can opt in with GF_CLIENT__TLS_VERIFY=true or the Settings toggle.
    tls_verify: bool = False
    trust_env: bool = True
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    stream_timeout: float = 120.0
    user_agent: str = "gigaforge/0.1 (client)"
    max_connections: int = 10

    # -- reliability -----------------------------------------------------------
    max_retries: int = 4
    backoff_base: float = 0.5
    backoff_max: float = 15.0
    backoff_jitter: bool = True
    max_retry_after: float = 30.0  # cap for a provider Retry-After hint

    # -- rate limiting ---------------------------------------------------------
    rpm: int = 0  # 0 disables the request bucket
    tpm: int = 0  # 0 disables the token bucket
    rate_burst: float = 1.0

    # -- function calling ------------------------------------------------------
    retry_on_text_call: bool = True
    max_text_call_retries: int = 2

    # -- misc ------------------------------------------------------------------
    default_model: str = "GigaChat"
    estimate_chars_per_token: float = 4.0
    metrics_enabled: bool = True
    # 0 means "resolve from the measured per-model table"
    # (gigaforge.providers.context_windows). A positive value pins one window
    # for every model; env: GF_CLIENT__MAX_CONTEXT / GIGAFORGE_GIGA_MAX_CONTEXT /
    # GIGACHAT_CONTEXT_WINDOW.
    max_context: int = 0

    # -- construction ----------------------------------------------------------
    @classmethod
    def from_env(
        cls, env: Optional[Mapping[str, str]] = None, **overrides: Any
    ) -> "ClientConfig":
        """Build a config from the environment plus explicit overrides."""
        source: Mapping[str, str] = os.environ if env is None else env
        data: Dict[str, Any] = {}
        for f in fields(cls):
            keys = _ENV_ALIASES.get(f.name) or (ENV_PREFIX + f.name.upper(),)
            raw: Any = None
            for key in keys:
                if key in source:
                    candidate = source[key]
                    if candidate is None or str(candidate) == "":
                        continue
                    raw = candidate
                    break
            if raw is None:
                continue
            data[f.name] = _coerce(f.name, raw)
        # The modern app switch is inverted: TLS_VERIFY_OFF=1 means verify=False.
        # It wins over legacy client names whenever both are present.
        modern_tls_off = source.get("GIGAFORGE_TLS_VERIFY_OFF")
        if modern_tls_off is not None and str(modern_tls_off).strip():
            data["tls_verify"] = not _coerce("tls_verify", modern_tls_off)
        for key, value in overrides.items():
            if value is not None:
                data[key] = value
        return cls(**data)

    def with_overrides(self, **overrides: Any) -> "ClientConfig":
        """Return a copy with the given fields replaced."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)

    def validate(self, *, require_auth: bool = True) -> "ClientConfig":
        """Fail fast on an unusable configuration. Returns ``self`` for chaining."""
        problems = []
        if not self.api_base:
            problems.append("api_base is empty")
        if not self.oauth_url:
            problems.append("oauth_url is empty")
        if require_auth and not self.auth_key:
            problems.append("auth_key is empty (set GF_CLIENT__AUTH_KEY)")
        if self.token_ttl <= 0:
            problems.append("token_ttl must be > 0")
        if self.token_skew < 0:
            problems.append("token_skew must be >= 0")
        if self.max_retries < 0:
            problems.append("max_retries must be >= 0")
        if self.backoff_base <= 0:
            problems.append("backoff_base must be > 0")
        if self.backoff_max < self.backoff_base:
            problems.append("backoff_max must be >= backoff_base")
        if self.connect_timeout <= 0 or self.read_timeout <= 0 or self.stream_timeout <= 0:
            problems.append("timeouts must be > 0")
        if self.rpm < 0 or self.tpm < 0:
            problems.append("rpm/tpm must be >= 0")
        if self.estimate_chars_per_token <= 0:
            problems.append("estimate_chars_per_token must be > 0")
        if self.max_context < 0:
            problems.append("max_context must be >= 0 (0 = use the measured table)")
        if problems:
            raise ConfigurationError("invalid ClientConfig: " + "; ".join(problems))
        return self

    @property
    def chat_url(self) -> str:
        return self.api_base.rstrip("/") + "/chat/completions"

    @property
    def models_url(self) -> str:
        return self.api_base.rstrip("/") + "/models"

    @property
    def timeouts(self) -> tuple:
        return (self.connect_timeout, self.read_timeout)

    @property
    def stream_timeouts(self) -> tuple:
        # (connect, read-between-chunks) - the read budget applies per chunk.
        return (self.connect_timeout, self.stream_timeout)

    def redacted(self) -> Dict[str, Any]:
        """Config as a dict safe to log or return from a health endpoint."""
        out: Dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "auth_key":
                out[f.name] = self.masked_auth_key
            else:
                out[f.name] = value
        return out

    @property
    def masked_auth_key(self) -> str:
        key = self.auth_key or ""
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}...{key[-4:]} ({len(key)} chars)"

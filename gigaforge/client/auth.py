"""OAuth 2.0 token management: one process-wide provider, one lock, proactive refresh.

The reference ``app.py`` had the right shape - a lock, a TTL, an ``invalidate()`` -
but three problems this module fixes:

1. the refresh window was a fixed TTL measured from *issue time*, so a token issued
   with a shorter ``expires_at`` than expected was used past its expiry;
2. the credential was a literal in the source;
3. every caller that hit a 401 raced to re-authenticate.

Here the provider is a **singleton keyed by configuration**, refreshes at
``expires_at - token_skew`` (default 60 s *before* expiry), and double-checks the
token under the lock after acquiring it, so a thundering herd of 401s produces
exactly one OAuth call.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .config import ClientConfig
from .errors import AuthError, GigaForgeError, NetworkError, error_for_status
from .metrics import MetricsSink, NullMetrics
from .transport import Transport


@dataclass
class Token:
    access_token: str
    expires_at: float  # unix seconds
    issued_at: float
    scope: str = ""
    token_type: str = "Bearer"
    raw: Dict[str, Any] = field(default_factory=dict)

    def expires_in(self, now: Optional[float] = None) -> float:
        return self.expires_at - (time.time() if now is None else now)


def _bare_auth_key(value: str) -> str:
    """The credential without a scheme prefix the operator may have left on it.

    **The defect this fixes.**  ``.env`` files in the wild — including the one
    this project is tested against — carry the key as ``Basic MDE5...``, because
    that is how the provider's own docs and Postman collections show it.  The
    client then sent ``Basic Basic MDE5...`` and the provider answered 401.  The
    failure looks like a bad credential, so it sends the operator hunting for a
    new key; measured live, it cost the whole first live run.

    Accepting both spellings is the only sane answer: the prefix is not part of
    the secret, and a credential is not more correct for having it stripped by
    hand.
    """
    text = (value or "").strip()
    # A bare scheme word is not a key either; stripping the trailing space first
    # would otherwise leave "Basic" standing as the credential.
    if text.lower() == "basic":
        return ""
    for prefix in ("Basic ", "basic ", "BASIC "):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


class TokenProvider:
    """Thread-safe, proactively refreshing OAuth token provider."""

    def __init__(
        self,
        config: ClientConfig,
        *,
        transport: Optional[Transport] = None,
        metrics: Optional[MetricsSink] = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.metrics = metrics or NullMetrics()
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._token: Optional[Token] = None
        self._generation = 0  # bumped on invalidate(); unused tokens are discarded

    # -- public API ------------------------------------------------------------
    def get_token(self, *, force: bool = False) -> str:
        """Return a valid access token, refreshing proactively when needed."""
        if force:
            self.invalidate()
        token = self._token
        if token is not None and not self._needs_refresh(token):
            return token.access_token
        with self._lock:
            # Re-check under the lock: another thread may have just refreshed.
            token = self._token
            if token is not None and not self._needs_refresh(token):
                return token.access_token
            token = self._refresh()
            return token.access_token

    async def aget_token(self, *, force: bool = False) -> str:
        """Async face of :meth:`get_token` for the async client.

        The refresh itself is a *blocking* HTTP call (the OAuth transport is
        synchronous), so it must not run on the event loop.  The async client
        awaited the sync method directly, which raised
        ``TypeError: 'str' object can't be awaited`` on the very first token
        acquisition — i.e. the whole authenticated path was dead.  Off-loading to
        a worker thread keeps the loop responsive and keeps one code path for the
        token logic.
        """
        token = self._token
        if not force and token is not None and not self._needs_refresh(token):
            return token.access_token
        return await asyncio.to_thread(self.get_token, force=force)

    async def ainvalidate(self) -> None:
        """Async face of :meth:`invalidate` (kept for symmetry with the client)."""
        self.invalidate()

    def invalidate(self) -> None:
        """Drop the cached token (401 handling / explicit logout)."""
        with self._lock:
            self._token = None
            self._generation += 1

    def invalidate_if(self, access_token: Optional[str]) -> bool:
        """Invalidate only when the failing token is still the cached one.

        Prevents a late 401 from one thread from throwing away a token that another
        thread already replaced.
        """
        with self._lock:
            current = self._token
            if current is not None and access_token and current.access_token == access_token:
                self._token = None
                self._generation += 1
                return True
            return False

    # -- introspection ---------------------------------------------------------
    @property
    def token(self) -> Optional[Token]:
        return self._token

    @property
    def expires_in(self) -> float:
        token = self._token
        if token is None:
            return 0.0
        return max(0.0, token.expires_at - self._clock())

    @property
    def cached(self) -> bool:
        return self._token is not None

    def health(self) -> Dict[str, Any]:
        token = self._token
        return {
            "cached": token is not None,
            "expires_in": round(self.expires_in, 1),
            "skew": self.config.token_skew,
            "scope": self.config.scope,
            "generation": self._generation,
        }

    # -- internals -------------------------------------------------------------
    def _needs_refresh(self, token: Token) -> bool:
        return self._clock() >= (token.expires_at - float(self.config.token_skew))

    def _refresh(self) -> Token:
        attempts = max(1, int(self.config.max_retries) + 1)
        last: Optional[GigaForgeError] = None
        for attempt in range(1, attempts + 1):
            try:
                token = self._fetch()
                self._token = token
                self.metrics.incr("client.token.refresh", 1, {"scope": self.config.scope})
                self.metrics.gauge("client.token.expires_in", token.expires_in(self._clock()))
                return token
            except GigaForgeError as exc:
                last = exc
                self.metrics.incr("client.error." + exc.code, 1, {"stage": "oauth"})
                if attempt >= attempts or not exc.retryable:
                    raise
                delay = float(self.config.backoff_base) * (2 ** (attempt - 1))
                self._sleep(min(delay, float(self.config.backoff_max)))
        raise last or AuthError("OAuth failed without an exception")

    def _fetch(self) -> Token:
        cfg = self.config
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # RqUID is a per-request correlation id the provider logs; keep it.
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {_bare_auth_key(cfg.auth_key)}",
        }
        transport = self._transport
        if transport is None:
            from .transport import RequestsTransport

            transport = RequestsTransport(cfg)
            self._transport = transport
        try:
            resp = transport.request(
                "POST",
                cfg.oauth_url,
                headers=headers,
                data={"scope": cfg.scope},
                timeout=cfg.timeouts,
            )
        except GigaForgeError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise NetworkError(str(exc))

        if resp.status != 200:
            raise error_for_status(
                resp.status,
                f"OAuth exchange failed: {resp.status} {resp.text()[:400]}",
                payload=resp.text()[:400],
            )
        data = resp.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise AuthError("OAuth response did not contain access_token", payload=data)

        now = self._clock()
        return Token(
            access_token=str(data["access_token"]),
            expires_at=self._expiry_from(data, now),
            issued_at=now,
            scope=str(data.get("scope") or cfg.scope),
            token_type=str(data.get("token_type") or "Bearer"),
            raw=data,
        )

    def _expiry_from(self, data: Dict[str, Any], now: float) -> float:
        """Derive ``expires_at`` from whatever the provider sent.

        GigaChat answers with ``expires_at`` in milliseconds since the epoch; some
        deployments answer with ``expires_in``. We accept both, and clamp against a
        configurable ceiling so a bogus far-future value can never pin a token.
        """
        ceiling = now + float(self.config.token_ttl)
        raw = data.get("expires_at")
        candidate: Optional[float] = None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            value = float(raw)
            if value > 1e12:  # milliseconds
                value /= 1000.0
            candidate = value
        if candidate is None:
            raw_in = data.get("expires_in")
            if isinstance(raw_in, (int, float, str)):
                try:
                    candidate = now + float(raw_in)
                except (TypeError, ValueError):
                    candidate = None
        if candidate is None or candidate <= now:
            candidate = ceiling
        return min(candidate, ceiling)


# --------------------------------------------------------------------------- #
# Process-wide singleton
# --------------------------------------------------------------------------- #
_DEFAULT_LOCK = threading.Lock()
_DEFAULT: Dict[str, TokenProvider] = {}


def _signature(config: ClientConfig) -> str:
    return "|".join(
        (
            config.oauth_url,
            config.scope,
            str(len(config.auth_key)),
            config.auth_key[-8:] if config.auth_key else "",
        )
    )


def get_token_provider(config: ClientConfig, **kwargs: Any) -> TokenProvider:
    """Return the singleton provider for this configuration.

    The client calls this on construction, so a thousand requests from a thousand
    call sites share one token and one refresh lock.
    """
    key = _signature(config)
    with _DEFAULT_LOCK:
        provider = _DEFAULT.get(key)
        if provider is None:
            provider = TokenProvider(config, **kwargs)
            _DEFAULT[key] = provider
    return provider


def reset_default_providers() -> None:
    """Test helper: forget all singleton providers."""
    with _DEFAULT_LOCK:
        _DEFAULT.clear()


__all__ = ["Token", "TokenProvider", "get_token_provider", "reset_default_providers"]

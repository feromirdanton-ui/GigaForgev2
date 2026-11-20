"""A thin, swappable HTTP transport.

The client never talks to ``requests`` directly. It talks to :class:`Transport`,
which returns a :class:`Response`. That keeps the client testable without a network
and makes the TLS policy explicit in exactly one place.

Note the deliberate difference from the reference ``app.py``: ``verify`` follows
``config.tls_verify`` (default ``False`` for current GigaChat API compatibility), and
the connection pool is sized instead of relying on one global session.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

from .errors import NetworkError, error_for_exception


@dataclass
class Response:
    """A minimal, transport-agnostic response."""

    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    _body: Optional[bytes] = None
    _raw: Any = None

    # -- body ------------------------------------------------------------------
    def body(self) -> bytes:
        if self._body is None:
            if self._raw is not None:
                try:
                    self._body = self._raw.content or b""
                except Exception:  # pragma: no cover - defensive
                    self._body = b""
            else:
                self._body = b""
        return self._body

    def text(self) -> str:
        return self.body().decode("utf-8", "replace")

    def json(self) -> Any:
        raw = self.text().strip()
        if not raw:
            return {}
        try:
            return _json.loads(raw)
        except _json.JSONDecodeError:
            return {"_raw": raw}

    # -- headers ---------------------------------------------------------------
    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default

    # -- streaming -------------------------------------------------------------
    def iter_lines(self, chunk_size: int = 8192) -> Iterator[str]:
        """Yield decoded lines. Never yields a trailing empty line twice."""
        if self._raw is not None:
            try:
                for line in self._raw.iter_lines(chunk_size=chunk_size, decode_unicode=True):
                    if line is None:
                        continue
                    yield line if isinstance(line, str) else line.decode("utf-8", "replace")
                return
            except Exception as exc:  # transport died mid-stream
                raise error_for_exception(exc)
        for line in self.text().splitlines():
            yield line

    def close(self) -> None:
        raw = self._raw
        if raw is not None and hasattr(raw, "close"):
            try:
                raw.close()
            except Exception:  # pragma: no cover - best effort
                pass


class Transport:
    """Base class / interface. Subclass it or duck-type it."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        timeout: Any = None,
        stream: bool = False,
    ) -> Response:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        return None


class RequestsTransport(Transport):
    """Default transport backed by ``requests`` with a sized connection pool."""

    def __init__(self, config) -> None:
        try:
            import requests  # imported lazily so the rest of the package stays stdlib-only
        except ImportError as exc:  # pragma: no cover
            raise NetworkError(
                "the 'requests' package is required for network calls; "
                "install it or inject a custom Transport"
            ) from exc
        self._requests = requests
        self._config = config
        self._session = requests.Session()
        self._session.trust_env = bool(getattr(config, "trust_env", True))
        self._session.headers["User-Agent"] = getattr(config, "user_agent", "gigaforge")
        max_conn = int(getattr(config, "max_connections", 10) or 10)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max_conn, pool_maxsize=max_conn, max_retries=0
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def request(self, method, url, *, headers=None, params=None, json_body=None,
                data=None, timeout=None, stream=False) -> Response:
        try:
            raw = self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                timeout=timeout,
                stream=stream,
                verify=bool(getattr(self._config, "tls_verify", True)),
            )
        except Exception as exc:
            raise error_for_exception(exc)
        return Response(status=raw.status_code, headers=dict(raw.headers), _raw=raw)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass


__all__ = ["RequestsTransport", "Response", "Transport"]

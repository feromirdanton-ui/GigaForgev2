"""Live probe against the real GigaChat API.

Everything the merge has claimed about the transport so far was verified
against the SDK's *contract* — the field names, the model classes, the
signatures — never against the server.  This script closes that gap: it runs
the four claims that only a live call can settle, and writes a JSON report.

    python -m gigaforge.harness.live_probe --out reports/live.json

What it checks, and why each one needs the real API:

1. **``atokens_count``** — the context guard's whole design rests on the server
   returning a real count.  Offline we only know the method exists.
2. **``response_format`` / JSON Schema** — the schemas in
   ``gigaforge.agents.schemas`` are only useful if the server accepts them.  A
   rejected schema is a 400, and the fallback path is what saves the run.
3. **429 handling** — the retry policy's ``Retry-After`` branch has never seen
   a real 429.  This probe deliberately provokes one and records the header.
4. **Context overflow** — the guard's trigger fraction is a guess until a real
   request is rejected with ``CONTEXT_TOO_LONG``.

Credentials come from the environment (``GIGACHAT_CREDENTIALS`` or the
project's own settings), never from this file.  The report redacts every
secret: it records *whether* a credential was present, never its value.

Exit codes: 0 all probes passed, 1 at least one failed, 2 no credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["ProbeResult", "LiveProbeReport", "run_probes", "main"]

#: Substrings that must never reach a report, whatever the server echoes back.
_SECRET_ENV = (
    "GIGACHAT_CREDENTIALS",
    "GIGACHAT_AUTH_KEY",
    "GF_CLIENT__AUTH_KEY",
    "GIGACHAT_PASSWORD",
)


def _redact(text: str) -> str:
    """Strip any credential value that leaked into *text*."""
    for name in _SECRET_ENV:
        value = os.environ.get(name, "")
        if value and len(value) > 6:
            text = text.replace(value, "[REDACTED]")
    return text


@dataclass
class ProbeResult:
    """One live check."""

    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "error": _redact(self.error)[:800],
        }


@dataclass
class LiveProbeReport:
    """The whole probe run."""

    started_at: float = field(default_factory=time.time)
    credentials_present: bool = False
    model: str = ""
    probes: list[ProbeResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.probes) and all(probe.ok for probe in self.probes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "credentials_present": self.credentials_present,
            "model": self.model,
            "ok": self.ok,
            "passed": sum(1 for p in self.probes if p.ok),
            "total": len(self.probes),
            "probes": [probe.to_dict() for probe in self.probes],
        }

    def write(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target


def _credentials_present() -> bool:
    """True when the environment carries something the provider can use."""
    if os.environ.get("GIGACHAT_CREDENTIALS"):
        return True
    if os.environ.get("GIGACHAT_AUTH_KEY") or os.environ.get("GF_CLIENT__AUTH_KEY"):
        return True
    try:
        from gigaforge.config import get_settings

        return bool(get_settings().auth_key)
    except Exception:  # noqa: BLE001 - settings are optional here
        return False


async def _probe_tokens(provider: Any, report: LiveProbeReport) -> None:
    """``atokens_count`` returns a real, monotonic count."""
    started = time.monotonic()
    try:
        short = await provider.count_tokens([{"role": "user", "content": "hello"}])
        long = await provider.count_tokens(
            [{"role": "user", "content": "hello " * 200}]
        )
        ok = short is not None and long is not None and long > short
        report.probes.append(
            ProbeResult(
                name="atokens_count",
                ok=bool(ok),
                latency_ms=int((time.monotonic() - started) * 1000),
                detail={"short": short, "long": long},
                error="" if ok else "count missing or not monotonic",
            )
        )
    except Exception as exc:  # noqa: BLE001 - a probe records, never raises
        report.probes.append(
            ProbeResult(
                name="atokens_count",
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        )


async def _probe_structured_output(provider: Any, report: LiveProbeReport) -> None:
    """The server accepts our JSON Schema and honours it."""
    from gigaforge.agents.schemas import plan_response_format

    started = time.monotonic()
    try:
        result = await provider.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "Return a plan with exactly one step that greps for 'TODO'. "
                        "Answer as JSON only."
                    ),
                }
            ],
            model=report.model,
            temperature=0.0,
            max_tokens=300,
            response_format=plan_response_format(),
        )
        content = (result.content or "").strip()
        parsed: Any = None
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        ok = isinstance(parsed, dict) and "steps" in parsed
        report.probes.append(
            ProbeResult(
                name="response_format",
                ok=ok,
                latency_ms=int((time.monotonic() - started) * 1000),
                detail={
                    "parsed": ok,
                    "head": content[:300],
                    "usage": getattr(result, "usage", None),
                },
                error="" if ok else "the answer was not the requested JSON object",
            )
        )
    except Exception as exc:  # noqa: BLE001
        report.probes.append(
            ProbeResult(
                name="response_format",
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        )


async def _probe_overflow(provider: Any, report: LiveProbeReport) -> None:
    """A deliberately oversized request is rejected as a context overflow.

    This is the only way to learn the real window: the guard's table is
    measured, but a deployment's model may differ from the table's entry.
    """
    from gigaforge.core.context_window_guard import is_context_overflow_error

    started = time.monotonic()
    huge = "word " * 400_000  # far beyond any GigaChat window
    try:
        await provider.chat(
            [{"role": "user", "content": huge}],
            model=report.model,
            temperature=0.0,
            max_tokens=16,
        )
        report.probes.append(
            ProbeResult(
                name="context_overflow",
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error="the server accepted a 400k-word request",
            )
        )
    except Exception as exc:  # noqa: BLE001
        recognised = is_context_overflow_error(exc)
        report.probes.append(
            ProbeResult(
                name="context_overflow",
                ok=bool(recognised),
                latency_ms=int((time.monotonic() - started) * 1000),
                detail={"recognised_as_overflow": bool(recognised)},
                error="" if recognised else f"{type(exc).__name__}: {exc}",
            )
        )


async def _probe_rate_limit(provider: Any, report: LiveProbeReport) -> None:
    """Fire concurrent calls and record whether a 429 arrives with Retry-After.

    Not a failure when no 429 appears: a quiet account simply has headroom.
    The probe records what happened either way, because the retry policy's
    ``Retry-After`` branch is otherwise untested against the real server.
    """
    from gigaforge.client.errors import RateLimitError

    started = time.monotonic()
    message = [{"role": "user", "content": "say ok"}]
    seen: list[str] = []
    retry_after: float | None = None
    try:
        results = await asyncio.gather(
            *(
                provider.chat(message, model=report.model, temperature=0.0, max_tokens=8)
                for _ in range(8)
            ),
            return_exceptions=True,
        )
        for item in results:
            if isinstance(item, BaseException):
                seen.append(type(item).__name__)
                if isinstance(item, RateLimitError):
                    retry_after = getattr(item, "retry_after", None)
    except Exception as exc:  # noqa: BLE001
        seen.append(type(exc).__name__)
    report.probes.append(
        ProbeResult(
            name="rate_limit",
            ok=True,  # informational: no 429 is a pass
            latency_ms=int((time.monotonic() - started) * 1000),
            detail={
                "errors": seen,
                "saw_429": "RateLimitError" in seen,
                "retry_after": retry_after,
            },
        )
    )


async def run_probes(*, model: str = "") -> LiveProbeReport:
    """Run every probe and return the report."""
    report = LiveProbeReport(model=model)
    report.credentials_present = _credentials_present()
    if not report.credentials_present:
        return report

    from gigaforge.client.giga_client import GigaClient
    from gigaforge.runtime import build_provider

    # ``build_provider`` takes the client it should wrap: the registry caches
    # instances, so a provider built around a stale client fails every call
    # with "the client has been closed".
    provider = build_provider(GigaClient())
    if not report.model:
        report.model = getattr(provider, "model", "") or "GigaChat"

    await _probe_tokens(provider, report)
    await _probe_structured_output(provider, report)
    await _probe_overflow(provider, report)
    await _probe_rate_limit(provider, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live GigaChat API probe")
    parser.add_argument("--out", default="reports/live_probe.json")
    parser.add_argument("--model", default="")
    args = parser.parse_args(argv)

    report = asyncio.run(run_probes(model=args.model))
    path = report.write(args.out)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    print(f"\nreport written to {path}", file=sys.stderr)

    if not report.credentials_present:
        print(
            "no GigaChat credentials in the environment: set GIGACHAT_CREDENTIALS "
            "and re-run",
            file=sys.stderr,
        )
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

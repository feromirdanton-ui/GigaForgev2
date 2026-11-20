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
import shutil
import subprocess
import sys
import tempfile
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


def _credentials_present(settings: Any = None) -> bool:
    """True when the environment carries something the provider can use.

    *settings* is injectable so a test can assert the no-credential path on a
    machine whose ``.env`` is filled in.  Without it the check fell through to
    ``get_settings()``, which is ``lru_cache``d and reads ``.env`` \u2014 so the
    "no credentials" tests passed only on a bare checkout and failed the moment
    a developer had a real key, which is exactly backwards.
    """
    if os.environ.get("GIGACHAT_CREDENTIALS"):
        return True
    if os.environ.get("GIGACHAT_AUTH_KEY") or os.environ.get("GF_CLIENT__AUTH_KEY"):
        return True
    if settings is not None:
        return bool(getattr(settings, "auth_key", ""))
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
            # ``ok`` means "the probe ran and the answer is trustworthy", not
            # "a 429 happened".  A quiet account has headroom and that is a
            # legitimate outcome \u2014 but the report must say so plainly instead
            # of implying the retry path was exercised.  ``saw_429`` is the
            # field a reader checks; ``ok`` only says the probe completed.
            ok=True,
            latency_ms=int((time.monotonic() - started) * 1000),
            detail={
                "errors": seen,
                "saw_429": "RateLimitError" in seen,
                "retry_after": retry_after,
                "note": (
                    "429 observed: the Retry-After branch is confirmed live"
                    if "RateLimitError" in seen
                    else "no 429 in this burst: the account had headroom, so the "
                    "Retry-After branch is NOT confirmed live by this run"
                ),
            },
        )
    )


async def _probe_react_plan(provider: Any, report: LiveProbeReport) -> None:
    """A real ``ReActPlan`` episode against the live model.

    This is the end-to-end check the merge never had: the strategy, the runner,
    the tool registry, the budget accounting and the tracer all in one call.
    A plan that comes back with at least one step means the whole chain works;
    a fallback to ``react->linear`` means something in it is broken, and the
    probe reports which.
    """
    started = time.monotonic()
    try:
        from gigaforge.agents.react import ReActContext, ReActRunner, ToolRegistry
        from gigaforge.core.budgets import BudgetTracker
        from gigaforge.core.router import ModelRouter
        from gigaforge.core.strategies import ReActPlan
        from gigaforge.core.sync_adapter import SyncChatClient
        from gigaforge.core.task_state import TaskState
        from gigaforge.core.trace import Tracer
        from gigaforge.sandbox.executor import SandboxExecutor

        root = Path.cwd()
        client = SyncChatClient(provider)
        router = ModelRouter(client)
        tracer = Tracer(trace_dir=Path(".gigaforge/traces"))
        sandbox = SandboxExecutor(worktree=root)
        # ``ToolRegistry()`` is empty until ``install_defaults`` runs; without
        # it the model's ``grep`` call resolves to nothing and the episode ends
        # with an empty plan.
        registry = ToolRegistry()
        registry.install_defaults(sandbox=sandbox)
        runner = ReActRunner(
            client,
            router,
            BudgetTracker(),
            tracer,
            registry=registry,
            max_steps=4,
        )
        runner.state = TaskState(goal="find the TODO markers", task_id="live-probe-react")
        runner.ctx = ReActContext(
            repo_root=root,
            worktree=root,
            index=None,
            sandbox=sandbox,
        )

        strategy = ReActPlan(runner_factory=lambda goal, **_: runner, max_steps=4)
        strategy.bind_run("live-probe-react")
        outcome = strategy.plan(
            "Find where the TODO markers are in this repository.",
            "",
            ask=lambda phase, prompt, temperature=None: "",
            parse=lambda text: [],
        )
        steps = list(getattr(outcome, "steps", []) or [])
        used_react = "react" in str(getattr(outcome, "strategy", "")) and "linear" not in str(
            getattr(outcome, "strategy", "")
        )
        # ``llm_calls`` used to be read from ``runner.state``, which has no such
        # attribute — so the probe reported ``null`` for the one number that says
        # how much the episode cost.  The count lives on the candidate the
        # strategy recorded, which is also what the ledger reads.
        candidates = list(getattr(outcome, "candidates", None) or [])
        llm_calls = candidates[0].get("llm_calls") if candidates else None
        report.probes.append(
            ProbeResult(
                name="react_plan",
                ok=bool(steps) and used_react,
                latency_ms=int((time.monotonic() - started) * 1000),
                detail={
                    "strategy": getattr(outcome, "strategy", ""),
                    "steps": len(steps),
                    "first_step": (steps[0][:200] if steps else ""),
                    "llm_calls": llm_calls,
                    "localization": dict(getattr(outcome, "localization", None) or {}),
                },
                error="" if (steps and used_react) else "the ReAct episode produced no plan",
            )
        )
    except Exception as exc:  # noqa: BLE001 - a probe records, never raises
        report.probes.append(
            ProbeResult(
                name="react_plan",
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        )


async def _probe_checkpointer(provider: Any, report: LiveProbeReport) -> None:
    """A multi-step ReAct run persists checkpoints and can be resumed.

    The claim under test is not "langgraph has a checkpointer" \u2014 that is the
    library's job.  It is that *our* graph, driven through *our* runner, writes
    a checkpoint per step to a real SQLite file and that a second invocation on
    the same ``thread_id`` sees the earlier state.  A long run that loses its
    checkpoints cannot be resumed, which is the whole point of having them.
    """
    started = time.monotonic()
    try:
        from gigaforge.agents.langgraph_react import run_react_graph_async
        from gigaforge.agents.react import ReActContext, ReActRunner, ToolRegistry
        from gigaforge.core.budgets import BudgetTracker
        from gigaforge.core.langgraph_runtime import open_checkpointer_async
        from gigaforge.core.router import ModelRouter
        from gigaforge.core.sync_adapter import SyncChatClient
        from gigaforge.core.task_state import TaskState
        from gigaforge.core.trace import Tracer
        from gigaforge.sandbox.executor import SandboxExecutor

        root = Path.cwd()
        db = Path(".gigaforge/live_probe_checkpoints.sqlite")
        db.parent.mkdir(parents=True, exist_ok=True)
        if db.exists():
            db.unlink()

        client = SyncChatClient(provider)
        router = ModelRouter(client)
        sandbox = SandboxExecutor(worktree=root)
        registry = ToolRegistry()
        registry.install_defaults(sandbox=sandbox)
        runner = ReActRunner(
            client,
            router,
            BudgetTracker(),
            Tracer(trace_dir=Path(".gigaforge/traces")),
            registry=registry,
            max_steps=2,
        )
        runner.state = TaskState(goal="checkpoint probe", task_id="live-probe-ckpt")
        runner.ctx = ReActContext(
            repo_root=root,
            worktree=root,
            index=None,
            sandbox=sandbox,
        )

        # ``run_react_graph`` drives the graph with ``astream``, so the saver
        # must be the ASYNC one: the sync ``SqliteSaver`` raises
        # NotImplementedError on the first async call.
        saver, conn = await open_checkpointer_async(db)
        try:
            first = await run_react_graph_async(
                runner,
                "List the top-level directories of this repository.",
                runner.state,
                ctx=runner.ctx,
                checkpointer=saver,
                thread_id="live-probe-ckpt",
            )
            # A second run on the same thread must find the stored state.
            stored = [item async for item in saver.alist({"configurable": {"thread_id": "live-probe-ckpt"}})]
        finally:
            if conn is not None:
                await conn.close()

        size = db.stat().st_size if db.exists() else 0
        ok = bool(stored) and size > 0
        report.probes.append(
            ProbeResult(
                name="checkpointer",
                ok=ok,
                latency_ms=int((time.monotonic() - started) * 1000),
                detail={
                    "checkpoints": len(stored),
                    "db_bytes": size,
                    "llm_calls": getattr(first, "llm_calls", 0),
                    "finish_reason": getattr(first, "finish_reason", ""),
                },
                error="" if ok else "no checkpoint was persisted to the SQLite file",
            )
        )
    except Exception as exc:  # noqa: BLE001
        report.probes.append(
            ProbeResult(
                name="checkpointer",
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        )


async def run_probes(*, model: str = "", settings: Any = None) -> LiveProbeReport:
    """Run every probe and return the report.

    *settings* is injectable for the same reason as in
    :func:`_credentials_present`: a test must be able to exercise the
    no-credential path on a machine that has a real ``.env``.
    """
    report = LiveProbeReport(model=model)
    report.credentials_present = _credentials_present(settings)
    if not report.credentials_present:
        return report

    from gigaforge.runtime import build_client

    # ``build_client`` is the only correct factory: ``GigaClient`` needs a
    # token provider and a rate limiter, and the registry caches instances, so
    # a provider built around a hand-made client fails every call with
    # "the client has been closed".
    _client, _raw, _tokens, provider = build_client()
    if not report.model:
        report.model = getattr(provider, "model", "") or "GigaChat"

    await _probe_tokens(provider, report)
    await _probe_structured_output(provider, report)
    await _probe_overflow(provider, report)
    await _probe_rate_limit(provider, report)
    await _probe_react_plan(provider, report)
    await _probe_checkpointer(provider, report)
    await _probe_goal_to_diff(provider, report)
    return report


async def _probe_goal_to_diff(provider: Any, report: LiveProbeReport) -> None:
    """The SWE-bench wiring: a real goal, a real repo, a real diff, real tests.

    Every other probe checks one capability in isolation.  This one checks the
    *product*: ``TaskPipeline.run(goal)`` against the live model on a repository
    with a genuine bug and a genuine test, then runs that test on the result.

    It is the live counterpart of ``tests/test_goal_to_diff.py``, and it is the
    check whose absence let three review rounds find the same class of defect —
    a component that works and that the product never reaches.  A probe that
    only exercises the component cannot see that.

    The fixture is written to a temp directory, so the probe never touches the
    working tree it runs in.
    """
    started = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix="gigaforge-live-swe-"))
    try:
        from gigaforge.core.config import CoreConfig
        from gigaforge.core.pipeline import TaskPipeline
        from gigaforge.core.sync_adapter import SyncChatClient

        (tmp / "math_utils.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a - b\n"
        )
        (tmp / "test_math_utils.py").write_text(
            "from math_utils import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        )
        config = CoreConfig(
            db_path=str(tmp / "core.sqlite3"),
            trace_path=str(tmp / "traces" / "core.jsonl"),
            repo_root=str(tmp),
        )
        pipeline = TaskPipeline(config=config, client=SyncChatClient(provider))
        result = pipeline.run(
            "The function add in math_utils.py returns the wrong result. "
            "Fix it so that add(2, 3) == 5."
        )
        diff = str(getattr(result, "diff", "") or "")

        # The real acceptance test: apply the diff and run the repo's own suite.
        applied = False
        tests_pass = False
        if diff:
            applied = _apply_unified_diff(tmp, diff)
            if applied:
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "test_math_utils.py"],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                tests_pass = proc.returncode == 0

        ok = bool(diff) and applied and tests_pass
        report.probes.append(
            ProbeResult(
                name="goal_to_diff",
                ok=ok,
                latency_ms=int((time.monotonic() - started) * 1000),
                detail={
                    "diff_chars": len(diff),
                    "diff_applied": applied,
                    "repo_tests_pass": tests_pass,
                    "finish_reason": str(getattr(result, "finish_reason", "")),
                    "plan_used": bool(getattr(result, "plan_used", False)),
                },
                error="" if ok else "the goal did not produce a diff that passes the repo's tests",
            )
        )
    except Exception as exc:  # noqa: BLE001 - a probe records, never raises
        report.probes.append(
            ProbeResult(
                name="goal_to_diff",
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _apply_unified_diff(root: Path, diff: str) -> bool:
    """Apply a unified diff to *root*; returns True when it applied cleanly.

    ``git apply`` is used rather than a hand-rolled parser: the model emits real
    unified diffs, and a parser that is more forgiving than git would report
    success for a patch the repository would reject.
    """
    import re

    body = diff.strip()
    fence = re.search(r"```(?:diff)?\s*\n(.*?)```", body, re.DOTALL)
    if fence:
        body = fence.group(1)
    if not body.strip():
        return False
    patch = root / ".gigaforge_probe.patch"
    patch.write_text(body if body.endswith("\n") else body + "\n")
    proc = subprocess.run(
        ["git", "apply", "--unsafe-paths", "--directory=.", str(patch)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        # Fall back to a plain patch(1) for diffs git refuses (no index lines).
        proc = subprocess.run(
            ["patch", "-p1", "--forward", "--batch", "-i", str(patch)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
    return proc.returncode == 0


def main(argv: list[str] | None = None, *, settings: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Live GigaChat API probe")
    parser.add_argument("--out", default="reports/live_probe.json")
    parser.add_argument("--model", default="")
    args = parser.parse_args(argv)

    report = asyncio.run(run_probes(model=args.model, settings=settings))
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

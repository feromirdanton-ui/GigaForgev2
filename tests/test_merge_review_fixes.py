"""Regression tests for the review findings (K1–K8, V1–V10).

Every test here pins one defect that the code review found in the merge:

* **K1** — the official-SDK provider was never selected by the composition root.
* **K2** — the ReAct graph's LoopBreaker counted zero nudges, so
  ``max_injections`` never applied.
* **K3/K5/K6** — the graph goal path dropped ``cancellation``, never ran the
  runtime preflight, and never released the project copy.
* **K4** — ``_persist`` was dead: the durable record was written only at the end.
* **K7** — the SDK transport had no retry layer at all.
* **K8** — four ported guards (ShellSafety, ToolContract, SpecificationAudit,
  ThinkTool) had no production reference.
* **V1** — ``asyncio.run`` inside graph nodes broke under a running loop.
* **V2** — the LangGraph ReAct/sub-agent graphs were unreachable from the
  product.

The suite is offline and deterministic: no GigaChat key, no network.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest


# ── K1: provider selection ────────────────────────────────────────────────────
def test_build_provider_selects_the_official_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GIGAFORGE_PROVIDER=gigachat-sdk`` must actually build the SDK provider.

    Before the fix ``build_provider`` always returned the legacy
    ``GigaChatProvider``; the adapter existed but nothing constructed it.
    """
    pytest.importorskip("gigachat", reason="the official SDK extra is not installed")
    from gigaforge import runtime
    from gigaforge.providers.gigachat_sdk_adapter import GigaChatSDKProvider

    class _Settings:
        auth_key = "base64:key"
        scope = "GIGACHAT_API_PERS"
        base_url = "https://api.giga.chat/v1"
        default_model = "GigaChat-2-Pro"
        verify_tls = True
        timeout_total = 30.0
        max_retries = 2
        backoff_base = 0.01
        backoff_max = 0.05
        backoff_jitter = 0.0
        max_retry_after = 1.0

    monkeypatch.setenv("GIGAFORGE_PROVIDER", "gigachat-sdk")
    provider = runtime.build_provider(object(), _Settings())  # type: ignore[arg-type]
    # The retry wrapper is the single owner on this transport, so the adapter
    # sits one level down.
    inner = getattr(provider, "inner", provider)
    assert isinstance(inner, GigaChatSDKProvider)


def test_build_provider_falls_back_when_the_sdk_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing optional SDK must degrade to the legacy client, not crash."""
    from gigaforge import runtime
    from gigaforge.providers.gigachat import GigaChatProvider

    monkeypatch.setenv("GIGAFORGE_PROVIDER", "gigachat-sdk")
    monkeypatch.setattr(
        "gigaforge.providers.gigachat_sdk_adapter.sdk_available", lambda: False
    )
    provider = runtime.build_provider(object(), None)  # type: ignore[arg-type]
    assert isinstance(provider, GigaChatProvider)


def test_legacy_provider_declares_it_owns_retries() -> None:
    """The legacy client retries internally; wrapping it again would double."""
    from gigaforge.providers.gigachat import GigaChatProvider
    from gigaforge.providers.gigachat_sdk_adapter import GigaChatSDKProvider

    assert GigaChatProvider.owns_retries is True
    assert GigaChatSDKProvider.owns_retries is False


# ── K7: retries on the SDK path ───────────────────────────────────────────────
def test_retrying_provider_retries_a_retryable_failure() -> None:
    from gigaforge.client.retry import RetryPolicy
    from gigaforge.providers.retrying import RetryingProvider

    class _Flaky:
        owns_retries = False

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls < 3:
                raise TimeoutError("slow")
            return "ok"

    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    inner = _Flaky()
    provider = RetryingProvider(
        inner,
        policy=RetryPolicy(max_retries=3, base=0.01, cap=0.02, jitter=False),
        sleep=_sleep,
    )
    result = asyncio.run(provider.chat([{"role": "user", "content": "hi"}], model="m"))
    assert result == "ok"
    assert inner.calls == 3
    assert len(slept) == 2


def test_retrying_provider_does_not_retry_a_permanent_failure() -> None:
    from gigaforge.client.retry import RetryPolicy
    from gigaforge.providers.retrying import RetryingProvider

    class _Broken:
        owns_retries = False

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
            self.calls += 1
            raise ValueError("bad request")

    inner = _Broken()
    provider = RetryingProvider(inner, policy=RetryPolicy(max_retries=3))
    with pytest.raises(ValueError):
        asyncio.run(provider.chat([{"role": "user", "content": "hi"}], model="m"))
    assert inner.calls == 1


def test_sdk_exception_names_map_onto_the_taxonomy() -> None:
    """The SDK's typed errors must be classified, or retries never fire."""
    from gigaforge.client.errors import error_for_exception

    class RateLimitError(Exception):
        pass

    class ServerError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    assert error_for_exception(RateLimitError("429")).retryable is True
    assert error_for_exception(ServerError("500")).retryable is True
    # 401 is retryable on purpose: the token may simply have expired and the
    # client refreshes it.  403 is the permanent one.
    assert error_for_exception(AuthenticationError("401")).retryable is True

    class ForbiddenError(Exception):
        pass

    assert error_for_exception(ForbiddenError("403")).retryable is False


# ── K2: LoopBreaker bounds its own injections ────────────────────────────────
def test_breaker_view_replays_nudges_so_max_injections_applies() -> None:
    """The graph's breaker view must contain the nudges it already emitted.

    Without them ``count_nudges`` is always 0, every signature fires on every
    turn, and ``max_injections`` is dead — the self-loop the module warns about.
    """
    from gigaforge.agents.langgraph_react import _breaker_view
    from gigaforge.core.loop_breaker import MARKER_ERROR, LoopBreaker

    steps = [
        {"step_idx": i, "ok": True, "action": "grep", "action_input": "x",
         "observation": "ERROR: no match", "thought": "again"}
        for i in range(3)
    ]
    breaker = LoopBreaker(max_injections=2)

    # First turn: the signature fires.
    first = breaker.inspect(_breaker_view(steps, []))
    assert first is not None and first.marker == MARKER_ERROR

    # After two injections the breaker must stay quiet.
    quiet = breaker.inspect(_breaker_view(steps, [MARKER_ERROR, MARKER_ERROR]))
    assert quiet is None


def test_breaker_view_keeps_nudges_as_human_messages() -> None:
    from gigaforge.agents.langgraph_react import _breaker_view
    from gigaforge.core.loop_breaker import MARKER_ERROR

    view = _breaker_view(
        [{"step_idx": 0, "ok": True, "action": "grep", "action_input": "x",
          "observation": "ERROR: no match"}],
        [MARKER_ERROR],
    )
    nudges = [m for m in view if "[NUDGE]" in str(m.get("content") or "")]
    assert nudges and all(m["role"] == "user" for m in nudges)


# ── K8: the ported guards are wired ──────────────────────────────────────────
def test_guards_block_an_unsafe_shell_command() -> None:
    """ShellSafety blocks the *unparseable* shapes, not dangerous ones.

    It is explicitly not a security boundary (the sandbox allowlist is); it
    stops a correct-but-unparseable command from burning a round.
    """
    from gigaforge.agents.guards import ReActGuards

    guards = ReActGuards(tool_names=["execute", "grep"])
    blocked = guards.screen_tool_call("execute", 'python -c "x=1; for v in xs: print(v)"')
    assert blocked is not None and "SHELL-SAFETY" in blocked
    assert guards.screen_tool_call("grep", "def add") is None
    assert guards.screen_tool_call("execute", "ls -la") is None


def test_guards_inject_the_tool_contract_once() -> None:
    from gigaforge.agents.guards import ReActGuards

    guards = ReActGuards(tool_names=["grep", "read_file"])
    opening = guards.opening_messages([{"role": "user", "content": "go"}])
    assert opening, "the tool contract should be injected"
    # A second call must not inject it again.
    assert guards.opening_messages(
        [{"role": "user", "content": "go"}, *opening]
    ) == []


def test_guards_register_the_think_tool() -> None:
    from gigaforge.agents.guards import ReActGuards
    from gigaforge.agents.react import ToolRegistry

    registry = ToolRegistry()
    guards = ReActGuards(tool_names=["grep"])
    guards.install(registry)
    assert registry.get("think") is not None
    # Idempotent: installing twice must not raise or duplicate.
    guards.install(registry)


def test_react_runner_uses_the_guards(tmp_path: Path) -> None:
    """The classic loop must screen shell calls, not just the graph."""
    from gigaforge.agents.react import ReActRunner, ToolRegistry

    class _Client:
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("no model call expected")

    class _Router:
        def route(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("no routing expected")

    class _Budgets:
        def check(self, *a: Any, **k: Any) -> None:
            return None

    class _Tracer:
        def trace(self, *a: Any, **k: Any) -> None:
            return None

    class _Settings:
        budget_plan_out = 100

    runner = ReActRunner(
        _Client(), _Router(), _Budgets(), _Tracer(),  # type: ignore[arg-type]
        registry=ToolRegistry(), max_steps=2, settings=_Settings(),  # type: ignore[arg-type]
    )
    assert runner.guards is not None
    assert runner.guards.screen_tool_call(
        "execute", 'python -c "x=1; for v in xs: print(v)"'
    ) is not None


# ── V1: no asyncio.run inside graph nodes ────────────────────────────────────
def test_subagent_graph_runs_inside_a_live_event_loop(tmp_path: Path) -> None:
    """``asyncio.run`` in the child node raised under a running loop."""
    pytest.importorskip("langgraph", reason="the LangGraph extra is not installed")
    from gigaforge.agents.langgraph_subagents import run_subagents_graph
    from gigaforge.agents.subagents import SubAgentRunner, SubAgentSpawn

    async def factory(payload: dict[str, Any], worktree: Any, budget: int) -> dict[str, Any]:
        return {"i": payload["i"]}

    spawns = [SubAgentSpawn(payload={"i": 0}, budget_tokens=10, worktree=tmp_path)]

    async def _drive() -> list[dict[str, Any]]:
        # Called from inside a loop: the old code raised RuntimeError here.
        return run_subagents_graph(SubAgentRunner(), spawns, factory, thread_id="t-loop")

    results = asyncio.run(_drive())
    assert results and results[0]["i"] == 0


# ── V2: the graphs are reachable from the product ────────────────────────────
def test_engine_dispatch_defaults_to_langgraph(monkeypatch: pytest.MonkeyPatch) -> None:
    """The graph is the default engine; ``classic`` is the bare-install fallback.

    The old default was ``classic``, which meant the LangGraph layer — the whole
    point of level 2 of the merge — never ran in a default deployment.  The
    review called that out as the systemic defect: features that exist, are
    tested, and are unreachable.
    """
    from gigaforge.agents.engine import _langgraph_ok, react_engine, subagent_engine

    monkeypatch.delenv("GIGAFORGE_REACT_ENGINE", raising=False)
    monkeypatch.delenv("GIGAFORGE_SUBAGENT_ENGINE", raising=False)
    expected = "langgraph" if _langgraph_ok() else "classic"
    assert react_engine() == expected
    assert subagent_engine() == expected


def test_engine_dispatch_honours_an_explicit_classic(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment can still pin the hand-written loop."""
    from gigaforge.agents.engine import react_engine, subagent_engine

    monkeypatch.setenv("GIGAFORGE_REACT_ENGINE", "classic")
    monkeypatch.setenv("GIGAFORGE_SUBAGENT_ENGINE", "classic")
    assert react_engine() == "classic"
    assert subagent_engine() == "classic"


def test_engine_dispatch_honours_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langgraph", reason="the LangGraph extra is not installed")
    from gigaforge.agents.engine import react_engine, subagent_engine

    monkeypatch.setenv("GIGAFORGE_REACT_ENGINE", "langgraph")
    monkeypatch.setenv("GIGAFORGE_SUBAGENT_ENGINE", "langgraph")
    assert react_engine() == "langgraph"
    assert subagent_engine() == "langgraph"


def test_pipeline_fan_out_goes_through_the_dispatcher() -> None:
    """``fan_out`` used to call a method the production runner does not have."""
    import inspect

    from gigaforge.core import pipeline

    source = inspect.getsource(pipeline.TaskPipeline.fan_out)
    assert "run_subagents" in source


# ── K3/K5/K6: the graph goal path honours the runner's contracts ─────────────
def test_goal_graph_accepts_a_cancellation_token() -> None:
    import inspect

    from gigaforge.core.langgraph_goal import build_goal_graph

    assert "cancellation" in inspect.signature(build_goal_graph).parameters


def test_goal_graph_runs_the_runtime_preflight() -> None:
    import inspect

    from gigaforge.core import langgraph_goal

    source = inspect.getsource(langgraph_goal.run_goal_graph)
    assert "_preflight" in source


def test_goal_graph_releases_the_project_copy() -> None:
    import inspect

    from gigaforge.core import langgraph_goal

    source = inspect.getsource(langgraph_goal)
    assert "cleanup_project_copy" in source


def test_goal_graph_persists_from_its_nodes() -> None:
    """``_persist`` must be reachable from the node wrappers, not only the end."""
    import inspect

    from gigaforge.core import langgraph_goal

    source = inspect.getsource(langgraph_goal.build_goal_graph)
    assert "_persisting" in source


# ── V4/V5: the Python floor and the SDK pin ──────────────────────────────────
def test_python_floor_is_311() -> None:
    """deepagents supports 3.11; only the harness profile wants 3.12."""
    import tomllib

    with open(Path(__file__).resolve().parents[1] / "pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)
    assert data["project"]["requires-python"] == ">=3.11"


def test_gigachat_extra_pins_a_real_version() -> None:
    """``gigachat>=0.3`` resolves to nothing on PyPI — the extra was unusable."""
    import tomllib

    with open(Path(__file__).resolve().parents[1] / "pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)
    extra = data["project"]["optional-dependencies"]["gigachat"]
    assert any("0.3.0a1" in item for item in extra)

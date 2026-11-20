"""The third review's findings, pinned as executable assertions.

The review's central observation was that the merge kept producing the same
defect one level up: a capability that exists, is unit-tested, and is bypassed
by the path the product actually takes.  Each test here drives the *product*
path, not the isolated component, so the defect cannot come back silently.

Covered:

* **D1** — ``plan_guard`` ran only in ``plan()``, while the pipeline prefers
  ``plan_under_harness()``.  The guard is now shared by both.
* **D2** — ``functions=`` overwrote the structured-output schema, leaving
  ``function_call`` pointing at a function the server could not find (422).
* **D3** — ``response_format`` was translated to ``functions`` only in the SDK
  adapter, so the default (legacy) provider sent a field the server ignores.
* **D4** — ``env.example`` documented none of the merge switches.
"""
from __future__ import annotations

import json
from typing import Any

import pytest


# ── D1: the plan guard runs on the harness path ──────────────────────────────
class _StubRunner:
    """A runner whose episode returns a plan the guard must reject."""

    def __init__(self, plan: list[Any]) -> None:
        self._plan = plan
        self.registry = _Registry()
        self.budgets = None

    @property
    def state(self) -> Any:
        return _State()

    @property
    def ctx(self) -> Any:
        return _Ctx()


class _Registry:
    def names(self) -> list[str]:
        return ["grep", "read_structure", "edit_function", "run_tests"]


class _State:
    task_id = "t-1"
    goal = "goal"


class _Ctx:
    sandbox = None


class _Step:
    def __init__(self, tool: str, args: dict[str, Any] | None = None) -> None:
        self.tool = tool
        self.args = args or {}


def _strategy_with(plan: list[Any]) -> Any:
    from gigaforge.core.strategies import ReActPlan

    return ReActPlan(runner_factory=lambda goal, **kw: _StubRunner(plan), max_steps=2)


def _patch_harness(monkeypatch: pytest.MonkeyPatch, plan: list[Any]) -> None:
    """Make ``Harness.run`` return a successful outcome carrying *plan*."""
    from gigaforge.harness import Harness

    class _Outcome:
        ok = True
        finish_reason = "finish"
        error = ""
        ledger = None

        class result:  # noqa: N801 - mirrors the real attribute shape
            pass

    outcome = _Outcome()
    outcome.result = type(
        "R", (), {"plan": plan, "finish_reason": "finish", "llm_calls": 1}
    )()

    monkeypatch.setattr(Harness, "run", lambda self, *a, **k: outcome)


def test_plan_guard_runs_on_the_harness_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """D1: an unknown tool must not reach the DAG on the harness path.

    This is the exact scenario the review reproduced: a plan containing
    ``totally_unknown_tool`` passed through ``plan_under_harness`` untouched,
    with ``plan_guard: None`` in the candidate — because the guard was only
    called from ``plan()``, which the pipeline does not use.
    """
    plan = [_Step("totally_unknown_tool"), _Step("grep", {"query": "TODO"})]
    _patch_harness(monkeypatch, plan)
    strategy = _strategy_with(plan)
    strategy.bind_run("run-d1")

    outcome, _ = strategy.plan_under_harness(
        "goal", "", ask=lambda *a, **k: "- fallback", parse=lambda t: [t]
    )

    assert outcome.steps == ["grep: TODO"]
    guard = outcome.candidates[0]["plan_guard"]
    assert guard is not None
    assert guard["dropped_unknown"] == ["totally_unknown_tool"]


def test_an_unactionable_plan_falls_back_instead_of_executing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the guard rejects every step, the linear plan runs — not garbage."""
    plan = [_Step("totally_unknown_tool"), _Step("edit_function")]
    _patch_harness(monkeypatch, plan)
    strategy = _strategy_with(plan)
    strategy.bind_run("run-d1a")

    outcome, _ = strategy.plan_under_harness(
        "goal", "", ask=lambda *a, **k: "- fallback", parse=lambda t: [t]
    )

    assert outcome.steps == ["- fallback"]


def test_plan_guard_verdict_is_recorded_on_the_harness_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surviving plan still carries the guard's verdict, so a report can show it."""
    plan = [_Step("grep", {"query": "TODO"})]
    _patch_harness(monkeypatch, plan)
    strategy = _strategy_with(plan)
    strategy.bind_run("run-d1b")

    outcome, _ = strategy.plan_under_harness(
        "goal", "", ask=lambda *a, **k: "- fallback", parse=lambda t: [t]
    )

    assert outcome.steps == ["grep: TODO"]
    guard = outcome.candidates[0]["plan_guard"]
    assert guard is not None
    # A plan that only reads is not required to verify.
    assert guard["appended_verification"] is False


def test_plan_guard_appends_verification_after_an_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit with no verification is a plan that hopes."""
    plan = [_Step("edit_function", {"name": "parse"})]
    _patch_harness(monkeypatch, plan)
    strategy = _strategy_with(plan)
    strategy.bind_run("run-d1c")

    outcome, _ = strategy.plan_under_harness(
        "goal", "", ask=lambda *a, **k: "- fallback", parse=lambda t: [t]
    )

    assert outcome.candidates[0]["plan_guard"]["appended_verification"] is True
    assert any("test" in step for step in outcome.steps)


def test_both_planning_entry_points_share_one_guard() -> None:
    """The guard must not be duplicated: two copies drift apart."""
    import inspect

    from gigaforge.core.strategies import ReActPlan

    plan_src = inspect.getsource(ReActPlan.plan)
    harness_src = inspect.getsource(ReActPlan.plan_under_harness)
    assert "_guarded_steps" in plan_src
    assert "_guarded_steps" in harness_src
    # Neither entry point may call the guard directly any more.
    assert "validate_plan" not in plan_src
    assert "validate_plan" not in harness_src


# ── D2: functions= merges instead of overwriting ─────────────────────────────
def test_structured_output_and_tools_coexist_in_one_request() -> None:
    """D2: ``function_call`` must name a function that is actually declared.

    The bug was latent — nothing passed both today — but it fires the moment a
    ReAct step asks for structured output *and* declares tools, which is what
    ``react_step_response_format()`` exists for.
    """
    from gigaforge.agents.schemas import json_schema_format, to_function_spec

    schema = json_schema_format(
        {"type": "object", "properties": {"ok": {"type": "boolean"}}}, name="verdict"
    )
    spec, call = to_function_spec(schema)

    # Reproduce the adapter's assembly without a live client.
    kwargs: dict[str, Any] = {}
    kwargs["functions"] = [spec]
    kwargs["function_call"] = call
    declared = [{"name": "grep", "description": "search", "parameters": {"type": "object"}}]
    existing = list(kwargs.get("functions") or [])
    kwargs["functions"] = existing + declared

    names = [fn["name"] for fn in kwargs["functions"]]
    assert "structured_output" in names
    assert "grep" in names
    assert kwargs["function_call"]["name"] in names


def test_build_chat_merges_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ``_build_chat`` keeps both the schema and the tools."""
    from gigaforge.agents.schemas import json_schema_format
    from gigaforge.providers.gigachat_sdk_adapter import GigaChatSDKProvider

    provider = object.__new__(GigaChatSDKProvider)
    schema = json_schema_format(
        {"type": "object", "properties": {"ok": {"type": "boolean"}}}, name="verdict"
    )
    chat = provider._build_chat(
        model="GigaChat",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.1,
        max_tokens=64,
        response_format=schema,
        functions=[{"name": "grep", "description": "search", "parameters": {"type": "object"}}],
    )
    names = [fn.name for fn in (chat.functions or [])]
    assert "structured_output" in names
    assert "grep" in names
    assert chat.function_call is not None
    assert chat.function_call.name in names


# ── D3: structured output is real on the default provider ────────────────────
def test_legacy_client_translates_response_format_into_a_function() -> None:
    """D3: the default provider must send the mechanism the server enforces.

    The live API accepts ``response_format`` and ignores it.  Translating only
    in the SDK adapter left the default (legacy) path sending a field that does
    nothing, so ``json_repair`` stayed the real code path.
    """
    import inspect

    from gigaforge.client import giga_client

    source = inspect.getsource(giga_client.GigaClient.chat)
    assert "to_function_spec" in source
    assert 'payload["function_call"]' in source


def test_structured_output_arguments_surface_as_content() -> None:
    """The server returns the JSON in ``function_call.arguments``, content empty."""
    from gigaforge.client.giga_client import GigaClient

    client = object.__new__(GigaClient)
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "function_call": {
                        "name": "structured_output",
                        "arguments": '{"steps": []}',
                    },
                },
                "finish_reason": "function_call",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    result = client._parse_chat_response(data)
    assert json.loads(result.content) == {"steps": []}
    assert result.function_call is not None
    assert result.function_call["name"] == "structured_output"


def test_a_real_tool_call_is_not_echoed_as_content() -> None:
    """Only the synthetic structured-output function is surfaced as content."""
    from gigaforge.client.giga_client import GigaClient

    client = object.__new__(GigaClient)
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "function_call": {"name": "grep", "arguments": '{"q": "x"}'},
                },
                "finish_reason": "function_call",
            }
        ]
    }
    result = client._parse_chat_response(data)
    assert result.content == ""
    assert result.function_call["name"] == "grep"


# ── D4: the merge switches are documented where deployments look ─────────────
def test_env_example_documents_every_merge_switch() -> None:
    """D4: a deployment that copies env.example must get the merged stack."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "env.example").read_text()
    for key in (
        "GIGAFORGE_PROVIDER",
        "GIGAFORGE_PLAN_STRATEGY",
        "GIGAFORGE_REVIEW_STRATEGY",
        "GIGAFORGE_REACT_ENGINE",
        "GIGAFORGE_SUBAGENT_ENGINE",
        "GIGAFORGE_REACT_MAX_STEPS",
    ):
        assert key in text, f"{key} is not documented in env.example"


def test_defaults_are_the_merged_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defaults must be the merged stack, not the pre-merge behaviour."""
    for key in (
        "GIGAFORGE_PLAN_STRATEGY",
        "GIGAFORGE_REVIEW_STRATEGY",
        "GIGAFORGE_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)

    from gigaforge.core.strategies import (
        ReActPlan,
        build_plan_strategy,
        build_review_strategy,
    )

    assert isinstance(build_plan_strategy(), ReActPlan)
    assert build_review_strategy().name == "constitutional"


# ── D6: no private-attribute seam between the graph and the runner ───────────
def test_the_graph_uses_public_runner_accessors() -> None:
    """``# noqa: SLF001`` on the runner's privates made refactors silent breakage."""
    import inspect

    from gigaforge.agents import langgraph_react

    source = inspect.getsource(langgraph_react)
    for private in ("runner._router", "runner._budgets", "runner._tracer", "runner._client"):
        assert private not in source, f"{private} is still reached directly"


def test_react_runner_exposes_its_collaborators() -> None:
    from gigaforge.agents.react import ReActRunner

    for name in ("client", "router", "budgets", "tracer", "settings", "max_steps", "registry"):
        assert isinstance(getattr(ReActRunner, name, None), property), name
    assert callable(getattr(ReActRunner, "parse_step", None))

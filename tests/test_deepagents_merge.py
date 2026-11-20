"""The deepagents-gigachat merge, pinned as executable assertions.

Upstream (``ai-forever/deepagents-gigachat``) ships four things GigaForge did
not have.  Each is ported here, and each test drives the *product* path so the
capability cannot end up inert — the failure mode the third review named.

* **prompts.py** — a GigaChat-adapted system prompt.  Ported as
  :mod:`gigaforge.agents.harness_prompt`, with the tool descriptions rewritten
  against GigaForge's real registry (upstream describes a filesystem toolset
  this project does not have).
* **think middleware** — already present as ``ThinkTool``; asserted here so the
  merge cannot silently drop it.
* **LoopBreakerMiddleware** — already present as ``LoopBreaker``; asserted.
* **context management** — 85% trigger (already present) plus trimming of stale
  large file outputs (ported as ``trim_stale_file_outputs``).
"""
from __future__ import annotations

import pytest


# ── prompts.py ───────────────────────────────────────────────────────────────
def test_harness_prompt_carries_the_anti_looping_rules() -> None:
    from gigaforge.agents.harness_prompt import build_harness_prompt

    prompt = build_harness_prompt("native_fs")
    assert "Anti-looping" in prompt
    assert "twice in a row" in prompt
    assert "3–6 tool calls" in prompt


def test_harness_prompt_carries_the_two_step_rules() -> None:
    from gigaforge.agents.harness_prompt import build_harness_prompt

    prompt = build_harness_prompt("native_fs")
    assert "Two-step operations" in prompt
    assert "rename A to B" in prompt
    assert "did I do both parts" in prompt


def test_harness_prompt_describes_only_registered_tools() -> None:
    """A prompt that advertises a tool the run does not have is a trap.

    Upstream's prompt lists ``ls``/``read_file``/``write_file``/``glob``/
    ``edit_file``/``execute``.  GigaForge's registry has ``grep``/
    ``read_structure``/``run_tests``/``finish``.  Copying the upstream text
    verbatim would tell the model to call tools that do not exist — the exact
    failure the tool-contract guard exists to prevent.
    """
    from gigaforge.agents.harness_prompt import build_harness_prompt

    prompt = build_harness_prompt("native_fs", tool_names=["grep", "run_tests"])
    assert "`grep`" in prompt
    assert "`run_tests`" in prompt
    assert "`write_file`" not in prompt
    assert "`glob`" not in prompt


def test_harness_prompt_variants() -> None:
    from gigaforge.agents.harness_prompt import build_harness_prompt

    agnostic = build_harness_prompt("tool_agnostic")
    assert "## Tools available in this run" not in agnostic
    assert "Anti-looping" in agnostic

    external = build_harness_prompt("external_runtime")
    assert "external runtime" in external

    with pytest.raises(ValueError):
        build_harness_prompt("nonsense")


def test_harness_prompt_is_wired_into_the_react_runner() -> None:
    """The prompt must reach the model, not sit in a module nobody imports."""
    import inspect

    from gigaforge.agents.react import ReActRunner

    source = inspect.getsource(ReActRunner._compose_initial_prompt)
    assert "build_harness_prompt" in source
    assert "GIGAFORGE_HARNESS_PROFILE" in source


def test_the_react_json_contract_survives_the_harness_prompt() -> None:
    """The harness prompt is prepended, never substituted.

    ``_REACT_PROMPT`` carries the JSON step contract the loop parses.  If the
    harness prompt replaced it, every step would fail to parse and the episode
    would end with an empty plan.
    """
    import inspect

    from gigaforge.agents.react import ReActRunner

    source = inspect.getsource(ReActRunner._compose_initial_prompt)
    assert "_REACT_PROMPT" in source
    assert "harness," in source


# ── think middleware ─────────────────────────────────────────────────────────
def test_think_tool_is_installed_by_the_guards() -> None:
    from gigaforge.agents.guards import ReActGuards
    from gigaforge.agents.react import ToolRegistry

    registry = ToolRegistry()
    guards = ReActGuards(workspace=None)
    guards.install(registry)
    assert "think" in registry.names()


# ── loop breaker ─────────────────────────────────────────────────────────────
def test_loop_breaker_stops_a_repeated_failing_call() -> None:
    """Three identical failing calls in a row must produce a nudge."""
    from gigaforge.core.loop_breaker import LoopBreaker

    messages: list[dict] = [{"role": "user", "content": "goal"}]
    for _ in range(3):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "grep", "arguments": "{}"}}
                ],
            }
        )
        messages.append({"role": "tool", "content": "ERROR: no such file", "tool_call_id": "c1"})

    signal = LoopBreaker().inspect(messages)
    assert signal is not None
    assert signal.kind == "repeated-failure"


# ── context management ───────────────────────────────────────────────────────
def test_compaction_triggers_at_85_percent() -> None:
    from gigaforge.core.context_window_guard import DEFAULT_TRIGGER_FRACTION

    assert DEFAULT_TRIGGER_FRACTION == 0.85


def test_stale_large_file_outputs_are_trimmed() -> None:
    """The biggest consumer of a long trajectory is stale file text."""
    from gigaforge.harness.compaction import TRIM_MARKER, trim_stale_file_outputs

    big = "x" * 10_000
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "goal"}]
    messages += [{"role": "user", "content": f"Observation (file):\n{big}"} for _ in range(4)]
    messages += [{"role": "assistant", "content": "recent"}] * 6

    trimmed = trim_stale_file_outputs(messages, keep_tail=6)
    stale = trimmed[2]
    assert TRIM_MARKER in stale["content"]
    assert len(stale["content"]) < 2_000
    # The tail is what the model is reasoning about: never touched.
    assert trimmed[-1]["content"] == "recent"


def test_trimming_is_idempotent() -> None:
    from gigaforge.harness.compaction import trim_stale_file_outputs

    big = "y" * 10_000
    messages = [{"role": "user", "content": big}] * 8
    once = trim_stale_file_outputs(messages, keep_tail=2)
    twice = trim_stale_file_outputs(once, keep_tail=2)
    assert once == twice


def test_trimming_never_touches_a_short_trajectory() -> None:
    from gigaforge.harness.compaction import trim_stale_file_outputs

    messages = [{"role": "user", "content": "z" * 10_000}]
    assert trim_stale_file_outputs(messages, keep_tail=6) == messages


def test_the_runner_trims_before_it_compacts() -> None:
    """Cheapest pass first: keep the reasoning, drop the file bodies."""
    import inspect

    from gigaforge.agents.react import ReActRunner

    source = inspect.getsource(ReActRunner._compact)
    assert "trim_stale_file_outputs" in source
    assert source.index("trim_stale_file_outputs") < source.index("compact_trajectory(")


# ── PathNormalizer ───────────────────────────────────────────────────────────
def test_path_normalizer_makes_workspace_paths_relative() -> None:
    from gigaforge.agents.output_guards import normalize_paths

    text = "matches:\n/src/gigaforge/core/pipeline.py:42: def run\n/tests/test_x.py:1: import"
    fixed = normalize_paths(text)
    assert "src/gigaforge/core/pipeline.py:42" in fixed
    assert "/src/gigaforge" not in fixed


def test_path_normalizer_leaves_urls_alone() -> None:
    from gigaforge.agents.output_guards import normalize_paths

    text = "see https://example.com/a/b.py for details"
    assert normalize_paths(text) == text


def test_path_normalizer_leaves_prose_alone() -> None:
    from gigaforge.agents.output_guards import normalize_paths

    text = "the ratio a/b and the path /tmp are not files"
    assert normalize_paths(text) == text


def test_path_normalizer_is_applied_to_observations() -> None:
    import inspect

    from gigaforge.agents.react import ReActRunner

    source = inspect.getsource(ReActRunner)
    assert "normalize_observation" in source


# ── DeterministicOutput ──────────────────────────────────────────────────────
class _Step:
    def __init__(self, tool: str, args: dict | None = None) -> None:
        self.tool = tool
        self.args = args or {}


def test_deterministic_output_blocks_a_guessed_number() -> None:
    from gigaforge.agents.output_guards import DeterministicOutput

    verdict = DeterministicOutput().check([_Step("edit_function", {"value": "42"})])
    assert not verdict.ok
    assert "42" in verdict.blocked[0]


def test_deterministic_output_allows_a_number_after_a_read() -> None:
    from gigaforge.agents.output_guards import DeterministicOutput

    verdict = DeterministicOutput().check(
        [_Step("grep", {"query": "count"}), _Step("edit_function", {"value": "42"})]
    )
    assert verdict.ok


def test_deterministic_output_ignores_non_numeric_args() -> None:
    from gigaforge.agents.output_guards import DeterministicOutput

    verdict = DeterministicOutput().check([_Step("edit_function", {"name": "parse"})])
    assert verdict.ok


def test_deterministic_output_is_wired_into_the_plan_guard() -> None:
    import inspect

    from gigaforge.core.strategies import ReActPlan

    source = inspect.getsource(ReActPlan._guarded_steps)
    assert "check_plan" in source


# ── the guards report what they are ──────────────────────────────────────────
def test_guards_describe_includes_the_ported_components() -> None:
    from gigaforge.agents.guards import ReActGuards

    described = ReActGuards(workspace=None).describe()
    assert "path_normalizer" in described
    assert "deterministic_output" in described
    assert "think_tool" in described

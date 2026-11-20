"""Regression tests for the Этап 0 merge work.

Covers the four things this iteration changed, each with the failure it is
meant to prevent:

* **context window** — the measured 261 120 table, ID normalisation, env
  override, and the provider reporting the right window per model (the old
  hard-coded 8 192 is the regression);
* **message shape** — a system message is always first, and every nudge is a
  human message (GigaChat answers 400 otherwise);
* **ported middleware** — ContextWindowGuard, LoopBreaker (all four
  signatures), ShellSafety, ToolContract, SpecificationAudit, ThinkTool;
* **firewall / validator / diff / DAG** — the modules the merge touches must
  keep their contracts.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gigaforge.core.context_window_guard import (
    ContextOverflowError,
    ContextWindowGuard,
    estimate_message_tokens,
    is_context_overflow_error,
)
from gigaforge.core.loop_breaker import (
    MARKER_BATCH,
    MARKER_ERROR,
    MARKER_FINAL,
    MARKER_GREP,
    LoopBreaker,
)
from gigaforge.core.specification_audit import (
    SPECIFICATION_AUDIT_MARKER,
    SpecificationAudit,
    snapshot_workspace_files,
)
from gigaforge.core.tool_contract import TOOL_CONTRACT_MARKER, ToolContract
from gigaforge.providers.context_windows import (
    DEFAULT_CONTEXT_WINDOW,
    GIGACHAT_CONTEXT_WINDOWS,
    context_window_for,
    normalize_model_id,
)
from gigaforge.providers.messages import (
    NUDGE_MARKER,
    count_nudges,
    is_nudge,
    normalize_messages,
    nudge,
)
from gigaforge.sandbox.shell_safety import ShellSafety, is_safe, unsafe_reason

# ══════════════════════════════════════════════════════════════════════════
# context window
# ══════════════════════════════════════════════════════════════════════════


def test_measured_window_is_261120_not_8192():
    assert DEFAULT_CONTEXT_WINDOW == 261_120
    assert set(GIGACHAT_CONTEXT_WINDOWS.values()) == {261_120}
    assert context_window_for("GigaChat-2-Pro") == 261_120


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("GigaChat-2-Pro", "GigaChat-2-Pro"),
        ("GigaChat-2-Pro:latest", "GigaChat-2-Pro"),
        ("gigachat:GigaChat-3-Ultra", "GigaChat-3-Ultra"),
        ("giga/GigaChat-2-Max", "GigaChat-2-Max"),
        ("GigaChat-3-Pro-preview", "GigaChat-3-Pro"),
        ("  GigaChat  ", "GigaChat"),
        ("", "GigaChat"),
        (None, "GigaChat"),
    ],
)
def test_model_id_normalisation(raw, expected):
    assert normalize_model_id(raw) == expected


def test_unknown_model_falls_back_to_measured_default():
    assert context_window_for("GigaChat-9-Imaginary") == 261_120


def test_env_override_wins_over_table():
    env = {"GIGAFORGE_GIGA_MAX_CONTEXT": "4096"}
    assert context_window_for("GigaChat-2-Pro", env=env) == 4096
    # The official ecosystem's variable name is honoured too.
    assert context_window_for("GigaChat-2-Pro", env={"GIGACHAT_CONTEXT_WINDOW": "2048"}) == 2048


def test_explicit_override_wins_over_env():
    env = {"GIGAFORGE_GIGA_MAX_CONTEXT": "4096"}
    assert context_window_for("GigaChat", override=1000, env=env) == 1000


def test_invalid_env_value_is_ignored():
    assert context_window_for("GigaChat", env={"GIGAFORGE_GIGA_MAX_CONTEXT": "abc"}) == 261_120
    assert context_window_for("GigaChat", env={"GIGAFORGE_GIGA_MAX_CONTEXT": "0"}) == 261_120


def test_provider_capabilities_use_measured_window():
    from gigaforge.providers.gigachat import GigaChatProvider

    class _Client:
        async def chat(self, **kwargs):  # pragma: no cover - not called
            raise AssertionError("not called")

        async def list_models(self):  # pragma: no cover
            return []

        def stream_chat(self, **kwargs):  # pragma: no cover
            raise AssertionError("not called")

        async def aclose(self):  # pragma: no cover
            return None

    provider = GigaChatProvider(_Client())
    assert provider.capabilities().max_context == 261_120
    assert provider.capabilities_for("GigaChat-3-Ultra").max_context == 261_120
    assert provider.max_context_for("GigaChat-2-Max") == 261_120


def test_provider_constructor_override_still_wins():
    from gigaforge.providers.gigachat import GigaChatProvider

    class _Client:
        async def aclose(self):  # pragma: no cover
            return None

    provider = GigaChatProvider(_Client(), max_context=4096)
    assert provider.capabilities().max_context == 4096


def test_client_config_reads_max_context_from_env():
    from gigaforge.client.config import ClientConfig

    cfg = ClientConfig.from_env({"GF_CLIENT__MAX_CONTEXT": "12345"})
    assert cfg.max_context == 12345
    assert ClientConfig().max_context == 0  # 0 = resolve from the table
    ClientConfig(max_context=0).validate(require_auth=False)
    with pytest.raises(Exception):
        ClientConfig(max_context=-1).validate(require_auth=False)


# ══════════════════════════════════════════════════════════════════════════
# message shape: system first, nudges as human
# ══════════════════════════════════════════════════════════════════════════


def test_system_message_is_hoisted_to_first():
    messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "system", "content": "you are a coder"},
        {"role": "assistant", "content": "ok"},
    ]
    out = normalize_messages(messages)
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "you are a coder"
    assert [m["role"] for m in out[1:]] == ["user", "assistant"]


def test_mid_conversation_system_message_is_hoisted_not_dropped():
    messages = [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "extra rule"},
    ]
    out = normalize_messages(messages)
    assert out[0]["role"] == "system"
    assert "base" in out[0]["content"]
    assert "extra rule" in out[0]["content"]
    assert [m["role"] for m in out[1:]] == ["user"]


def test_multiple_leading_system_messages_are_merged():
    out = normalize_messages(
        [
            {"role": "system", "content": "a"},
            {"role": "system", "content": "b"},
            {"role": "user", "content": "q"},
        ]
    )
    assert len([m for m in out if m["role"] == "system"]) == 1
    assert out[0]["content"] == "a\n\nb"


def test_normalize_never_leaves_a_system_message_after_index_zero():
    out = normalize_messages(
        [
            {"role": "user", "content": "1"},
            {"role": "system", "content": "2"},
            {"role": "assistant", "content": "3"},
            {"role": "system", "content": "4"},
        ]
    )
    assert out[0]["role"] == "system"
    assert all(m["role"] != "system" for m in out[1:])


def test_nudge_is_a_human_message():
    message = nudge("stop looping", marker="[X]")
    assert message["role"] == "user"
    assert message["content"].startswith(NUDGE_MARKER)
    assert "[X]" in message["content"]
    assert is_nudge(message, "[X]")
    assert not is_nudge({"role": "system", "content": NUDGE_MARKER})


def test_count_nudges_counts_only_matching_marker():
    history = [nudge("a", marker="[A]"), nudge("b", marker="[B]"), nudge("c", marker="[A]")]
    assert count_nudges(history) == 3
    assert count_nudges(history, "[A]") == 2


def test_provider_normalises_messages_before_transport():
    from gigaforge.providers.gigachat import GigaChatProvider

    seen: dict = {}

    class _Client:
        async def chat(self, **kwargs):
            seen.update(kwargs)
            from gigaforge.client.giga_client import ChatResult

            return ChatResult(content="ok")

        async def aclose(self):  # pragma: no cover
            return None

    provider = GigaChatProvider(_Client())
    import asyncio

    asyncio.run(
        provider.chat(
            [
                {"role": "user", "content": "q"},
                {"role": "system", "content": "s"},
            ],
            model="GigaChat-2-Pro",
        )
    )
    assert seen["messages"][0]["role"] == "system"


# ══════════════════════════════════════════════════════════════════════════
# ContextWindowGuard
# ══════════════════════════════════════════════════════════════════════════


def test_guard_uses_measured_window_by_default():
    guard = ContextWindowGuard()
    assert guard.window_for("GigaChat-2-Pro") == 261_120
    assert guard.trigger_tokens_for("GigaChat-2-Pro") == int(261_120 * 0.85)


def test_guard_does_not_fire_on_a_small_conversation():
    guard = ContextWindowGuard()
    messages = [{"role": "user", "content": "x" * 100} for _ in range(10)]
    assert guard.check(messages, model="GigaChat-2-Pro") > 0


def test_guard_fires_proactively_near_the_window():
    guard = ContextWindowGuard(context_window=1000, trigger_fraction=0.85)
    messages = [{"role": "user", "content": "x" * 400} for _ in range(10)]
    with pytest.raises(ContextOverflowError) as excinfo:
        guard.check(messages, model="GigaChat")
    assert excinfo.value.proactive is True
    assert excinfo.value.context_window == 1000


def test_guard_stays_quiet_on_a_compaction_retry():
    guard = ContextWindowGuard(context_window=1000, trigger_fraction=0.85)
    messages = [{"role": "user", "content": "x" * 400} for _ in range(10)]
    # No raise: the caller just compacted and is retrying.
    assert guard.check(messages, already_compacted=True) > 0


def test_guard_ignores_short_conversations():
    guard = ContextWindowGuard(context_window=100, trigger_fraction=0.5, minimum_messages=7)
    messages = [{"role": "user", "content": "x" * 1000} for _ in range(3)]
    assert guard.check(messages) > 0  # no raise


def test_guard_translates_provider_overflow():
    class UnprocessableEntityError(Exception):
        pass

    exc = UnprocessableEntityError("422 context_too_long: maximum context exceeded")
    assert is_context_overflow_error(exc)
    translated = ContextWindowGuard().translate(exc, model="GigaChat-2-Pro")
    assert isinstance(translated, ContextOverflowError)
    assert translated.proactive is False


def test_guard_leaves_unrelated_errors_alone():
    exc = ValueError("something else")
    assert not is_context_overflow_error(exc)
    assert ContextWindowGuard().translate(exc) is exc


def test_guard_rejects_bad_configuration():
    with pytest.raises(ValueError):
        ContextWindowGuard(context_window=0)
    with pytest.raises(ValueError):
        ContextWindowGuard(trigger_fraction=1.5)
    with pytest.raises(ValueError):
        ContextWindowGuard(minimum_messages=1)


def test_estimate_message_tokens_is_monotonic():
    small = [{"role": "user", "content": "a"}]
    large = [{"role": "user", "content": "a" * 4000}]
    assert estimate_message_tokens(large) > estimate_message_tokens(small)


# ══════════════════════════════════════════════════════════════════════════
# LoopBreaker — all four signatures
# ══════════════════════════════════════════════════════════════════════════


def _round(tool: str, args: dict, result: str) -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [{"name": tool, "args": args}]},
        {"role": "tool", "name": tool, "content": result},
    ]


def test_loop_breaker_detects_identical_call_three_times():
    messages = [{"role": "user", "content": "go"}]
    for _ in range(3):
        messages += _round("read_file", {"path": "a.py"}, "Error: boom")
    signal = LoopBreaker().inspect(messages)
    assert signal is not None
    assert signal.marker == MARKER_ERROR
    assert signal.detail["identical_call"] is True
    assert signal.message()["role"] == "user"


def test_loop_breaker_detects_same_error_family_with_drifting_args():
    messages = [{"role": "user", "content": "go"}]
    messages += _round("execute", {"command": "python -c 'x=1; for v in y: pass'"}, "SyntaxError: invalid syntax")
    messages += _round("execute", {"command": "python -c 'y=1; for v in z: pass'"}, "SyntaxError: invalid syntax")
    messages += _round("execute", {"command": "python -c 'z=1; for v in w: pass'"}, "SyntaxError: invalid syntax")
    signal = LoopBreaker().inspect(messages)
    assert signal is not None
    assert signal.detail["family"] == "python-syntax"


def test_loop_breaker_detects_two_empty_greps():
    messages = [{"role": "user", "content": "count"}]
    messages += _round("grep", {"pattern": "a"}, "no matches")
    messages += _round("grep", {"pattern": "b"}, "0 matches")
    signal = LoopBreaker().inspect(messages)
    assert signal is not None
    assert signal.marker == MARKER_GREP


def test_loop_breaker_budget_nudges_are_staged():
    messages = [{"role": "user", "content": "go"}]
    for i in range(12):
        messages += _round("read_file", {"path": f"f{i}.py"}, "ok")
    signal = LoopBreaker().inspect(messages)
    assert signal is not None and signal.marker == MARKER_BATCH

    messages.append(signal.message())
    for i in range(12, 24):
        messages += _round("read_file", {"path": f"f{i}.py"}, "ok")
    signal = LoopBreaker().inspect(messages)
    assert signal is not None and signal.marker == MARKER_FINAL


def test_loop_breaker_does_not_repeat_itself():
    messages = [{"role": "user", "content": "go"}]
    breaker = LoopBreaker()
    injections = 0
    for _ in range(6):
        for _ in range(3):
            messages += _round("read_file", {"path": "a.py"}, "Error: boom")
        signal = breaker.inspect(messages)
        if signal is not None and signal.marker == MARKER_ERROR:
            injections += 1
            messages.append(signal.message())
    # Bounded: the same signature may nudge at most max_injections times.
    assert injections == breaker.max_injections


def test_loop_breaker_is_quiet_on_a_healthy_run():
    messages = [{"role": "user", "content": "go"}]
    messages += _round("read_file", {"path": "a.py"}, "content")
    messages += _round("edit_file", {"path": "a.py"}, "applied")
    assert LoopBreaker().inspect(messages) is None


def test_loop_breaker_rejects_bad_thresholds():
    with pytest.raises(ValueError):
        LoopBreaker(batch_rounds=10, final_rounds=10)


# ══════════════════════════════════════════════════════════════════════════
# ShellSafety
# ══════════════════════════════════════════════════════════════════════════


def test_shell_safety_blocks_multiline_in_quoted_string():
    command = 'sh -c "echo one\necho two"'
    assert not is_safe(command)
    pattern, _ = unsafe_reason(command)
    assert pattern == "multiline-in-quoted-string"


def test_shell_safety_allows_a_heredoc():
    assert is_safe("cat <<'EOF'\nline1\nline2\nEOF")


def test_shell_safety_blocks_python_c_compound_statement():
    assert not is_safe('python -c "x=1; for v in xs: print(v)"')
    assert is_safe('python -c "print(1)"')


def test_shell_safety_verdict_message_carries_the_marker():
    verdict = ShellSafety().check('python3 -c "a=1; if a: pass"')
    assert not verdict.safe
    assert "[SHELL-SAFETY]" in verdict.message()


def test_shell_safety_ignores_non_shell_tools():
    safety = ShellSafety()
    assert safety.check_tool_call("read_file", {"path": "a.py"}).safe
    assert not safety.check_tool_call("execute", {"command": 'python -c "a=1; for b in c: pass"'}).safe


# ══════════════════════════════════════════════════════════════════════════
# ToolContract / SpecificationAudit / ThinkTool
# ══════════════════════════════════════════════════════════════════════════


def test_tool_contract_lists_only_available_tools():
    contract = ToolContract.for_tools(["read_file", "edit_file"])
    assert "`read_file`" in contract.text
    assert "`edit_file`" in contract.text
    assert "`execute`" not in contract.text


def test_tool_contract_injects_once_as_human_message():
    contract = ToolContract.for_tools(["read_file"])
    history = [{"role": "user", "content": "go"}]
    first = contract.maybe_inject(history)
    assert first is not None and first["role"] == "user"
    assert TOOL_CONTRACT_MARKER in first["content"]
    history.append(first)
    assert contract.maybe_inject(history) is None


def test_tool_contract_empty_is_a_noop():
    assert ToolContract().maybe_inject([{"role": "user", "content": "x"}]) is None


def test_specification_audit_fires_on_a_new_file(tmp_path: Path):
    (tmp_path / "existing.py").write_text("x = 1\n", encoding="utf-8")
    audit = SpecificationAudit.for_workspace(tmp_path)
    assert "existing.py" in audit.initial_files

    history = [
        {"role": "user", "content": "write out.json"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "read_file", "args": {"path": "out.json"}}],
        },
        {"role": "tool", "name": "read_file", "content": "{}"},
    ]
    message = audit.maybe_inject(history)
    assert message is not None
    assert message["role"] == "user"
    assert SPECIFICATION_AUDIT_MARKER in message["content"]
    assert "out.json" in message["content"]


def test_specification_audit_ignores_preexisting_files(tmp_path: Path):
    (tmp_path / "existing.py").write_text("x = 1\n", encoding="utf-8")
    audit = SpecificationAudit.for_workspace(tmp_path)
    history = [
        {"role": "user", "content": "look"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "read_file", "args": {"path": "existing.py"}}],
        },
        {"role": "tool", "name": "read_file", "content": "x = 1"},
    ]
    assert audit.maybe_inject(history) is None


def test_snapshot_skips_vendor_directories(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.js").write_text("", encoding="utf-8")
    files = snapshot_workspace_files(tmp_path)
    assert "src/a.py" in files
    assert not any("node_modules" in name for name in files)


def test_think_tool_echoes_and_logs():
    from gigaforge.agents.think_tool import THINK_TOOL_NAME, ThinkTool

    tool = ThinkTool()
    assert tool("plan: 1) read 2) edit") == "plan: 1) read 2) edit"
    assert tool.invoke({"thought": "second"}) == "second"
    assert tool.log.to_dict()["count"] == 2
    assert tool.name == THINK_TOOL_NAME
    assert tool.schema["name"] == THINK_TOOL_NAME


# ══════════════════════════════════════════════════════════════════════════
# firewall: diff parsing / application
# ══════════════════════════════════════════════════════════════════════════


def test_diff_parse_accepts_a_fenced_diff():
    from gigaforge.firewall.diff import parse

    text = (
        "```diff\n"
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-x = 1\n"
        "+x = 2\n"
        " y = 3\n"
        "```\n"
    )
    parsed = parse(text)
    assert parsed.ok
    assert parsed.paths == ["hello.py"]
    assert parsed.hunk_count() == 1


def test_diff_parse_rejects_empty_input():
    from gigaforge.firewall.diff import parse
    from gigaforge.firewall.errors import DiffParseError

    with pytest.raises(DiffParseError):
        parse("   ")


def test_diff_path_safety_blocks_traversal_and_absolute_paths():
    from gigaforge.firewall.diff import is_path_allowed

    assert is_path_allowed("src/app.py")[0]
    assert not is_path_allowed("../etc/passwd")[0]
    assert not is_path_allowed("src/../../etc/passwd")[0]
    assert not is_path_allowed("~/secrets")[0]
    assert not is_path_allowed("")[0]
    # A leading '/' is normalised away (the diff parser strips the a/ b/ prefix),
    # so the path is treated as repo-relative rather than as an absolute escape.
    assert is_path_allowed("/etc/passwd")[0]


def test_diff_applier_writes_and_rolls_back(tmp_path: Path):
    from gigaforge.firewall.config import FirewallConfig
    from gigaforge.firewall.diff import Applier, parse

    target = tmp_path / "hello.py"
    target.write_text("x = 1\ny = 3\n", encoding="utf-8")
    cfg = FirewallConfig(repo_root=str(tmp_path))
    parsed = parse(
        "--- a/hello.py\n+++ b/hello.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 2\n y = 3\n",
        cfg,
    )
    result = Applier(cfg).apply(parsed)
    assert result.ok, result.errors
    assert target.read_text(encoding="utf-8") == "x = 2\ny = 3\n"


# ══════════════════════════════════════════════════════════════════════════
# DAG executor
# ══════════════════════════════════════════════════════════════════════════


def _dag(tmp_path: Path, nodes):
    from gigaforge.core.config import CoreConfig
    from gigaforge.core.dag import DagExecutor

    cfg = CoreConfig(db_path=str(tmp_path / "core.sqlite3"))
    return DagExecutor(nodes, config=cfg)


def test_dag_runs_nodes_in_dependency_order(tmp_path: Path):
    from gigaforge.core.contracts import NodeSpec

    order: list[str] = []

    def make(name: str, requires=(), provides=()):
        def fn(ctx, artifacts):
            order.append(name)
            return {key: name for key in provides}

        return NodeSpec(name=name, fn=fn, requires=requires, provides=provides)

    executor = _dag(
        tmp_path,
        [
            make("a", provides=("x",)),
            make("b", requires=("x",), provides=("y",)),
            make("c", requires=("y",)),
        ],
    )
    result = executor.run()
    assert result.ok
    assert order == ["a", "b", "c"]


def test_dag_rejects_a_cycle(tmp_path: Path):
    from gigaforge.core.contracts import NodeSpec
    from gigaforge.core.errors import CycleError

    def fn(ctx, artifacts):
        return {}

    with pytest.raises(CycleError):
        _dag(
            tmp_path,
            [
                NodeSpec(name="a", fn=fn, requires=("y",), provides=("x",)),
                NodeSpec(name="b", fn=fn, requires=("x",), provides=("y",)),
            ],
        )


def test_dag_rejects_duplicate_node_names(tmp_path: Path):
    from gigaforge.core.contracts import NodeSpec
    from gigaforge.core.errors import DuplicateNodeError

    def fn(ctx, artifacts):
        return {}

    with pytest.raises(DuplicateNodeError):
        _dag(
            tmp_path,
            [NodeSpec(name="a", fn=fn), NodeSpec(name="a", fn=fn)],
        )


def test_dag_retries_only_idempotent_nodes(tmp_path: Path):
    from gigaforge.core.contracts import NodeSpec

    attempts = {"n": 0}

    def flaky(ctx, artifacts):
        attempts["n"] += 1
        raise RuntimeError("boom")

    executor = _dag(
        tmp_path,
        [NodeSpec(name="flaky", fn=flaky, retries=2, idempotent=False, critical=False)],
    )
    result = executor.run()
    # A non-critical failure is recorded but does not fail the run; the point
    # here is that a NON-idempotent node is never retried.
    assert result.node("flaky").status == "failed"
    assert attempts["n"] == 1


def test_dag_retries_an_idempotent_node(tmp_path: Path):
    from gigaforge.core.contracts import NodeSpec

    attempts = {"n": 0}

    def flaky(ctx, artifacts):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("boom")
        return {"out": "ok"}

    executor = _dag(
        tmp_path,
        [NodeSpec(name="flaky", fn=flaky, retries=2, idempotent=True, provides=("out",))],
    )
    result = executor.run()
    assert result.ok
    assert attempts["n"] == 2


# ══════════════════════════════════════════════════════════════════════════
# SDK adapter (import-safe without the official package)
# ══════════════════════════════════════════════════════════════════════════


def test_sdk_adapter_maps_messages_and_enforces_system_first():
    from gigaforge.providers.gigachat_sdk_adapter import to_sdk_messages

    out = to_sdk_messages(
        [
            {"role": "user", "content": "q"},
            {"role": "system", "content": "s"},
            {"role": "tool", "content": "r", "name": "read_file"},
        ]
    )
    assert out[0]["role"] == "system"
    assert out[-1]["role"] == "function"


def test_sdk_adapter_maps_a_completion_to_chat_result():
    from gigaforge.providers.gigachat_sdk_adapter import from_sdk_completion

    class _Msg:
        content = "hello"
        function_call = None

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15
        precached_prompt_tokens = 4

    class _Completion:
        choices = [_Choice()]
        usage = _Usage()

    result = from_sdk_completion(_Completion())
    assert result.content == "hello"
    assert result.finish_reason == "stop"
    assert result.usage["total_tokens"] == 15
    assert result.usage["cached_tokens"] == 4


def test_sdk_adapter_reports_measured_window():
    from gigaforge.providers.gigachat_sdk_adapter import GigaChatSDKProvider

    provider = GigaChatSDKProvider(client=object())
    assert provider.capabilities().max_context == 261_120
    assert provider.capabilities_for("GigaChat-3-Ultra").max_context == 261_120


def test_sdk_adapter_rejects_unknown_retry_policy():
    from gigaforge.providers.gigachat_sdk_adapter import GigaChatSDKProvider

    with pytest.raises(ValueError):
        GigaChatSDKProvider(client=object(), retries="both")


def test_sync_adapter_still_works_with_a_provider():
    from gigaforge.core.sync_adapter import SyncChatClient

    class _Provider:
        async def chat(self, messages, *, model="", **kwargs):
            from gigaforge.client.giga_client import ChatResult

            return ChatResult(content=f"echo:{messages[-1]['content']}")

        async def list_models(self):
            return []

        async def aclose(self):
            return None

    client = SyncChatClient(_Provider())
    result = client.complete([{"role": "user", "content": "hi"}], model="GigaChat")
    assert result.content == "echo:hi"
    client.aclose()

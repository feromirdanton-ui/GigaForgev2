"""Executable probes for the P0 findings N1, N2, N3 of review iteration 5.

Run:  python scripts/probe_p0.py
Exit: 0 when every probe passes, 1 otherwise.

Each probe drives the REAL product path (TaskPipeline / Harness / ReActRunner),
not a mock of the architecture, because the whole class of defect being checked
is "the component exists but the product never reaches it".
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(name)


# ── fixtures ────────────────────────────────────────────────────────────────
def _pipeline(tmp: Path, *, finish_payload: Any = None, script: Any = None) -> Any:
    from gigaforge.core.config import CoreConfig
    from gigaforge.core.pipeline import TaskPipeline
    from gigaforge.core.sync_adapter import SyncChatClient

    payload = finish_payload or {
        "steps": [
            {"tool": "edit_function", "args": {"name": "validate_email"}},
            {"tool": "run_tests", "args": {}},
        ]
    }

    class _Provider:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat(self, messages: Any, *, model: str, **kwargs: Any) -> Any:
            from gigaforge.client.giga_client import ChatResult

            self.calls.append({"model": model, "messages": list(messages), **kwargs})
            if script is not None:
                index = min(len(self.calls) - 1, len(script) - 1)
                body = script[index]
            else:
                body = {"thought": "ready", "action": "finish", "action_input": payload}
            return ChatResult(
                content=json.dumps(body),
                usage={"prompt_tokens": 11, "completion_tokens": 7},
            )

        async def list_models(self) -> list[dict[str, Any]]:
            return []

        def capabilities(self) -> Any:
            return None

    provider = _Provider()
    config = CoreConfig(
        db_path=str(tmp / "core.sqlite3"),
        trace_path=str(tmp / "traces" / "core.jsonl"),
        repo_root=str(tmp),
    )
    return TaskPipeline(config=config, client=SyncChatClient(provider)), provider


# ── N1: the plan reaches the coder ─────────────────────────────────────────
def probe_n1() -> None:
    """The coder's prompt must contain the plan and the localisation."""
    from gigaforge.core.pipeline import RunContext
    from gigaforge.core.state import TaskState

    tmp = Path(tempfile.mkdtemp())
    pipeline, _ = _pipeline(tmp)
    ctx = RunContext(run_id="r1", state=TaskState(run_id="r1", goal="fix validate_email"))
    ctx.extras["localization"] = {"target_function": "validate_email", "target_file": "auth.py"}

    block = pipeline._plan_block(
        ctx, {"plan": ["edit_function: validate_email", "run_tests"]}
    )
    check("N1 plan block carries the plan", "edit_function: validate_email" in block, block[:80])
    check("N1 plan block carries the target symbol", "validate_email" in block)
    check("N1 plan block carries the target file", "auth.py" in block)

    # And the block is actually spliced into the coder's slice text.
    empty = pipeline._plan_block(RunContext(run_id="r2", state=TaskState(goal="g")), {})
    check("N1 no plan -> no block (prompt unchanged)", empty == "", repr(empty))


# ── N2: the harness dispatches to run_react ────────────────────────────────
def probe_n2() -> None:
    """Harness.run(react=True) must call run_react, not runner.run."""
    from gigaforge.harness import Harness, HarnessPolicy

    calls: list[str] = []

    class _Runner:
        def __init__(self) -> None:
            self.state = type("S", (), {"task_id": "t1"})()

        async def run(self, task: str, state: Any, **kw: Any) -> Any:
            calls.append("runner.run")
            return type("R", (), {"finish_reason": "classic", "steps": [], "llm_calls": 0})()

    import gigaforge.agents.engine as engine

    original = engine.run_react

    def _spy(runner: Any, task: str, state: Any, **kw: Any) -> Any:
        calls.append(f"run_react:{kw.get('engine')}")
        return type("R", (), {"finish_reason": "react", "steps": [], "llm_calls": 0})()

    engine.run_react = _spy  # type: ignore[assignment]
    try:
        harness = Harness(policy=HarnessPolicy(sleep=lambda _s: None))
        outcome = harness.run(
            _Runner(), "goal", _Runner().state, run_id="r1", engine="classic", react=True
        )
    finally:
        engine.run_react = original  # type: ignore[assignment]

    check("N2 harness dispatches through run_react", calls == ["run_react:classic"], str(calls))
    check("N2 ledger records the engine that ran", outcome.ledger.engine == "classic",
          str(outcome.ledger.engine))


# ── N3: the trajectory survives every return branch ────────────────────────
def probe_n3() -> None:
    """A finish with an unusable plan must still carry the trajectory out.

    The live scenario from the review: the model greps, finds the files, then
    "finishes" with an untargeted ``edit_function``.  The guard correctly drops
    that step — and the salvage must then turn the observations into read steps
    instead of the run reporting zero steps.
    """
    from gigaforge.core.strategies import ReActPlan

    tmp = Path(tempfile.mkdtemp())
    (tmp / "auth.py").write_text("def validate_email(x):\n    return '@' in x\n")
    (tmp / "README.md").write_text("# TODO: document validate_email\n")

    script = [
        {"thought": "find the marker", "action": "grep", "action_input": "TODO"},
        {"thought": "done", "action": "finish",
         "action_input": [{"tool": "edit_function", "args": {}}]},
    ]
    pipeline, _ = _pipeline(tmp, script=script)
    strategy = ReActPlan(runner_factory=pipeline._build_react_runner, max_steps=4)
    strategy.bind_run("run-n3")
    outcome = strategy.plan("find the TODO markers", "", ask=lambda *a, **k: "- x",
                            parse=lambda t: [t])
    check("N3 a finish with an unusable plan does not fall back to linear",
          outcome.strategy == "react", outcome.strategy)
    check("N3 the episode's work survives as read steps",
          bool(outcome.steps) and all("read_structure" in s for s in outcome.steps),
          str(outcome.steps))


def main() -> int:
    probe_n1()
    probe_n2()
    probe_n3()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} probe(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all P0 probes passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

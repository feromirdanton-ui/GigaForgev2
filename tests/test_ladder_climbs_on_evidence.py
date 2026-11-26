"""The ladder must actually climb: a repeated failure buys a different model.

**The defect this pins.**  ``_ask`` read ``phase_attempts``/``phase_repeats``
from ``ctx.extras`` and nothing in the repository ever wrote them.  The ladder
was asked "how many times has this failed?" and always heard "zero", so
escalation was physically impossible — a scale with no drive.  The test that
existed asserted that ``decide_level`` *was called*, which is green on a
completely dead escalation.

These tests assert the observable consequence instead: the model the provider
is asked for changes.  A test that counts calls cannot see a dead ladder; a
test that reads the model name can.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.config import CoreConfig  # noqa: E402
from gigaforge.core.contracts import RunContext  # noqa: E402
from gigaforge.core.ladder import Level  # noqa: E402
from gigaforge.core.router import ModelRouter  # noqa: E402
from gigaforge.core.state import TaskState  # noqa: E402


class _RecordingClient:
    """A client that records the model of every call and answers with text."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return _Reply("ok")

    def complete(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return _Reply("ok")


class _Reply:
    def __init__(self, text: str) -> None:
        self.content = text
        self.text = text
        self.usage = None
        self.model = ""


def _router(**overrides: Any) -> tuple[ModelRouter, _RecordingClient]:
    cfg = CoreConfig(
        **{
            "cheap_model": "cheap-model",
            "strong_model": "strong-model",
            "flagship_model": "flagship-model",
            **overrides,
        }
    )
    client = _RecordingClient()
    return ModelRouter(client=client, config=cfg), client


def _ctx() -> RunContext:
    return RunContext(run_id="r1", state=TaskState(run_id="r1", goal="do the thing"))


def test_the_first_call_of_a_phase_is_attempt_zero() -> None:
    """``attempt`` counts *prior* calls, so the threshold means what it says."""
    router, _ = _router()
    ctx = _ctx()
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.router = router

    first, repeats_first = pipeline._note_phase_attempt(ctx, "code", "prompt A")
    second, repeats_second = pipeline._note_phase_attempt(ctx, "code", "prompt A")
    third, repeats_third = pipeline._note_phase_attempt(ctx, "code", "prompt B")

    assert (first, repeats_first) == (0, 0)
    assert (second, repeats_second) == (1, 1)
    # A different prompt is a *new question*, not a third attempt at the old
    # one.  The counter used to be ``attempts[phase] += 1``, which made a
    # ten-section document escalate every section after the second to the
    # flagship model although none had failed.  Keying by (phase, prompt) is
    # what makes "attempt" mean "attempt at this question".
    assert (third, repeats_third) == (0, 0)


def test_two_identical_failures_escalate_to_a_different_model() -> None:
    """The behavioural claim: the third call is served by another model.

    This is the assertion the old test could not make.  It reads the model the
    provider was asked for, not whether the ladder was consulted.
    """
    router, client = _router()
    ctx = _ctx()
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.router = router
    pipeline._event = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline._degraded = lambda *a, **k: None  # type: ignore[method-assign]

    # Three calls with the same prompt: the same question against the same
    # context, which is exactly the "repeated identical failure" the ladder
    # escalates on.
    for _ in range(3):
        pipeline._ask("code", "the same prompt", ctx)

    models = [call.get("model") for call in client.calls]
    assert len(models) == 3
    assert models[0] == "strong-model", models
    # By the third call the ladder has seen two identical repeats and must have
    # moved up a rung — which is only visible if the model changed.
    assert models[2] != models[0], (
        "the ladder did not climb: the third identical call was served by the "
        f"same model as the first ({models})"
    )
    assert models[2] == "flagship-model", models


def test_a_different_prompt_does_not_escalate() -> None:
    """New information is a new attempt, not a repeat."""
    router, client = _router()
    ctx = _ctx()
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.router = router
    pipeline._event = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline._degraded = lambda *a, **k: None  # type: ignore[method-assign]

    for index in range(3):
        pipeline._ask("code", f"prompt {index}", ctx)

    models = [call.get("model") for call in client.calls]
    assert models[0] == "strong-model", models
    # Attempt count alone escalates at 2, so the third call may move up; what
    # must NOT happen is escalation on the second call, which is a fresh prompt.
    assert models[1] == "strong-model", models


def test_l2_and_l3_are_different_models_on_every_writing_phase() -> None:
    """A rung that resolves to the same model is not a rung.

    Measured on the previous build: L2 and L3 both resolved to ``GigaChat-Pro``
    on plan/code/fix/react, so escalating a step that had failed twice bought
    the model that had just failed twice.
    """
    router, _ = _router()
    for phase in ("plan", "code", "fix", "react", "review", "summarize"):
        l2 = router._model_for_level(phase, Level.L2_STRONG)
        l3 = router._model_for_level(phase, Level.L3_FLAGSHIP)
        assert l3 == "flagship-model", (phase, l3)
        assert l3 != l2, f"{phase}: L2 and L3 resolve to the same model ({l2})"


def test_no_flagship_configured_means_no_pretend_escalation() -> None:
    """With no flagship, L3 resolves to nothing rather than to the strong model."""
    router, _ = _router(flagship_model="")
    assert router._model_for_level("code", Level.L3_FLAGSHIP) is None


def test_a_flagship_equal_to_strong_is_not_a_rung() -> None:
    router, _ = _router(flagship_model="strong-model")
    assert router._model_for_level("code", Level.L3_FLAGSHIP) is None


def test_l0_runs_a_registered_handler_without_calling_a_model() -> None:
    """L0 is "no model call", and it must be reachable **from the product**.

    The previous version of this test set ``ctx.extras["deterministic_available"]``
    by hand.  That is the defect it was supposed to catch: nothing in
    ``gigaforge/`` ever wrote that key, so the test created a condition the
    product could not create and then verified that the branch worked.  The
    branch worked; the condition never happened.

    This test registers the handler the way the product does — through
    ``TaskPipeline``'s own registry — and asserts the observable consequence:
    the provider is never called.
    """
    router, client = _router()
    ctx = _ctx()
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.router = router
    pipeline._event = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline._degraded = lambda *a, **k: None  # type: ignore[method-assign]
    # The real registration path, not a hand-written dict.
    pipeline._register_deterministic_handlers()
    pipeline.deterministic_handlers["code"] = lambda prompt, c: "computed"

    assert pipeline._ask("code", "anything", ctx) == "computed"
    assert client.calls == [], "L0 must not call a model"


def test_l0_without_a_handler_degrades_instead_of_failing() -> None:
    """A ladder bug must not fail the run: record it and buy the model."""
    router, client = _router()
    ctx = _ctx()
    degraded: list[tuple] = []
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.router = router
    pipeline._event = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline._degraded = lambda *a, **k: degraded.append(a)  # type: ignore[method-assign]
    # A handler that claims the phase but cannot serve this input: the ladder
    # says L0, the registry has nothing, and the run must still finish.
    pipeline.deterministic_handlers = {"code": _DecliningHandler()}

    assert pipeline._ask("code", "anything", ctx) == "ok"
    assert len(client.calls) == 1


class _DecliningHandler:
    """A handler that exists but refuses this input."""

    @staticmethod
    def can_serve(prompt: str, ctx: Any) -> bool:
        return False

    def __call__(self, prompt: str, ctx: Any) -> str:  # pragma: no cover - never run
        raise AssertionError("a declining handler must not be called")


def test_a_declining_handler_does_not_claim_the_phase() -> None:
    """``can_serve`` is what keeps a registry entry from becoming a wrong answer.

    ``summarize`` is deterministic for a diff summary and *not* for an answer
    goal, where the summary is the deliverable.  Without the decline, wiring the
    registry would have replaced a real answer with a computed string.
    """
    router, client = _router()
    ctx = _ctx()
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.router = router
    pipeline._event = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline._degraded = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline._register_deterministic_handlers()

    # An answer goal: the summary IS the answer, so the handler must decline.
    ctx.extras["answer_text"] = "2 + 2 equals 4."
    assert pipeline._deterministic_handler("summarize", "p", ctx) is None

    # A diff summary: the harness holds the facts, so it serves.
    ctx.extras.pop("answer_text")
    ctx.extras["final_diff"] = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    ctx.extras["accepted"] = True
    ctx.extras["changed_files"] = ["x"]
    assert pipeline._deterministic_handler("summarize", "p", ctx) is not None


def test_the_summarize_phase_never_calls_a_model_for_an_accepted_diff() -> None:
    """The end-to-end claim: a real run's summary costs zero tokens.

    This is the assertion the review asked for — a router that raises if it is
    ever asked for a model, driven through the actual node, with the run still
    producing a summary.
    """
    from gigaforge.core.pipeline import TaskPipeline

    class _ExplodingRouter:
        """Any model call is a test failure, not a fallback."""

        def __init__(self) -> None:
            self.calls = 0

        def decide_level(self, *a: Any, **k: Any) -> Any:
            from gigaforge.core.ladder import Decision, Level

            return Decision(level=Level.L0_DETERMINISTIC, phase="summarize",
                            reason="test")

        def complete(self, *a: Any, **k: Any) -> Any:
            self.calls += 1
            raise AssertionError("the summarize phase must not buy a model call")

        def route(self, *a: Any, **k: Any) -> Any:
            self.calls += 1
            raise AssertionError("the summarize phase must not buy a model call")

    router = _ExplodingRouter()
    ctx = _ctx()
    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.router = router
    pipeline._event = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline._degraded = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline._register_deterministic_handlers()

    diff = "--- a/index.html\n+++ b/index.html\n@@ -1 +1 @@\n-a\n+b\n"
    out = pipeline._node_summarize(
        ctx, {"final_diff": diff, "accepted": True}
    )

    assert router.calls == 0, "the summarize phase called a model"
    assert "index.html" in out["summary"], out["summary"]
    assert "1 file" in out["summary"], out["summary"]

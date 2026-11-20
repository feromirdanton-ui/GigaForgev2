"""``_event`` must not be called with a name it already owns.

The defect this pins, measured live: the routing event was emitted as

    self._event("pipeline.task_kind", run_id=..., kind=kind.kind, ...)

but ``_event``'s first parameter is *named* ``kind``::

    def _event(self, kind: str, **payload: Any) -> None:

so the call raised ``TypeError: got multiple values for argument 'kind'``.  The
call sat inside ``try: ... except Exception`` that falls through to the code
path, so the exception was swallowed, the sectional generator never ran, and the
run produced a single model answer — the exact "chatbot, not harness" symptom
the whole iteration was meant to remove.  The trace showed it plainly:

    pipeline.generation_plan_error: TaskPipeline._event() got multiple values
    for argument 'kind'

A swallowed exception is the worst kind of bug: the run reports success.  The
guard below is static — it reads the source of every ``_event`` call and refuses
a keyword that collides with the signature — so the class cannot come back
through a new call site.
"""
from __future__ import annotations

import ast
import re
import inspect
from pathlib import Path

import pytest

from gigaforge.core.pipeline import TaskPipeline

RESERVED = {"kind", "self"}


def _event_calls(source: str) -> list[ast.Call]:
    tree = ast.parse(source)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "_event":
            calls.append(node)
    return calls


class TestEventKeywordCollisions:
    def test_no_event_call_uses_a_reserved_keyword(self) -> None:
        source = inspect.getsource(TaskPipeline)
        offenders: list[str] = []
        for call in _event_calls(source):
            for keyword in call.keywords:
                if keyword.arg in RESERVED:
                    offenders.append(f"line {call.lineno}: {keyword.arg}=")
        assert not offenders, (
            "_event's first parameter is named 'kind'; passing kind= raises "
            "TypeError, which the surrounding except swallows: " + "; ".join(offenders)
        )

    def test_the_signature_is_what_the_guard_assumes(self) -> None:
        params = list(inspect.signature(TaskPipeline._event).parameters)
        assert params[:2] == ["self", "kind"], (
            "the guard's RESERVED set is derived from this signature; update both"
        )

    def test_the_routing_event_uses_task_kind(self) -> None:
        source = inspect.getsource(TaskPipeline)
        assert "task_kind=kind.kind" in source
        # ``task_kind=`` contains ``kind=`` as a substring, so the negative
        # check needs a word boundary — otherwise it fails on the correct code.
        assert not re.search(r"(?<![_a-z])kind=kind\.kind", source)


class TestRoutingReachesSectionalGeneration:
    def test_a_page_goal_does_not_fall_through_to_the_coder(self) -> None:
        """The artifact branch must return, not raise into the fall-through."""
        source = inspect.getsource(TaskPipeline._subagent_diff)
        # The event is emitted *before* the return, so a raise there would skip
        # the sectional path entirely.
        assert source.index("subagents_sections") < source.index("_sectional_generation")

    def test_the_fall_through_is_logged(self) -> None:
        source = inspect.getsource(TaskPipeline._subagent_diff)
        assert "generation_plan_error" in source, (
            "a swallowed routing error must at least be visible in the trace"
        )

"""The ReAct loop must deliver the harness's measured prefix to the model.

Measured live: a document goal about the decision ladder produced a confident,
entirely invented description of it ("L0: Initial Assessment") while the brief
— which quoted ``core/ladder.py`` verbatim — sat unread on ``state.prefix``.
The tokens were paid for and never delivered.

The loop serialised only ``serialize_prefix()``, a three-key dict of goal, plan
and slice hash.  ``state.prefix.text`` is where the brief and the skills live.
"""
from __future__ import annotations

from gigaforge.core.state import TaskState, build_system_prefix


def _state_with_prefix() -> TaskState:
    state = TaskState(goal="Explain the decision ladder")
    state.prefix = build_system_prefix(
        repo_map="gigaforge/core/ladder.py",
        tool_contract="",
        skills="",
        extra={"brief": "### Source: `gigaforge/core/ladder.py`\nL0_DETERMINISTIC = 0"},
    )
    return state


def test_the_measured_prefix_reaches_the_react_prompt() -> None:
    from gigaforge.agents.react import ReActRunner

    agent = ReActRunner.__new__(ReActRunner)
    agent._max_steps = 8
    agent._registry = type("R", (), {"names": lambda self: ["grep"]})()
    prompt = agent._compose_initial_prompt("Explain the decision ladder", _state_with_prefix(), None)
    assert "### Source:" in prompt
    assert "L0_DETERMINISTIC" in prompt


def test_a_state_without_a_prefix_still_composes() -> None:
    from gigaforge.agents.react import ReActRunner

    agent = ReActRunner.__new__(ReActRunner)
    agent._max_steps = 8
    agent._registry = type("R", (), {"names": lambda self: ["grep"]})()
    prompt = agent._compose_initial_prompt("Do something", TaskState(goal="Do something"), None)
    assert "Task: Do something" in prompt

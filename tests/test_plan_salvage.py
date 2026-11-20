"""The plan salvage must not invent work.

A live run exposed this: asked to *find* the TODO markers, the ReAct episode
grepped, found them, then emitted an untargeted ``edit_function``.  The plan
guard correctly dropped it — and the salvage, which exists so the episode's work
is not thrown away, turned the observations into "edit these four files".

The episode had never been asked to change anything.  A read step is always true
of an observation; an edit step is a claim the episode never made.
"""
from __future__ import annotations

from gigaforge.agents.react import ReActRunner


def _runner() -> ReActRunner:
    return object.__new__(ReActRunner)


def _observation(payload: str) -> dict[str, str]:
    return {"role": "user", "content": f"Observation: {payload}"}


def test_salvage_emits_read_steps_not_edits() -> None:
    trajectory = [
        _observation('{"matches": [{"file": "a.py", "line": 3}, {"file": "b.py", "line": 9}]}')
    ]
    steps = _runner().synthesize_plan(trajectory)
    assert steps, "the observations named files, so a plan is derivable"
    assert {s.tool for s in steps} == {"read_structure"}, (
        "salvage must not claim an edit the episode never decided on"
    )
    assert {s.args["path"] for s in steps} == {"a.py", "b.py"}


def test_salvage_is_empty_when_nothing_was_observed() -> None:
    """No observations means no plan — report max_steps honestly."""
    assert _runner().synthesize_plan([]) == []
    assert _runner().synthesize_plan([{"role": "assistant", "content": "thinking"}]) == []


def test_salvage_deduplicates_and_caps() -> None:
    rows = ", ".join(f'{{"file": "f{i}.py", "line": 1}}' for i in range(12))
    steps = _runner().synthesize_plan([_observation(f'{{"matches": [{rows}]}}')])
    paths = [s.args["path"] for s in steps]
    assert len(paths) == len(set(paths)), "duplicate files must collapse"
    assert len(paths) <= 5, "a salvaged plan stays small"


def test_salvage_ignores_non_observation_messages() -> None:
    trajectory = [
        {"role": "assistant", "content": 'Thought: I should edit {"file": "x.py"}'},
        _observation('{"matches": [{"file": "real.py", "line": 1}]}'),
    ]
    steps = _runner().synthesize_plan(trajectory)
    assert [s.args["path"] for s in steps] == ["real.py"]

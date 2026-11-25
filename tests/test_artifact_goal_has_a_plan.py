"""An artifact goal must not be sent to the file-targeting planner.

Measured live on the site goal (HEAD aa5ac71): cycle 1 failed the contract on a
section name, then cycles 2 and 3 failed at ``plan`` with ``{'steps': 0}`` and
the goal stopped with "the same output and failure repeated".  The second
failure was the harness's own: the ReAct planner demands a step naming an
existing path, and a page that does not exist yet has none.

The plan for a page is its sections, which ``plan_generation`` already decides.
"""

from __future__ import annotations

import pytest

from gigaforge.harness.generation import plan_generation


def test_plan_generation_names_the_sections_of_the_live_goal():
    plan = plan_generation(
        "Create a single-file index.html: a futuristic cyberpunk landing page "
        "about the GigaForge agent harness, with a hero, a feature grid of at "
        "least four cards, and a footer"
    )
    kinds = plan.kinds()
    assert kinds, "a page goal must decompose into sections"
    assert "hero" in kinds
    assert "footer" in kinds
    assert plan.path == "index.html"


def test_the_artifact_plan_branch_is_reachable():
    """The branch must exist in the plan node, before the router is required.

    A regression here is silent: the run simply falls through to the ReAct
    planner and dies with ``steps: 0``, which reads like a model failure.
    """
    import inspect

    from gigaforge.core import pipeline as pipeline_module

    source = inspect.getsource(pipeline_module.TaskPipeline._node_plan)
    assert "pipeline.plan_artifact" in source
    assert "plan_generation" in source
    # The artifact branch must come before the router guard, or a run with no
    # router raises instead of planning.
    assert source.index("pipeline.plan_artifact") < source.index(
        "no model client is wired into the pipeline"
    )


@pytest.mark.parametrize(
    "goal",
    [
        "Create a single-file index.html: a cyberpunk landing page about GigaForge",
        "Build a landing page for a bakery with a hero and a footer",
        "Make a website for my bakery",
    ],
)
def test_every_ordinary_page_goal_has_sections(goal):
    plan = plan_generation(goal)
    assert plan.kinds(), f"no sections planned for {goal!r}"


def test_a_chat_answer_is_at_the_top_level_of_the_result():
    """A caller reads ``answer``, not a diff block inside a cycle summary.

    Measured live: "What is 2+2? Answer in chat" completed with
    ``answer: None`` while the sentence sat inside ``cycles[0].summary``.
    """
    from gigaforge.core.goal import GoalResult

    result = GoalResult(
        goal_id="g",
        goal="What is 2+2?",
        status="completed",
        accepted=True,
        answer="2 + 2 equals 4.",
        answer_citations=["https://example.com"],
        answer_searched=True,
    )
    payload = result.to_dict()
    assert payload["answer"] == "2 + 2 equals 4."
    assert payload["answer_citations"] == ["https://example.com"]
    assert payload["answer_searched"] is True


def test_a_goal_without_an_answer_reports_an_empty_string():
    from gigaforge.core.goal import GoalResult

    payload = GoalResult(
        goal_id="g", goal="x", status="completed", accepted=True
    ).to_dict()
    assert payload["answer"] == ""
    assert payload["answer_citations"] == []


def test_the_langgraph_path_carries_the_answer():
    """The graph the product actually runs must not drop the reply.

    Measured live: ``pipeline.answer`` logged 20 chars, ``settle`` accepted,
    and the goal record still had ``answer: None`` — the LangGraph cycle node
    never copied the field into its state.  The classic GoalRunner did.
    """
    import inspect

    from gigaforge.core import langgraph_goal

    source = inspect.getsource(langgraph_goal)
    assert '"answer": str(getattr(result, "answer"' in source
    assert '"answer": str(values.get("answer")' in source
    assert "answer: str" in inspect.getsource(langgraph_goal.GoalState)

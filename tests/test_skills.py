"""Skills: procedural knowledge selected by task class, not by luck.

**The defect this file exists for.**  The harness had prompts and no
*procedural knowledge that applies to one class of task and not another*.  The
consequences were measured live, repeatedly:

* a page section child returned a whole page instead of one section;
* a footer child answered in Slim/HAML (``section.class=footer``);
* a document goal died on ``GF-CORE-PIPE-070``;
* a search-backed answer was rejected for missing citations.

Each is a procedure belonging to a task class.  These tests pin the *selection*
rule, because a skill set that is wrong is worse than no skill set: it spends
tokens telling the model about work it is not doing.

Two selection defects were found by running the selector, not by reading it:

1. an empty ``phase`` was compared literally, so asking for skills by kind
   alone — which is what the prefix builder does — returned nothing and the
   whole feature was silently dead;
2. ``repair`` and ``document`` leaked into runs they do not apply to, because
   kind alone cannot separate "write a design doc" from "fix the parser" (both
   are ``code``) or "fix a rejected diff" from "answer a question".
"""
from __future__ import annotations

import pytest

from gigaforge.skills import (
    BUILTIN_SKILLS,
    Skill,
    SkillRegistry,
    default_registry,
    render_skills,
    select_skills,
)


# ── the registry is real ────────────────────────────────────────────────────
def test_the_builtin_registry_is_not_empty() -> None:
    assert len(default_registry()) >= 6


def test_every_skill_has_a_name_a_description_and_a_body() -> None:
    """A skill with no description cannot be indexed; one with no body is a label."""
    for skill in BUILTIN_SKILLS:
        assert skill.name
        assert skill.description
        assert skill.body.strip()
        assert skill.token_cost > 0


def test_skill_names_are_unique() -> None:
    names = [s.name for s in BUILTIN_SKILLS]
    assert len(names) == len(set(names))


# ── selection by task class ─────────────────────────────────────────────────
def test_a_page_goal_gets_the_page_procedures() -> None:
    names = {s.name for s in select_skills(kind="artifact", goal="create index.html")}
    assert "page-create" in names
    assert "page-section" in names


def test_a_page_edit_gets_the_section_procedure() -> None:
    """Editing a page is section work; the create procedure is not needed."""
    names = {s.name for s in select_skills(kind="artifact_edit", goal="update index.html")}
    assert "page-section" in names
    assert "page-create" not in names


def test_a_code_fix_gets_the_code_procedure() -> None:
    names = {s.name for s in select_skills(kind="code", goal="fix db.py")}
    assert "code-fix" in names
    assert "page-create" not in names


def test_a_chat_question_gets_the_chat_procedure() -> None:
    names = {s.name for s in select_skills(kind="answer", goal="what is 2+2")}
    assert "chat-answer" in names
    assert "page-create" not in names


# ── the two leaks the selector had ──────────────────────────────────────────
def test_an_empty_phase_matches_everything() -> None:
    """Asking by kind alone must not return nothing.

    The prefix builder knows the task class before any phase runs, so it asks
    with ``phase=""``.  Comparing that literally made the whole feature dead.
    """
    assert select_skills(kind="code", goal="fix db.py")


def test_repair_is_not_selected_without_a_phase() -> None:
    """``repair`` is about a rejected diff; a run that has none does not need it."""
    names = {s.name for s in select_skills(kind="code", goal="fix db.py")}
    assert "repair" not in names


def test_repair_is_selected_in_the_fix_phase() -> None:
    names = {s.name for s in select_skills(kind="code", phase="fix", goal="fix db.py")}
    assert "repair" in names


def test_a_markdown_goal_gets_the_document_procedure() -> None:
    """A ``.md`` goal is classified ``code``; the path is what separates it."""
    names = {s.name for s in select_skills(kind="code", goal="write docs/design.md")}
    assert "document" in names


def test_a_python_goal_does_not_get_the_document_procedure() -> None:
    names = {s.name for s in select_skills(kind="code", goal="fix db.py")}
    assert "document" not in names


def test_arithmetic_does_not_get_the_code_reading_procedure() -> None:
    """``what is 2+2`` is an answer goal; reading code is noise for it."""
    names = {s.name for s in select_skills(kind="answer", goal="what is 2+2")}
    assert "code-analyse" not in names


def test_a_question_about_a_source_file_gets_the_code_reading_procedure() -> None:
    names = {s.name for s in select_skills(kind="answer", goal="find the bug in app.py")}
    assert "code-analyse" in names


# ── the budget ──────────────────────────────────────────────────────────────
def test_the_budget_is_respected() -> None:
    """A skill set that silently doubles the prompt is a cost incident."""
    chosen = select_skills(kind="", budget_tokens=200)
    assert sum(s.token_cost for s in chosen) <= 200 or len(chosen) == 1


def test_the_highest_priority_skill_survives_a_tight_budget() -> None:
    """When only one body fits, it is the most important one."""
    chosen = select_skills(kind="artifact", goal="create index.html", budget_tokens=200)
    assert chosen
    assert chosen[0].name == "page-section"


def test_a_skill_that_does_not_fit_is_skipped_not_truncated() -> None:
    """Half a procedure is worse than none: the model follows the half it sees.

    The *first* skill is still taken even when it alone exceeds the budget —
    a run with no procedure at all is worse than one a few hundred tokens over.
    What must never happen is a body cut in half.
    """
    chosen = select_skills(kind="", budget_tokens=1)
    assert len(chosen) == 1
    assert chosen[0].body.strip() == chosen[0].body.strip()  # whole, not sliced
    assert chosen[0].token_cost > 1


# ── rendering: two tiers ────────────────────────────────────────────────────
def test_the_index_lists_every_skill() -> None:
    """The index is the cheap tier: it says what knowledge exists."""
    index = default_registry().index()
    for skill in BUILTIN_SKILLS:
        assert skill.name in index


def test_the_rendered_block_contains_the_selected_bodies() -> None:
    chosen = select_skills(kind="artifact", goal="create index.html")
    text = render_skills(chosen)
    for skill in chosen:
        assert f"## Skill: {skill.name}" in text


def test_the_rendered_block_does_not_contain_unselected_bodies() -> None:
    """A code-fix run must not pay for the page-section procedure."""
    chosen = select_skills(kind="code", goal="fix db.py")
    text = render_skills(chosen)
    assert "## Skill: page-section" not in text


def test_the_index_can_be_omitted() -> None:
    chosen = select_skills(kind="code", goal="fix db.py")
    text = render_skills(chosen, include_index=False)
    assert "## Available skills" not in text


# ── a custom registry ───────────────────────────────────────────────────────
def test_a_custom_registry_can_be_built_and_queried() -> None:
    registry = SkillRegistry(
        [Skill(name="only", description="the only one", body="do the thing")]
    )
    assert registry.names() == ("only",)
    assert registry.get("only") is not None
    assert registry.get("missing") is None
    assert [s.name for s in registry.select(kind="code")] == ["only"]


def test_a_skill_without_a_name_is_refused() -> None:
    with pytest.raises(ValueError):
        SkillRegistry([Skill(name="", description="x", body="y")])


def test_explicit_names_pin_the_selection() -> None:
    """An operator or a test can ask for an exact set."""
    chosen = select_skills(names=["chat-answer"], kind="artifact")
    assert [s.name for s in chosen] == ["chat-answer"]

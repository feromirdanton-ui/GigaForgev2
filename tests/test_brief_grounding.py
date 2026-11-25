"""A document about code must be written from the code, not from its name.

**The defect this file exists for.**  A live goal said:

    Write a concise design note to docs/design.md explaining the GigaForge
    decision ladder: the four levels, when a step escalates, and why L0 exists.

The run reported ``completed | accepted: True`` and produced this:

    - **L0**: Initial Assessment — a baseline where decisions are evaluated for
      their feasibility and alignment with goals.
    - **L1**: Detailed Analysis — decisions requiring more in-depth examination.
    - **L2**: Expert Review — when complexities arise, experts review.
    - **L3**: Final Approval — at this level, decisions are finalized.

None of that is what the ladder does.  L0 is *no model call*.  L1 is a cheap
model.  L3 is a flagship model reached only on evidence.  The model never read
``core/ladder.py``; it invented a plausible ladder from the words in the goal,
and every structural check passed because the document was well-formed and
confident.

That is the most dangerous class of failure this harness can produce: not a
broken artifact, a *wrong* one that looks right.  The harness can prevent it
deterministically — when the goal names a module that exists, quote the module
into the brief.  It costs a few thousand tokens once; the alternative costs a
wrong document, then a second run to replace it.

These tests pin three things: the source is found when the goal names it, the
source is NOT invented when the goal does not, and the quote reaches the
prompt.
"""
from __future__ import annotations

import pytest

from gigaforge.harness.brief import TaskBrief, build_brief, render_brief


@pytest.fixture()
def tree(tmp_path):
    """A small tree with a module whose name a goal can refer to."""
    (tmp_path / "gigaforge" / "core").mkdir(parents=True)
    (tmp_path / "gigaforge" / "core" / "ladder.py").write_text(
        '"""The decision ladder: L0 deterministic, L1 cheap, L2 strong, L3 flagship."""\n'
        "\n"
        "class Level:\n"
        "    L0_DETERMINISTIC = 0\n"
        "    L1_CHEAP = 1\n"
    )
    (tmp_path / "gigaforge" / "core" / "router.py").write_text("class Router: pass\n")
    (tmp_path / "README.md").write_text("# Project\n")
    return tmp_path


# ── the source is found ─────────────────────────────────────────────────────
def test_a_named_module_is_quoted(tree) -> None:
    """The live case: "the decision ladder" -> core/ladder.py."""
    brief = build_brief(
        "Write a design note explaining the GigaForge decision ladder", repo_root=tree
    )
    paths = [p for p, _ in brief.grounding]
    assert "gigaforge/core/ladder.py" in paths


def test_an_explicit_path_is_quoted(tree) -> None:
    brief = build_brief(
        "Explain gigaforge/core/router.py in a note", repo_root=tree
    )
    paths = [p for p, _ in brief.grounding]
    assert "gigaforge/core/router.py" in paths


def test_the_quote_is_verbatim(tree) -> None:
    """A paraphrase is a second source of truth; the quote must be the file."""
    brief = build_brief("Explain the decision ladder", repo_root=tree)
    source = dict(brief.grounding)["gigaforge/core/ladder.py"]
    assert "L0_DETERMINISTIC = 0" in source


# ── the source is not invented ──────────────────────────────────────────────
def test_a_goal_naming_nothing_quotes_nothing(tree) -> None:
    """A guess at "the relevant file" is how a brief starts quoting the wrong one."""
    brief = build_brief("Write a poem about the sea", repo_root=tree)
    assert brief.grounding == ()


def test_a_missing_module_is_not_quoted(tree) -> None:
    brief = build_brief("Explain the quantum scheduler", repo_root=tree)
    assert brief.grounding == ()


def test_the_quote_is_capped(tree) -> None:
    """A brief is context, not a repository dump."""
    (tree / "gigaforge" / "core" / "huge.py").write_text("x = 1\n" * 50_000)
    brief = build_brief("Explain the huge module", repo_root=tree)
    for _, source in brief.grounding:
        assert len(source) <= 6_200


def test_tests_directory_is_not_grounding(tree) -> None:
    """The tests describe the code; the code is what the document is about."""
    (tree / "tests").mkdir()
    (tree / "tests" / "test_ladder.py").write_text("def test_x(): pass\n")
    brief = build_brief("Explain the ladder", repo_root=tree)
    paths = [p for p, _ in brief.grounding]
    assert not any(p.startswith("tests/") for p in paths)


# ── the quote reaches the prompt ────────────────────────────────────────────
def test_the_rendered_brief_contains_the_source(tree) -> None:
    brief = build_brief("Explain the decision ladder", repo_root=tree)
    text = render_brief(brief)
    assert "### Source:" in text
    assert "L0_DETERMINISTIC" in text


def test_the_brief_says_not_to_trust_the_name(tree) -> None:
    """The instruction that makes the quote useful rather than decorative."""
    brief = build_brief("Explain the decision ladder", repo_root=tree)
    text = render_brief(brief)
    assert "not what its name suggests" in text


def test_a_brief_without_grounding_renders_no_source_block(tree) -> None:
    brief = build_brief("Write a poem about the sea", repo_root=tree)
    assert "### Source:" not in render_brief(brief)


def test_grounding_makes_a_brief_non_empty() -> None:
    """An otherwise bare brief with a quote is still worth sending."""
    brief = TaskBrief(grounding=(("a.py", "x = 1\n"),))
    assert not brief.is_empty


def test_the_brief_records_which_files_it_quoted(tree) -> None:
    brief = build_brief("Explain the decision ladder", repo_root=tree)
    assert brief.to_dict()["grounding"] == ["gigaforge/core/ladder.py"]

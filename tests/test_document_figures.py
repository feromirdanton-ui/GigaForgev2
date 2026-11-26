"""A figure is computed from the document, not described by the model.

PaperOrchestra's plotting agent produces figures.  A document about a system has
no numeric series to plot, and a model asked to "include a chart" writes a
description of one — the same failure as a model asked for facts.  A table is
the figure that can be *computed* from what the run already holds, so it is true
by construction.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.document_pipeline import (  # noqa: E402
    DocumentPlan,
    Outline,
    Section,
    Table,
    assemble,
    build_outline_table,
)


def _plan(*titles: str) -> DocumentPlan:
    return DocumentPlan(
        outline=Outline(
            title="How a run works",
            sections=[Section(title=t, purpose="p") for t in titles],
        )
    )


# ── the table is the document ──────────────────────────────────────────────
def test_the_table_lists_every_section_in_order() -> None:
    plan = _plan("Overview", "Phases", "Settling")
    table = build_outline_table(plan.outline)
    assert table is not None
    assert [row[1] for row in table.rows] == ["Overview", "Phases", "Settling"]
    assert [row[0] for row in table.rows] == ["1", "2", "3"]


def test_the_table_counts_the_evidence_and_the_length() -> None:
    """Every cell comes from the outline, so it cannot disagree with it."""
    plan = _plan("Overview")
    plan.outline.sections[0].evidence = ["a", "b", "c"]
    plan.outline.sections[0].body = "x" * 42
    table = build_outline_table(plan.outline)
    assert table.rows[0][2] == "3"
    assert table.rows[0][3] == "42"


def test_a_document_with_no_sections_has_no_table() -> None:
    """A caption over no rows is worse than no figure."""
    assert build_outline_table(Outline(title="Empty")) is None


# ── rendering ──────────────────────────────────────────────────────────────
def test_the_table_renders_as_markdown() -> None:
    table = Table(caption="Cap", headers=["a", "b"], rows=[["1", "2"]])
    rendered = table.render()
    assert "| a | b |" in rendered
    assert "| --- | --- |" in rendered
    assert "| 1 | 2 |" in rendered
    assert "**Cap**" in rendered


def test_a_short_row_is_padded_not_left_malformed() -> None:
    """A row with fewer cells than the header is a broken table everywhere."""
    table = Table(caption="C", headers=["a", "b", "c"], rows=[["1"]])
    line = [l for l in table.render().splitlines() if l.startswith("| 1")][0]
    assert line.count("|") == 4


def test_a_pipe_in_a_cell_does_not_break_the_table() -> None:
    table = Table(caption="C", headers=["a", "b"], rows=[["x|y", "2"]])
    line = [l for l in table.render().splitlines() if "x" in l][0]
    assert "x\\|y" in line


def test_a_table_with_no_headers_renders_nothing() -> None:
    assert Table(caption="C").render() == ""


# ── assembly ───────────────────────────────────────────────────────────────
def test_the_figure_follows_the_prose_it_supports() -> None:
    plan = _plan("Overview")
    plan.outline.sections[0].table = Table(
        caption="Cap", headers=["a"], rows=[["1"]]
    )
    doc = assemble(plan, {"Overview": "The prose."})
    assert doc.index("The prose.") < doc.index("**Cap**")


def test_a_section_without_a_table_is_unchanged() -> None:
    doc = assemble(_plan("Overview"), {"Overview": "Body."})
    assert "|" not in doc
    assert "Body." in doc


def test_the_assembled_table_matches_the_assembled_document() -> None:
    """The figure describes the document that was written, not a plan for one."""
    plan = _plan("Overview", "Phases")
    plan.outline.sections[0].table = build_outline_table(plan.outline)
    doc = assemble(plan, {"Overview": "A", "Phases": "B"})
    # Both sections appear in the table, and both appear as headings.
    assert "| 1 | Overview |" in doc
    assert "| 2 | Phases |" in doc
    assert "## Overview" in doc and "## Phases" in doc

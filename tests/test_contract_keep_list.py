"""A correction step must say what to keep, not only what is missing.

Measured live (subagents mode): the model rewrote index.html to add `hero` and
dropped `cyan`/`magenta`; then rewrote again to add the colours and dropped
`hero`.  It oscillated for the whole step budget, because each correction named
only the missing items.

Listing the already-satisfied requirements turns the correction into "extend the
current page" rather than "rebuild the page".
"""
from __future__ import annotations

from pathlib import Path

from gigaforge.harness.goal_contract import (
    Requirement,
    check_artifacts,
    extract_requirements,
)

GOAL = (
    "Cyberpunk page: neon cyan/magenta palette, glitch hero title, "
    "feature grid, and a footer."
)


def _reqs() -> list[Requirement]:
    return extract_requirements(GOAL)


def test_the_verdict_separates_met_from_unmet(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<style>body{color:cyan}</style>\n<footer>x</footer>\n"
    )
    verdict = check_artifacts(_reqs(), tmp_path, files=["index.html"])
    assert "colour 'cyan'" in verdict.met
    assert "colour 'magenta'" in verdict.unmet


def test_the_correction_step_names_what_to_keep(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<style>body{color:cyan}</style>\n<footer>x</footer>\n"
    )
    observation = check_artifacts(_reqs(), tmp_path, files=["index.html"]).as_observation()
    assert "ALREADY satisfied" in observation
    assert "colour 'cyan'" in observation
    assert "section 'footer'" in observation
    # ...and the missing item is still the headline requirement.
    assert "colour 'magenta'" in observation


def test_a_satisfied_contract_produces_no_observation(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<style>body{color:cyan;border-color:magenta}</style>\n"
        "<section class='hero'>h</section>\n<div class='grid'>g</div>\n"
        "<footer>f</footer>\n" + "x" * 1400
    )
    verdict = check_artifacts(_reqs(), tmp_path, files=["index.html"])
    assert verdict.ok, verdict.unmet
    assert verdict.as_observation() == ""

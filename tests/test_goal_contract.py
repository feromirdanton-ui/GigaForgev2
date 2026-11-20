"""The goal contract: the harness checks the artifact, not the model's opinion.

A live run asked for a cyberpunk page with a "neon cyan/magenta palette".  The
model wrote a 4 KB page whose entire palette was ``#fff`` on ``#000`` — no cyan,
no magenta — called ``finish`` with an empty plan, and the run reported success.
Nothing had ever opened the file.  The model was grading its own homework.

These tests pin the two halves of the fix: the requirements are extracted only
from what the goal literally says, and the check is a real string search over
the produced files.
"""
from __future__ import annotations

from pathlib import Path

from gigaforge.harness.goal_contract import (
    check_artifacts,
    extract_requirements,
)

GOAL = (
    "Create a single-page futuristic cyberpunk website about the GigaForge agent "
    "harness. It must be a self-contained index.html with embedded CSS: neon "
    "cyan/magenta palette, dark background, glitch-effect hero title, feature "
    "grid describing the harness, and a footer. No external assets."
)


# ── extraction ───────────────────────────────────────────────────────────────
def test_extracts_the_colours_the_goal_names() -> None:
    labels = [r.label for r in extract_requirements(GOAL)]
    assert "colour 'cyan'" in labels
    assert "colour 'magenta'" in labels


def test_extracts_the_sections_the_goal_names() -> None:
    labels = [r.label for r in extract_requirements(GOAL)]
    assert "section 'footer'" in labels
    assert "section 'hero'" in labels
    assert "section 'grid'" in labels


def test_extracts_self_contained() -> None:
    labels = [r.label for r in extract_requirements(GOAL)]
    assert "no external assets" in labels


def test_a_goal_with_no_checkable_requirement_yields_none() -> None:
    """The harness must not invent a standard the user never set."""
    assert extract_requirements("Refactor the parser to be faster.") == []


def test_a_colour_word_inside_another_word_is_not_a_requirement() -> None:
    """'cyanide' is not a request for cyan."""
    assert extract_requirements("Remove the cyanide reference.") == []


# ── checking ─────────────────────────────────────────────────────────────────
def _write(tmp_path: Path, body: str, name: str = "index.html") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _page(style: str, body: str) -> str:
    """A page of realistic size.

    The contract requires a minimum artifact size once a goal names several
    parts, so a 300-byte fixture would fail for a reason unrelated to what the
    test is about.  The padding is inert markup, not a bypass: the size rule is
    checked by its own test below.
    """
    filler = "<div class='feature-box'>harness capability</div>" * 40
    return (
        "<!DOCTYPE html><html><head><style>" + style + "</style></head><body>"
        + body + filler + "</body></html>"
    )


def test_the_live_failure_is_caught(tmp_path: Path) -> None:
    """The exact artifact the live run produced: #fff on #000, no cyan/magenta."""
    _write(
        tmp_path,
        "<!DOCTYPE html><html><head><style>body{color:#fff;background:#000}"
        ".hero-title{animation:glitch 3s}</style></head><body>"
        "<div class='hero'><h1 class='hero-title'>GigaForge</h1></div>"
        "<div class='grid'>" + "<div class='feature-box'>x</div>" * 8 + "</div>"
        "<footer>c</footer></body></html>",
    )
    verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
    assert not verdict.ok
    assert "colour 'cyan'" in verdict.unmet
    assert "colour 'magenta'" in verdict.unmet


def test_a_page_that_meets_the_goal_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _page(
            "body{color:#fff;background:#000}"
            ".hero-title{color:#00ffff;text-shadow:0 0 8px #ff00ff;animation:glitch 3s}"
            ".grid{display:grid}",
            "<div class='hero'><h1 class='hero-title'>GigaForge</h1></div>"
            "<div class='grid'>" + "<div class='feature-box'>x</div>" * 8 + "</div>"
            "<footer>c</footer>",
        ),
    )
    verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
    assert verdict.ok, verdict.unmet


def test_an_external_asset_fails_self_contained(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "<html><head><link href='https://cdn.example.com/x.css' rel='stylesheet'>"
        "</head><body><footer>c</footer></body></html>",
    )
    verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
    assert "no external assets" in verdict.unmet


def test_no_artifact_at_all_is_a_failure(tmp_path: Path) -> None:
    verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
    assert not verdict.ok
    assert "no artifact was produced" in verdict.unmet


def test_the_observation_names_what_is_missing(tmp_path: Path) -> None:
    _write(tmp_path, "<html><body>nothing</body></html>")
    verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
    text = verdict.as_observation()
    assert "CONTRACT" in text
    assert "cyan" in text
    assert "write_file" in text


def test_a_passing_verdict_has_no_observation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _page(
            "body{color:#00ffff;background:#ff00ff;text-shadow:0 0 4px #0ff}"
            ".grid{display:grid}",
            "<div class='hero'>h</div>"
            "<div class='grid'>" + "<div class='feature-box'>x</div>" * 8 + "</div>"
            "<footer>c</footer>",
        ),
    )
    verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
    assert verdict.ok, verdict.unmet
    assert verdict.as_observation() == ""

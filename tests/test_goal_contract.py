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
        # A headline is part of what a landing page is; the contract now checks
        # for it, so a fixture without one would fail for a reason unrelated to
        # the test.
        + "<h1>GigaForge</h1>"
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


def test_html_comments_are_not_visible_placeholder_copy(tmp_path: Path) -> None:
    page = _page(
        "body{color:#00ffff;background:#ff00ff}.grid{display:grid}",
        "<div class='hero'><h1>GigaForge</h1></div>"
        "<div class='grid'>" + "<div class='feature-box'>Real capability details.</div>" * 8
        + "</div><footer>Built for useful work.</footer>"
        "<!-- Feature Card 1 --><!-- Footer Content -->",
    )
    _write(tmp_path, page)
    verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
    assert verdict.ok, verdict.unmet


class TestExplicitInteractiveRequirements:
    """Named features must exist as behavior, not just as labels in navigation."""

    SNN_GOAL = (
        "Create a fan site with model cards (snnTorch, Brian2, NEST, Norse), "
        "an events/learning section, reduced-motion support, responsive mobile "
        "navigation, a working join form with client-side validation and a "
        "success state in an aria-live region, and ensure every href target exists."
    )

    def test_extracts_named_models_and_interactions(self) -> None:
        labels = [r.label for r in extract_requirements(self.SNN_GOAL)]
        for model in ("snnTorch", "Brian2", "NEST", "Norse"):
            assert f"include '{model}'" in labels
        assert "reduced-motion support" in labels
        assert "working responsive mobile navigation" in labels
        assert "a working form with validation and a success state" in labels
        assert "an events/learning content section" in labels
        assert "all internal link targets exist" in labels

    def test_navigation_labels_do_not_fake_the_requested_features(
        self, tmp_path: Path
    ) -> None:
        page = _page(
            "body{color:#00ffff;background:#111}",
            "<nav><a href='#models'>Models</a><a href='#events'>Events &amp; "
            "Learning</a><a href='#join'>Join</a></nav>"
            "<section id='models'><h2>Spiking neuron models</h2></section>"
            "<section id='join'><h2>Join the club</h2>"
            "<form><input required><button>Join</button></form></section>",
        )
        _write(tmp_path, page)
        verdict = check_artifacts(extract_requirements(self.SNN_GOAL), tmp_path)
        assert not verdict.ok
        assert "include 'snnTorch'" in verdict.unmet
        assert "reduced-motion support" in verdict.unmet
        assert "working responsive mobile navigation" in verdict.unmet
        assert "a working form with validation and a success state" in verdict.unmet
        assert "an events/learning content section" in verdict.unmet
        assert "all internal link targets exist" in verdict.unmet

    def test_mobile_navigation_must_toggle_its_expanded_state(self) -> None:
        requirement = next(
            item for item in extract_requirements(self.SNN_GOAL)
            if item.kind == "responsive_navigation"
        )
        page = (
            "<style>@media(max-width:600px){nav{display:block}}</style>"
            "<nav><button aria-expanded='false' aria-controls='links' "
            "onclick='toggleMenu()'>Menu</button><ul id='links'></ul></nav>"
            "<script>function toggleMenu(){menu.style.display='none'}</script>"
        )

        assert not requirement.satisfied_by(page)

    def test_complete_interactions_satisfy_the_goal(self, tmp_path: Path) -> None:
        page = _page(
            "body{color:#00ffff;background:#111}"
            "@media (prefers-reduced-motion: reduce){*{animation:none}}"
            "@media(max-width:600px){nav{display:block}}",
            "<nav><button aria-expanded='false' aria-controls='nav-links' "
            "onclick=\"const open = this.getAttribute('aria-expanded') === 'false'; "
            "this.setAttribute('aria-expanded', String(open)); "
            "document.getElementById('nav-links').hidden = !open;\">Menu</button>"
            "<ul id='nav-links'><li><a href='#models'>Models</a></li>"
            "<li><a href='#events'>Events</a></li><li><a href='#join'>Join</a></li>"
            "</ul></nav>"
            "<section id='models'><h2>Models</h2>"
            "<article>snnTorch</article><article>Brian2</article>"
            "<article>NEST</article><article>Norse</article></section>"
            "<section id='events'><h2>Events &amp; Learning</h2></section>"
            "<section id='join'><h2>Join the club</h2>"
            "<form><input required><button>Join</button></form>"
            "<p aria-live='polite'>Спасибо, вы успешно присоединились!</p></section>"
            "<script>document.querySelector('form').addEventListener('submit', "
            "function(event){event.preventDefault();});</script>",
        )
        _write(tmp_path, page)
        verdict = check_artifacts(extract_requirements(self.SNN_GOAL), tmp_path)
        assert verdict.ok, verdict.unmet


class TestExplicitSizeRequirements:
    GOAL = (
        "Write a 700-900 word guide and ensure the file contains at least "
        "3,000 characters."
    )

    def test_extracts_word_ranges_and_character_minimums(self) -> None:
        requirements = extract_requirements(self.GOAL)
        assert [(item.kind, item.minimum, item.maximum) for item in requirements] == [
            ("word_count", 700, 900),
            ("char_count", 3000, None),
        ]

    def test_extracts_exact_word_and_character_counts(self) -> None:
        requirements = extract_requirements(
            "Write exactly 250 words and precisely 1,200 characters."
        )

        assert [(item.kind, item.minimum, item.maximum) for item in requirements] == [
            ("word_count", 250, 250),
            ("char_count", 1200, 1200),
        ]

    def test_exact_word_count_rejects_both_short_and_long_artifacts(
        self, tmp_path: Path
    ) -> None:
        requirement = extract_requirements("Write exactly 3 words.")

        _write(tmp_path, "one two")
        too_short = check_artifacts(requirement, tmp_path)
        _write(tmp_path, "one two three four")
        too_long = check_artifacts(requirement, tmp_path)

        assert any("add exactly 1 word" in item for item in too_short.unmet)
        assert any("remove exactly 1 word" in item for item in too_long.unmet)

    def test_rejects_short_output_even_when_character_count_is_met(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "welcome " * 699 + "." * 3000, name="guide.txt")

        verdict = check_artifacts(extract_requirements(self.GOAL), tmp_path)

        assert any(
            item.startswith("between 700 and 900 words") for item in verdict.unmet
        )
        assert not any(
            item.startswith("at least 3000 characters") for item in verdict.unmet
        )

    def test_accepts_output_within_both_explicit_size_constraints(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "welcome " * 700 + "." * 3000, name="guide.txt")

        verdict = check_artifacts(extract_requirements(self.GOAL), tmp_path)

        assert verdict.ok, verdict.unmet

    def test_reports_the_measured_overflow_for_a_word_range(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "welcome " * 1_000, name="guide.txt")

        verdict = check_artifacts(extract_requirements(self.GOAL), tmp_path)

        assert any(
            "between 700 and 900 words" in item
            and "has 1000 words" in item
            and "aim for about 800 words" in item
            and "rather than append" in item
            for item in verdict.unmet
        )

    def test_underflow_feedback_reports_the_exact_deficit(self, tmp_path: Path) -> None:
        _write(tmp_path, "welcome " * 173, name="guide.txt")

        verdict = check_artifacts(extract_requirements(self.GOAL), tmp_path)

        assert any("has 173 words" in item and "add at least 527 complete words" in item
                   for item in verdict.unmet)
        assert any("has 1384 characters" in item and "add at least 1616 complete characters"
                   in item for item in verdict.unmet)

    def test_sized_text_rejects_unfinished_section_placeholders(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "welcome " * 700 + "Detailed explanation continues in subsequent sections.",
            name="guide.txt",
        )

        verdict = check_artifacts(extract_requirements(self.GOAL), tmp_path)

        assert not verdict.ok
        assert any("placeholder phrases" in item and "continues in subsequent sections" in item
                   for item in verdict.unmet)


class TestResearchSourceRequirements:
    GOAL = "Use web research and cite 3 official source pages."

    def test_extracts_numbered_source_requirement(self) -> None:
        requirement, = extract_requirements(self.GOAL)

        assert requirement.kind == "source_count"
        assert requirement.minimum == requirement.maximum == 3

    def test_requires_three_numbered_entries_with_urls(self, tmp_path: Path) -> None:
        requirement = extract_requirements(self.GOAL)
        _write(
            tmp_path,
            "1. **First source**\nURL: https://one.example/docs\n"
            "2. **Second source**\nURL: https://two.example/docs\n"
            "3. **Third source**\nURL: https://three.example/docs\n",
            name="research.md",
        )

        verdict = check_artifacts(requirement, tmp_path)

        assert verdict.ok, verdict.unmet

    def test_rejects_literal_escaped_newlines_in_a_research_report(
        self, tmp_path: Path
    ) -> None:
        requirement = extract_requirements(self.GOAL)
        _write(
            tmp_path,
            "1. **First source**\\nURL: https://one.example/docs\\n"
            "2. **Second source**\\nURL: https://two.example/docs\\n"
            "3. **Third source**\\nURL: https://three.example/docs",
            name="research.md",
        )

        verdict = check_artifacts(requirement, tmp_path)

        assert not verdict.ok
        assert any("found 1 numbered entries" in item for item in verdict.unmet)


class TestHeadlineAndMetaCopy:
    """Two things a live goal run shipped and the contract accepted.

    The page had no <h1> at all — the hero was a <section> whose first child was
    an <h2> — and a feature card read "This is where we showcase our
    cutting-edge AI technology", a description of what the card would say
    accepted as the card's content.
    """

    def test_a_page_without_a_headline_is_rejected(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            _page(
                "body{color:#00ffff;background:#ff00ff}",
                "<section class='hero'><h2>GigaForge</h2></section>"
                "<div class='grid'><div class='feature-box'>x</div></div>"
                "<footer>c</footer>",
            ).replace("<h1>GigaForge</h1>", ""),
        )
        verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
        assert not verdict.ok
        assert any("headline" in item for item in verdict.unmet), verdict.unmet

    def test_an_empty_headline_is_rejected(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            _page(
                "body{color:#00ffff;background:#ff00ff}",
                "<section class='hero'><h1>   </h1></section>"
                "<div class='grid'><div class='feature-box'>x</div></div>"
                "<footer>c</footer>",
            ).replace("<h1>GigaForge</h1>", ""),
        )
        verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
        assert not verdict.ok

    def test_meta_copy_is_a_placeholder(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            _page(
                "body{color:#00ffff;background:#ff00ff}",
                "<section class='hero'><h1>GigaForge</h1></section>"
                "<div class='grid'><div class='feature-box'>"
                "This is where we showcase our cutting-edge AI technology."
                "</div></div><footer>c</footer>",
            ),
        )
        verdict = check_artifacts(extract_requirements(GOAL), tmp_path)
        assert not verdict.ok
        assert any("this is where we" in item.lower() for item in verdict.unmet), verdict.unmet


class TestCssVariables:
    """A palette declared through CSS custom properties is a real palette.

    Measured live: a sectional run produced

        :root { --accent: cyan; --secondary: magenta; }
        h1 { color: var(--accent); }

    and the contract rejected it for missing cyan and magenta.  The colours were
    declared correctly; the reference was indirect, and resolving an indirection
    is a string operation, not a judgement.
    """

    def test_a_colour_reached_through_var_is_found(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "<!DOCTYPE html><html><head><style>"
            ":root{--accent:cyan;--secondary:magenta}"
            "h1{color:var(--accent)}h2{color:var(--secondary)}"
            "</style></head><body><h1>GigaForge</h1>"
            "<section class='hero'>h</section><div class='grid'>g</div>"
            "<footer>f</footer>" + "x" * 1400 + "</body></html>",
        )
        verdict = check_artifacts(extract_requirements(GOAL), tmp_path, files=["index.html"])
        assert verdict.ok, verdict.unmet

    def test_a_var_without_a_definition_does_not_satisfy(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "<!DOCTYPE html><html><head><style>"
            "h1{color:var(--accent)}"
            "</style></head><body><h1>GigaForge</h1>"
            "<section class='hero'>h</section><div class='grid'>g</div>"
            "<footer>f</footer>" + "x" * 1400 + "</body></html>",
        )
        verdict = check_artifacts(extract_requirements(GOAL), tmp_path, files=["index.html"])
        assert not verdict.ok
        assert any("cyan" in item for item in verdict.unmet), verdict.unmet


class TestRejectionReasonReachesTheCaller:
    """A refused run must say why it was refused.

    Measured live: ``run --json`` on a page the contract had just rejected for
    placeholders reported ``status: rejected``, ``error: "ok"`` and the summary
    "Produced a change that could not be validated".  The contract's verdict was
    recorded in ``ctx.extras['contract_unmet']`` and then dropped by ``_collect``.
    """

    def test_contract_unmet_becomes_the_error(self) -> None:
        import inspect

        from gigaforge.core.pipeline import TaskPipeline

        source = inspect.getsource(TaskPipeline._collect)
        assert "contract_unmet" in source, (
            "the contract's verdict must reach the result, not stop at the event log"
        )
        assert "did not meet the goal" in source

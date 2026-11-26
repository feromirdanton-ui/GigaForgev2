"""Measurement must distinguish a change from the same result observed twice.

**The defect this pins.**  The bench reported "5/5" and "6/6" as different
facts.  With a stochastic pipeline they are the same fact: a task that passes
4 times in 5 and one that passes 5 times in 5 are one task observed twice.  A
harness that "improved" from 4/5 to 5/5 learned nothing, and one that
"regressed" from 5/5 to 4/5 broke nothing.  Without a floor every run is a
reason to celebrate or panic, and both are wrong.

**The second defect.**  Citations were collected and never checked.  An answer
could cite a URL the tools never returned and score the same as one that cited
its evidence — a confident false claim reported as success.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.bench.harness import BenchResult  # noqa: E402
from gigaforge.bench.report import delta_report, noise_floor  # noqa: E402
from gigaforge.harness.citations import (  # noqa: E402
    citation_f1,
    citation_issue,
    extract_urls,
    normalise_url,
)


def _result(name: str, passes: int, attempts: int) -> BenchResult:
    return BenchResult(
        name=name,
        task=name,
        success=passes > 0,
        cache_hit=False,
        llm_calls=1,
        tokens_in=10,
        tokens_out=10,
        latency_s=0.1,
        attempts=attempts,
        passes=passes,
    )


# ── noise floor ──────────────────────────────────────────────────────────────

def test_four_of_five_to_five_of_five_is_within_noise() -> None:
    """The exact case the review named: 4/5 -> 5/5 is not an improvement."""
    before = [_result("t", 4, 5)]
    after = [_result("t", 5, 5)]
    verdicts = noise_floor(before, after)
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.delta > 0
    assert not v.significant, (
        f"4/5 -> 5/5 was called a real change (delta={v.delta}, floor={v.floor})"
    )


def test_a_collapse_is_a_real_change() -> None:
    """5/5 -> 0/5 is far outside the noise and must be reported."""
    verdicts = noise_floor([_result("t", 5, 5)], [_result("t", 0, 5)])
    assert verdicts[0].significant
    assert verdicts[0].delta < 0


def test_more_attempts_shrink_the_floor() -> None:
    """The floor is a function of sample size, not a constant."""
    few = noise_floor([_result("t", 2, 3)], [_result("t", 3, 3)])[0]
    many = noise_floor([_result("t", 20, 30)], [_result("t", 30, 30)])[0]
    assert many.floor < few.floor, (many.floor, few.floor)


def test_a_new_task_is_reported() -> None:
    verdicts = noise_floor([], [_result("new", 1, 1)])
    assert verdicts[0].task == "new"
    assert verdicts[0].before == 0.0


def test_the_delta_report_separates_noise_from_change() -> None:
    text = delta_report(noise_floor(
        [_result("a", 4, 5), _result("b", 5, 5)],
        [_result("a", 5, 5), _result("b", 0, 5)],
    ))
    assert "within noise" in text
    assert "CHANGED" in text


# ── citations ────────────────────────────────────────────────────────────────

def test_an_invented_url_fails_the_gate() -> None:
    """A confident false claim is worse than citing nothing."""
    issue = citation_issue(
        "See https://invented.example.com/fake for details.",
        ["https://real.example.com/page"],
    )
    assert issue
    assert "never returned" in issue


def test_a_real_citation_passes() -> None:
    issue = citation_issue(
        "According to https://real.example.com/page, the answer is 4.",
        ["https://real.example.com/page"],
    )
    assert issue == ""


def test_citing_fewer_sources_than_available_is_allowed() -> None:
    """A question answered from one authoritative page is a good answer."""
    issue = citation_issue(
        "Per https://a.example.com/x, yes.",
        ["https://a.example.com/x", "https://b.example.com/y",
         "https://c.example.com/z"],
    )
    assert issue == ""


def test_require_citation_flags_a_bare_answer() -> None:
    issue = citation_issue("The answer is 4.", ["https://a.example.com/x"],
                           require_citation=True)
    assert "cites no source" in issue


def test_url_normalisation_ignores_case_and_fragment() -> None:
    assert normalise_url("https://Example.COM/a/#frag") == "https://example.com/a"
    assert normalise_url("https://example.com/a") == "https://example.com/a"


def test_trailing_punctuation_is_not_part_of_the_url() -> None:
    urls = extract_urls("See https://example.com/a, and https://example.com/b.")
    assert urls == ["https://example.com/a", "https://example.com/b"]


def test_f1_is_the_harmonic_mean() -> None:
    score = citation_f1(
        "https://a.example.com/x and https://b.example.com/y",
        ["https://a.example.com/x", "https://b.example.com/y",
         "https://c.example.com/z", "https://d.example.com/w"],
    )
    assert score.precision == 1.0
    assert score.recall == 0.5
    assert abs(score.f1 - 2 / 3) < 1e-9

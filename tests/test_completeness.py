"""Completeness is computed, not typed.

**The defect.**  Every report this project produced said "9/10" — a figure a
human typed after looking at a screenshot.  It was not reproducible, it did not
move when the code changed, and two runs of the same build could be scored
differently by the same person.  A number that cannot be recomputed is not a
metric; it is an opinion with a denominator.

``ContractVerdict.completeness`` is ``met / (met + unmet)``: the same arithmetic
for every mode, in the run's own output, changing when the artifact changes.

The rule that matters: **nothing checked is not a perfect artifact.**  A verdict
with no requirements returns 0.0, not 1.0 — reporting 1.0 on the strength of
having checked nothing is the same class of error as reporting "tests passed"
for a repo with no tests.
"""
from __future__ import annotations

from gigaforge.harness.goal_contract import ContractVerdict, Requirement


class TestCompleteness:
    def test_all_met_is_one(self) -> None:
        verdict = ContractVerdict(met=["a", "b"], unmet=[])
        assert verdict.completeness == 1.0

    def test_none_met_is_zero(self) -> None:
        verdict = ContractVerdict(met=[], unmet=["a", "b"])
        assert verdict.completeness == 0.0

    def test_partial_is_the_fraction(self) -> None:
        verdict = ContractVerdict(met=["a", "b", "c"], unmet=["d"])
        assert verdict.completeness == 0.75

    def test_nothing_checked_is_zero_not_one(self) -> None:
        """A verdict with no requirements must not claim a perfect artifact."""
        assert ContractVerdict().completeness == 0.0

    def test_it_is_recomputable_from_the_verdict(self) -> None:
        """The number is a function of the verdict, not a stored opinion."""
        verdict = ContractVerdict(met=["a"], unmet=["b", "c"])
        assert verdict.completeness == len(verdict.met) / (
            len(verdict.met) + len(verdict.unmet)
        )

    def test_to_dict_carries_it(self) -> None:
        payload = ContractVerdict(met=["a"], unmet=["b"]).to_dict()
        assert payload["completeness"] == 0.5
        assert payload["ok"] is False

    def test_ok_and_completeness_agree(self) -> None:
        """ok is 'nothing unmet'; completeness 1.0 is the same statement."""
        assert ContractVerdict(met=["a"]).ok is True
        assert ContractVerdict(met=["a"]).completeness == 1.0
        assert ContractVerdict(unmet=["a"]).ok is False
        assert ContractVerdict(unmet=["a"]).completeness == 0.0


class TestTheRequirementShape:
    def test_a_requirement_has_a_label_and_a_kind(self) -> None:
        req = Requirement("section", "section 'hero'", ("class=\"hero\"",))
        assert req.kind == "section"
        assert req.label == "section 'hero'"

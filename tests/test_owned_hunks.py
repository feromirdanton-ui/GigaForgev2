"""Dropping the model's hunk must not drop the harness's own.

The defect, measured live: ``_node_act`` dropped *every* hunk for a
harness-owned path, which also removed the ``initial_diff`` the sectional
generator had just produced.  The fix loop accepted the page, then settle
reported ``diff_chars: 0, files: 0``, and the goal loop rejected three cycles
running with the error ``ok`` — a run that had built a correct page reported
that it had built nothing.

The rule is: the model's contribution to an owned path is dropped, the
harness's own hunks are kept.  Both halves are pinned here, because a fix that
keeps everything is as wrong as one that drops everything.
"""
from __future__ import annotations

from gigaforge.core.pipeline import _drop_hunks_for, _keep_hunks_for

HARNESS = (
    "diff --git a/index.html b/index.html\n"
    "--- /dev/null\n+++ b/index.html\n@@ -0,0 +1,2 @@\n+<html>\n+</html>\n"
)
MODEL = (
    "diff --git a/index.html b/index.html\n"
    "--- a/index.html\n+++ b/index.html\n@@ -1,2 +1,2 @@\n-<html>\n+<html lang=\"en\">\n"
)
OTHER = (
    "diff --git a/db.py b/db.py\n--- a/db.py\n+++ b/db.py\n@@ -1 +1 @@\n-a\n+b\n"
)


class TestKeepHunksFor:
    def test_keeps_only_the_named_path(self) -> None:
        out = _keep_hunks_for(HARNESS + OTHER, {"index.html"})
        assert "index.html" in out
        assert "db.py" not in out

    def test_empty_inputs_are_safe(self) -> None:
        assert _keep_hunks_for("", {"x"}) == ""
        assert _keep_hunks_for(HARNESS, set()) == ""

    def test_it_is_the_complement_of_drop(self) -> None:
        combined = HARNESS + OTHER
        kept = _keep_hunks_for(combined, {"index.html"})
        dropped = _drop_hunks_for(combined, {"index.html"})
        assert "index.html" in kept and "index.html" not in dropped
        assert "db.py" in dropped and "db.py" not in kept


class TestTheModelHunkIsDroppedAndTheHarnessHunkKept:
    def test_the_harness_diff_survives(self) -> None:
        """The exact live failure: the assembled page's diff was thrown away."""
        model_diff = MODEL
        harness_diff = HARNESS
        owned = {"index.html"}
        after = _drop_hunks_for(model_diff, owned)
        after = _merge(after, _keep_hunks_for(harness_diff, owned))
        assert after.strip(), "the harness's own diff was dropped"
        assert "+<html>" in after

    def test_the_model_hunk_does_not_survive(self) -> None:
        after = _drop_hunks_for(MODEL, {"index.html"})
        assert after.strip() == ""

    def test_an_unrelated_file_is_untouched(self) -> None:
        after = _drop_hunks_for(MODEL + OTHER, {"index.html"})
        assert "db.py" in after


def _merge(original: str, repaired: str) -> str:
    from gigaforge.core.pipeline import _merge_repaired_diff

    return _merge_repaired_diff(original, repaired)

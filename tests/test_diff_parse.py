"""The diff parser, and the ownership invariant that depends on it.

**The defect these tests exist for.**  ``_drop_hunks_for`` protects files the
harness assembled itself.  It matched only ``diff --git`` headers, while the
harness's own normaliser (``firewall.validator._add_hinted_file_headers``)
produces bare ``--- a/X`` / ``+++ b/X`` pairs.  So the form the product actually
feeds it was never dropped, and ``final_diff`` described an edit that was not in
the tree.

The tests below are written against the *class* of defect, not one example:
every test that asserts "this block is dropped" also asserts that a neighbouring
block is kept, because a parser that drops everything would pass the first half.
"""
from __future__ import annotations

import pytest

from gigaforge.core.pipeline import (
    _drop_hunks_for,
    _keep_hunks_for,
    _merge_repaired_diff,
)
from gigaforge.harness.diff_parse import normalise_path, paths_in, split_diff

FULL = (
    "diff --git a/index.html b/index.html\n"
    "index 111..222 100644\n"
    "--- a/index.html\n"
    "+++ b/index.html\n"
    "@@ -1 +1 @@\n"
    "-HARNESS\n"
    "+MODEL\n"
)

BARE = "--- a/index.html\n+++ b/index.html\n@@ -1 +1 @@\n-HARNESS\n+MODEL\n"

OTHER = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)


class TestSplitDiff:
    def test_the_canonical_form_is_one_block(self) -> None:
        blocks = split_diff(FULL)
        assert [b.path for b in blocks] == ["index.html"]

    def test_the_bare_form_is_one_block(self) -> None:
        """The form ``_add_hinted_file_headers`` produces."""
        blocks = split_diff(BARE)
        assert [b.path for b in blocks] == ["index.html"]
        assert "@@ -1 +1 @@" in blocks[0].text

    def test_two_files_are_two_blocks(self) -> None:
        blocks = split_diff(FULL + OTHER)
        assert [b.path for b in blocks] == ["index.html", "app.py"]

    def test_a_bare_block_after_a_full_block_is_its_own(self) -> None:
        blocks = split_diff(FULL + BARE.replace("index.html", "page.html"))
        assert [b.path for b in blocks] == ["index.html", "page.html"]

    def test_a_hunk_header_inside_a_block_does_not_start_a_new_one(self) -> None:
        """A multi-hunk diff repeats ``---``/``+++`` only once, but a repaired
        diff can carry both a ``diff --git`` line and the pair."""
        two_hunks = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
            "@@ -9 +9 @@\n-p\n+q\n"
        )
        blocks = split_diff(two_hunks)
        assert len(blocks) == 1
        # ``@@`` appears twice per hunk header (``@@ -1 +1 @@``), so count lines.
        assert sum(1 for ln in blocks[0].text.splitlines() if ln.startswith("@@")) == 2

    def test_creation_from_dev_null_takes_the_new_path(self) -> None:
        created = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+print(1)\n"
        assert [b.path for b in split_diff(created)] == ["new.py"]

    def test_deletion_to_dev_null_takes_the_old_path(self) -> None:
        deleted = "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-print(1)\n"
        assert [b.path for b in split_diff(deleted)] == ["gone.py"]

    def test_a_timestamp_after_the_path_is_not_part_of_it(self) -> None:
        stamped = "--- a/x.py\t2026-01-01 00:00:00\n+++ b/x.py\t2026-01-01 00:00:01\n@@ -1 +1 @@\n-a\n+b\n"
        assert [b.path for b in split_diff(stamped)] == ["x.py"]

    def test_text_that_is_not_a_diff_is_no_blocks(self) -> None:
        assert split_diff("just prose, no hunks") == []
        assert split_diff("") == []

    def test_concatenating_the_blocks_returns_the_input(self) -> None:
        """A caller that joins the blocks back must not lose a byte."""
        source = FULL + OTHER
        assert "".join(b.text for b in split_diff(source)) == source

    def test_paths_in_lists_each_file_once(self) -> None:
        assert paths_in(FULL + OTHER + BARE) == ["index.html", "app.py"]


class TestNormalisePath:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("a/index.html", "index.html"),
            ("b/index.html", "index.html"),
            ("./index.html", "index.html"),
            ("index.html", "index.html"),
            ("sub/index.html", "sub/index.html"),
            ("a\\index.html", "index.html"),
        ],
    )
    def test_forms(self, raw: str, expected: str) -> None:
        assert normalise_path(raw) == expected

    def test_a_subdirectory_is_not_the_root_file(self) -> None:
        """``index.html`` must not match ``sub/index.html``."""
        assert normalise_path("sub/index.html") != normalise_path("index.html")


class TestDropHunksFor:
    def test_the_bare_form_is_dropped(self) -> None:
        """The defect: this returned the input unchanged."""
        assert _drop_hunks_for(BARE, {"index.html"}) == ""

    def test_the_canonical_form_is_dropped(self) -> None:
        assert _drop_hunks_for(FULL, {"index.html"}) == ""

    def test_a_neighbouring_file_is_kept(self) -> None:
        """A parser that drops everything would pass the tests above."""
        kept = _drop_hunks_for(FULL + OTHER, {"index.html"})
        assert "app.py" in kept
        assert "index.html" not in kept

    def test_a_subdirectory_file_is_not_dropped_by_the_root_name(self) -> None:
        nested = BARE.replace("index.html", "sub/index.html")
        assert _drop_hunks_for(nested, {"index.html"}) == nested

    def test_no_paths_is_a_no_op(self) -> None:
        assert _drop_hunks_for(FULL, set()) == FULL


class TestKeepHunksFor:
    def test_the_bare_form_is_kept(self) -> None:
        assert _keep_hunks_for(BARE, {"index.html"}) == BARE

    def test_the_other_file_is_not_kept(self) -> None:
        kept = _keep_hunks_for(FULL + OTHER, {"index.html"})
        assert "index.html" in kept
        assert "app.py" not in kept

    def test_no_paths_keeps_nothing(self) -> None:
        assert _keep_hunks_for(FULL, set()) == ""


class TestMergeRepairedDiff:
    def test_the_repaired_block_replaces_the_original(self) -> None:
        repaired = BARE.replace("+MODEL", "+REPAIRED")
        merged = _merge_repaired_diff(FULL, repaired)
        assert "REPAIRED" in merged
        assert "+MODEL" not in merged

    def test_an_untouched_file_keeps_its_block(self) -> None:
        merged = _merge_repaired_diff(FULL + OTHER, BARE.replace("+MODEL", "+REPAIRED"))
        assert "app.py" in merged
        assert "REPAIRED" in merged

    def test_a_file_the_repair_created_is_appended(self) -> None:
        created = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+print(1)\n"
        merged = _merge_repaired_diff(FULL, created)
        assert "new.py" in merged
        assert "index.html" in merged

    def test_empty_repaired_returns_the_original(self) -> None:
        assert _merge_repaired_diff(FULL, "") == FULL


class TestTheOwnershipInvariant:
    """If a file was restored, the diff must not describe an edit to it.

    This is the invariant the defect violated: ``resolution.restored`` was
    non-empty while ``final_diff`` still carried the model's hunk.  Stated as a
    test, it fails on the old implementation for both header forms.
    """

    @pytest.mark.parametrize("shape", [FULL, BARE], ids=["canonical", "bare"])
    def test_a_restored_file_has_no_hunk_in_the_diff(self, shape: str) -> None:
        restored = {"index.html"}
        final = _drop_hunks_for(shape + OTHER, restored)
        assert paths_in(final) == ["app.py"], (
            "a file the harness restored must not appear in final_diff"
        )

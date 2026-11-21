"""Ownership is one invariant in a type, not three mechanisms in comments.

**The defect.**  Three mechanisms solved one problem, and none of them said what
the problem was:

1. ``_drop_hunks_for(diff, owned)`` removed the model's hunks for an owned path;
2. ``_merge_repaired_diff(diff, _keep_hunks_for(prefetched, owned))`` put the
   harness's own hunks back;
3. ``_restore_harness_owned(ctx)`` wrote the harness's bytes over whatever the
   fix loop had applied.

Each was added to fix a live failure of the previous one, and the invariant they
were all approximating — *the harness's assembled page is the artifact; the
model's answer is a guess at the same file* — lived only in comments.  The
consequence: ``_restore_harness_owned`` overwrote the file with the harness's
content **after** the model's diff, so a model that fixed a real bug in that file
had its fix silently discarded.  Ownership was absolute because nothing in the
code could say it was not.

The fix is a type.  ``AUTHORITATIVE`` means the harness assembled the file from
a plan and its bytes are the artifact; ``SEED`` means the harness wrote a
baseline and the model's edits are the work.  These tests pin both policies and
the tree-side half of the resolution.
"""
from __future__ import annotations

from pathlib import Path

from gigaforge.harness.ownership import OwnedFile, OwnedFiles, Policy


class TestThePolicyIsAValue:
    def test_authoritative_is_the_default(self) -> None:
        """An assembled page is the artifact unless something says otherwise."""
        assert OwnedFile(path="index.html", content="<html></html>").policy is Policy.AUTHORITATIVE

    def test_a_seed_file_says_so(self) -> None:
        entry = OwnedFile(path="index.html", content="x", policy=Policy.SEED)
        assert entry.policy is Policy.SEED

    def test_the_sets_are_separated(self) -> None:
        owned = OwnedFiles.from_mapping(
            {"a.html": "A", "b.html": "B"},
            policies={"b.html": Policy.SEED},
        )
        assert owned.authoritative_paths == {"a.html"}
        assert owned.seed_paths == {"b.html"}

    def test_an_empty_collection_is_falsy(self) -> None:
        assert not OwnedFiles()

    def test_contains_works_by_path(self) -> None:
        owned = OwnedFiles.from_mapping({"a.html": "A"})
        assert "a.html" in owned
        assert "b.html" not in owned


class TestTheTreeSideHonoursThePolicy:
    """``_restore_harness_owned`` must not overwrite a seeded file.

    This is the exact defect the review found: an edit of an existing page had
    its fix silently reverted because the restore step wrote the harness's
    baseline back over the model's work.
    """

    def _pipeline(self, tmp_path: Path):
        from gigaforge.core.pipeline import TaskPipeline

        pipeline = TaskPipeline.__new__(TaskPipeline)
        pipeline._repo_root = lambda: tmp_path  # type: ignore[method-assign]
        return pipeline

    def _ctx(self, *, edit: bool, content: dict[str, str]):
        class _Ctx:
            extras = {
                "harness_owned_content": content,
                "generation": {"edit": edit},
            }

        return _Ctx()

    def test_an_authoritative_file_is_restored(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("model's guess", encoding="utf-8")
        pipeline = self._pipeline(tmp_path)
        ctx = self._ctx(edit=False, content={"index.html": "the assembled page"})
        restored = pipeline._restore_harness_owned(ctx)
        assert restored == ["index.html"]
        assert (tmp_path / "index.html").read_text() == "the assembled page"

    def test_a_seeded_file_is_left_alone(self, tmp_path: Path) -> None:
        """The model's edit to a page that already existed must survive."""
        (tmp_path / "index.html").write_text("the model's fix", encoding="utf-8")
        pipeline = self._pipeline(tmp_path)
        ctx = self._ctx(edit=True, content={"index.html": "the old baseline"})
        restored = pipeline._restore_harness_owned(ctx)
        assert restored == [], "a seeded file must not be written back"
        assert (tmp_path / "index.html").read_text() == "the model's fix"

    def test_an_untouched_authoritative_file_is_not_rewritten(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("same", encoding="utf-8")
        pipeline = self._pipeline(tmp_path)
        ctx = self._ctx(edit=False, content={"index.html": "same"})
        assert pipeline._restore_harness_owned(ctx) == []

    def test_a_deleted_authoritative_file_is_recreated(self, tmp_path: Path) -> None:
        pipeline = self._pipeline(tmp_path)
        ctx = self._ctx(edit=False, content={"index.html": "the assembled page"})
        assert pipeline._restore_harness_owned(ctx) == ["index.html"]
        assert (tmp_path / "index.html").read_text() == "the assembled page"

    def test_no_owned_content_is_a_no_op(self, tmp_path: Path) -> None:
        pipeline = self._pipeline(tmp_path)
        assert pipeline._restore_harness_owned(self._ctx(edit=False, content={})) == []

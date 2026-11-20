"""The harness's own page must survive the model's diff.

Measured live on the ``run`` mode: the sectional generator assembled a 5452-byte
page and wrote it to disk, the model returned a 1139-byte hero-only page as a
diff, the fix loop applied it, and the run settled ``accepted: false`` with the
contract reporting "section 'features' missing", "section 'footer' missing",
"colour 'cyan' missing".  The harness had built the page correctly and then let
the model overwrite it — and then failed itself for the result.

Dropping the model's hunk from the *diff* was not enough: the fix loop applies
the diff to the tree before the drop happens.  The content has to be restored.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.core.pipeline import TaskPipeline

ASSEMBLED = (
    "<!DOCTYPE html><html><head><style>body{color:#0ff}</style></head><body>"
    '<section class="hero"><h1>GigaForge</h1></section>'
    '<section class="features"><div class="card">A</div><div class="card">B</div>'
    '<div class="card">C</div></section>'
    '<div class="footer">Footer</div></body></html>'
)
MODEL_ANSWER = (
    "<!DOCTYPE html><html><head><style>body{color:#fff}</style></head><body>"
    '<section class="hero"><h1>GigaForge</h1></section></body></html>'
)


class _Ctx:
    def __init__(self) -> None:
        self.run_id = "t"
        self.extras: dict = {}


class _Stub(TaskPipeline):
    def __init__(self, root: Path) -> None:  # noqa: D107
        self.config = type("C", (), {"repo_root": str(root)})()

    def _repo_root(self) -> Path:
        return Path(self.config.repo_root)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return tmp_path


class TestRestoreHarnessOwned:
    def test_the_assembled_page_is_written_back(self, repo: Path) -> None:
        (repo / "index.html").write_text(MODEL_ANSWER, encoding="utf-8")
        ctx = _Ctx()
        ctx.extras["harness_owned_content"] = {"index.html": ASSEMBLED}
        restored = _Stub(repo)._restore_harness_owned(ctx)
        assert restored == ["index.html"]
        assert (repo / "index.html").read_text(encoding="utf-8") == ASSEMBLED

    def test_an_untouched_file_is_not_rewritten(self, repo: Path) -> None:
        (repo / "index.html").write_text(ASSEMBLED, encoding="utf-8")
        ctx = _Ctx()
        ctx.extras["harness_owned_content"] = {"index.html": ASSEMBLED}
        assert _Stub(repo)._restore_harness_owned(ctx) == []

    def test_a_deleted_file_is_recreated(self, repo: Path) -> None:
        ctx = _Ctx()
        ctx.extras["harness_owned_content"] = {"index.html": ASSEMBLED}
        assert _Stub(repo)._restore_harness_owned(ctx) == ["index.html"]
        assert (repo / "index.html").is_file()

    def test_no_owned_content_is_a_noop(self, repo: Path) -> None:
        (repo / "index.html").write_text(MODEL_ANSWER, encoding="utf-8")
        assert _Stub(repo)._restore_harness_owned(_Ctx()) == []
        assert (repo / "index.html").read_text(encoding="utf-8") == MODEL_ANSWER

    def test_the_restored_page_keeps_every_section(self, repo: Path) -> None:
        (repo / "index.html").write_text(MODEL_ANSWER, encoding="utf-8")
        ctx = _Ctx()
        ctx.extras["harness_owned_content"] = {"index.html": ASSEMBLED}
        _Stub(repo)._restore_harness_owned(ctx)
        page = (repo / "index.html").read_text(encoding="utf-8")
        assert page.count('class="card"') == 3
        assert 'class="footer"' in page
        assert "#0ff" in page


class TestTheSectionalGeneratorRecordsItsContent:
    def test_it_stores_the_page_it_wrote(self) -> None:
        import inspect

        source = inspect.getsource(TaskPipeline._sectional_generation)
        assert 'ctx.extras["harness_owned_content"]' in source, (
            "the assembled page must be kept, or the model's diff overwrites it"
        )

    def test_act_restores_before_building_the_proposal(self) -> None:
        import inspect

        source = inspect.getsource(TaskPipeline._node_act)
        assert "_restore_harness_owned" in source
        # ``ProposeResult(`` also appears in the ``propose`` closure, so the
        # anchor is the final construction, which is the one that becomes the
        # node's output.
        assert source.index("_restore_harness_owned") < source.index(
            "proposal = ProposeResult("
        )

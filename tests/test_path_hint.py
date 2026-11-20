"""A bare hunk is not an unparseable diff when the harness knows the file.

Measured live on the ``run`` mode: the model answered with a hunk and no
``--- a/path`` header, the parser raised "hunk without a file header and no path
hint", and the run settled ``rejected`` with ``files: 0`` — while the harness
held the path in ``harness_owned_paths`` and the page on disk was correct.

The parser already supports ``path_hint``; nothing passed it.  The harness knows
which file it asked about, so it says so.
"""
from __future__ import annotations

import inspect

from gigaforge.core.fixloop import FixLoop
from gigaforge.core.pipeline import TaskPipeline
from gigaforge.firewall.diff import parse
from gigaforge.firewall.validator import DiffValidator

BARE_HUNK = "@@ -1,2 +1,2 @@\n-<h1>Old</h1>\n+<h1>New</h1>\n"


class TestTheParserAcceptsAHint:
    def test_a_bare_hunk_parses_with_a_hint(self) -> None:
        parsed = parse(BARE_HUNK, path_hint="index.html")
        assert [f.path for f in parsed.files] == ["index.html"]

    def test_a_bare_hunk_without_a_hint_still_raises(self) -> None:
        import pytest

        from gigaforge.firewall.diff import DiffParseError

        with pytest.raises(DiffParseError):
            parse(BARE_HUNK)


class TestTheValidatorForwardsIt:
    def test_validate_takes_a_path_hint(self) -> None:
        params = inspect.signature(DiffValidator.validate).parameters
        assert "path_hint" in params

    def test_run_once_takes_a_path_hint(self) -> None:
        params = inspect.signature(DiffValidator._run_once).parameters
        assert "path_hint" in params

    def test_a_bare_hunk_is_not_a_parse_failure(self, tmp_path) -> None:
        (tmp_path / "index.html").write_text("<h1>Old</h1>\n", encoding="utf-8")
        from gigaforge.firewall.config import FirewallConfig

        validator = DiffValidator(FirewallConfig(repo_root=str(tmp_path)))
        outcome = validator.validate(BARE_HUNK, max_retries=0, dry_run=True,
                                     path_hint="index.html")
        reasons = [a.repair.reason for a in outcome.attempts if a.repair]
        assert "diff could not be parsed" not in reasons


class TestTheLoopForwardsIt:
    def test_fixloop_run_takes_a_path_hint(self) -> None:
        assert "path_hint" in inspect.signature(FixLoop.run).parameters

    def test_validate_once_takes_a_path_hint(self) -> None:
        assert "path_hint" in inspect.signature(FixLoop.validate_once).parameters

    def test_the_pipeline_passes_the_owned_path(self) -> None:
        source = inspect.getsource(TaskPipeline._node_act)
        assert "path_hint=" in source
        assert "harness_owned_paths" in source

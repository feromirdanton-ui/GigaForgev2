from __future__ import annotations

from pathlib import Path

from gigaforge.firewall.config import FirewallConfig
from gigaforge.firewall.validator import DiffValidator


def test_autofix_note_does_not_abort_diff_validation(
    tmp_path: Path, monkeypatch
) -> None:
    source = "def add(a: int, b: int) -> int:\n    return a - b\n"
    (tmp_path / "math_utils.py").write_text(source, encoding="utf-8")
    validator = DiffValidator(
        config=FirewallConfig(repo_root=str(tmp_path), reindex_after_apply=False)
    )
    monkeypatch.setattr(
        validator,
        "_auto_fix",
        lambda paths: ["math_utils.py: auto-fixed by ruff"],
    )
    diff = (
        "--- a/math_utils.py\n"
        "+++ b/math_utils.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a: int, b: int) -> int:\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )

    outcome = validator.validate(diff, task="make add return the sum")

    assert outcome.accepted, outcome.reason

from gigaforge.core.config import CoreConfig
from gigaforge.core.contracts import ValidateResult
from gigaforge.core.fixloop import FixLoopResult
from gigaforge.core.pipeline import RunContext, TaskPipeline, _merge_focused_creation
from gigaforge.core.state import TaskState


def test_unvalidated_creation_header_does_not_suppress_focused_creation(tmp_path):
    pipeline = TaskPipeline(config=CoreConfig(repo_root=str(tmp_path)))
    focused_diff = (
        "--- /dev/null\n"
        "+++ b/research.md\n"
        "@@ -0,0 +1,2 @@\n"
        "+# Research\n"
        "+Findings.\n"
    )
    calls = []

    def ask(_phase, _prompt, _ctx):
        calls.append(True)
        return focused_diff

    pipeline._ask = ask
    ctx = RunContext(
        run_id="run",
        state=TaskState(run_id="run", goal="Create research.md"),
    )

    result = pipeline._ensure_creations(
        ctx,
        "--- a/research.md\n+++ b/research.md\n",
        "",
        validated=False,
    )

    assert result == focused_diff.strip()
    assert calls == [True]


def test_focused_creation_is_accepted_and_tracks_applied_file():
    result = FixLoopResult(run_id="run", task="create research.md", accepted=False)
    validation = ValidateResult(
        accepted=True,
        applied=True,
        changed_files=["research.md"],
        reason="applied",
    )

    outcome = _merge_focused_creation(
        result,
        "diff --git a/research.md b/research.md",
        validation,
        ["research.md"],
    )

    assert outcome == {"accepted": True, "files": ["research.md"], "missing": []}
    assert result.accepted is True
    assert result.changed_files == ["research.md"]
    assert result.iterations[-1].validate is validation


def test_focused_creation_does_not_accept_uncreated_requested_files():
    result = FixLoopResult(run_id="run", task="create research.md and notes.md", accepted=False)
    validation = ValidateResult(
        accepted=True,
        applied=True,
        changed_files=["research.md"],
        reason="applied",
    )

    outcome = _merge_focused_creation(
        result,
        "diff --git a/research.md b/research.md",
        validation,
        ["research.md", "notes.md"],
    )

    assert outcome["accepted"] is False
    assert outcome["missing"] == ["notes.md"]
    assert result.accepted is False

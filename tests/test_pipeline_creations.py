from types import SimpleNamespace

import pytest

from gigaforge.core.config import CoreConfig
from gigaforge.core.contracts import RunContext
from gigaforge.core.errors import PipelineError
from gigaforge.core.fixloop import FixLoop
from gigaforge.core.pipeline import TaskPipeline
from gigaforge.core.state import TaskState
from gigaforge.firewall.config import FirewallConfig
from gigaforge.firewall.validator import DiffValidator


def _pipeline(tmp_path):
    firewall_config = FirewallConfig(repo_root=str(tmp_path))
    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.config = CoreConfig(repo_root=str(tmp_path))
    pipeline.firewall = SimpleNamespace(config=firewall_config)
    pipeline.router = SimpleNamespace(last_route=None)
    pipeline.fixloop = FixLoop(validator=DiffValidator(config=firewall_config))
    pipeline.optimizer = None
    pipeline._file_context = lambda goal: ""
    pipeline._subagent_diff = lambda ctx, **kwargs: ""
    pipeline._episode_diff = lambda ctx: ""
    pipeline._owned_files = lambda ctx: None
    pipeline._event = lambda *args, **kwargs: None
    return pipeline


def test_missing_goal_file_is_added_before_fixloop_validation(tmp_path):
    pipeline = _pipeline(tmp_path)
    ctx = RunContext(
        run_id="create-file-test",
        state=TaskState(
            run_id="create-file-test",
            goal="Create research.py with a short module docstring.",
        ),
    )
    pipeline._ask = lambda phase, prompt, context: (
        "```diff\n"
        "--- /dev/null\n"
        "+++ b/research.py\n"
        "@@ -0,0 +1 @@\n"
        "+\"\"\"Research notes for the task.\"\"\"\n"
        "```"
    )

    result = pipeline._node_act(ctx, {})

    assert result["proposal"].diff
    assert ctx.extras["fix_result"].accepted
    assert ctx.extras["fix_result"].changed_files == ["research.py"]
    assert (tmp_path / "research.py").read_text(encoding="utf-8") == (
        '"""Research notes for the task."""\n'
    )


def test_focused_creation_rejects_diff_for_another_path(tmp_path):
    pipeline = _pipeline(tmp_path)
    ctx = RunContext(
        run_id="wrong-create-path",
        state=TaskState(
            run_id="wrong-create-path",
            goal="Create research.py with a short module docstring.",
        ),
    )
    pipeline._ask = lambda *args: (
        "```diff\n"
        "--- /dev/null\n"
        "+++ b/other.py\n"
        "@@ -0,0 +1 @@\n"
        "+\"\"\"Wrong path.\"\"\"\n"
        "```"
    )

    with pytest.raises(PipelineError, match="did not return a diff creating that file"):
        pipeline._ensure_creations(ctx, "", "")

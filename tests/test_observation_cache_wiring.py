"""The observation cache must actually invalidate, and must actually save tokens.

Two defects are pinned here, both found by reading the wiring rather than the
class:

1. ``ReActRunner`` built the cache with ``getattr(self, "ctx", None)`` — an
   attribute that does not exist on the class.  The root was therefore always
   ``None``, the workspace stamp was always ``""``, and the cache NEVER
   invalidated: a cached grep survived a write, so the model edited a file,
   searched again, and was handed the pre-edit answer.
2. A cache hit still appended the full observation text to the trajectory, so
   the same tokens were paid again on every subsequent request.  The cache saved
   a tool call and cost the budget it existed to protect.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from gigaforge.agents.observation_cache import ObservationCache


# ── invalidation ────────────────────────────────────────────────────────────
def test_a_write_invalidates_a_cached_observation(tmp_path: Path) -> None:
    """The correctness bug: a stale grep must not survive an edit."""
    target = tmp_path / "a.py"
    target.write_text("v1\n", encoding="utf-8")

    cache = ObservationCache(root=tmp_path)
    cache.put("grep", "v1", "found v1")
    assert cache.get("grep", "v1") == "found v1"

    target.write_text("v2\n", encoding="utf-8")
    assert cache.get("grep", "v1") is None, (
        "the tree changed but the cache still served the pre-edit observation"
    )


def test_a_new_file_invalidates_too(tmp_path: Path) -> None:
    cache = ObservationCache(root=tmp_path)
    cache.put("grep", "x", "nothing")
    (tmp_path / "new.py").write_text("x\n", encoding="utf-8")
    assert cache.get("grep", "x") is None


def test_a_cache_with_no_root_never_claims_a_hit_after_a_change(tmp_path: Path) -> None:
    """A rootless cache cannot detect changes, so it must not be trusted.

    This is the shape the runner was accidentally in.  The class cannot invent a
    root, so the guard belongs at the call site — but the class must at least be
    honest: with no root the stamp is empty and stays empty.
    """
    cache = ObservationCache(root=None)
    assert cache._workspace_stamp() == ""


def test_the_runner_passes_the_context_root_to_the_cache(tmp_path: Path) -> None:
    """The wiring bug: ``self.ctx`` does not exist, so the root was always None."""
    from gigaforge.agents.react import ReActRunner

    class _Client:
        async def chat(self, *a, **k):
            from gigaforge.client.giga_client import ChatResult

            return ChatResult(content='{"thought":"t","action":"finish","action_input":"done"}',
                              usage={"prompt_tokens": 1, "completion_tokens": 1})

    class _Budgets:
        def remaining(self, phase):
            return 10**9

        def task_remaining_for(self, task_id):
            return 10**9

        def check(self, *a, **k):
            return None

        def commit_task(self, *a, **k):
            return None

        def check_overshoot(self, *a, **k):
            return None

        def reserve(self, *a, **k):
            from tests.budget_stubs import NullClaim

            return NullClaim()

    class _Router:
        def route(self, phase):
            return "m"

    class _Tracer:
        def trace(self, *a, **k):
            return None

    class _State:
        task_id = "t"

        def serialize_prefix(self):
            return {}

    class _Ctx:
        def __init__(self, root):
            self.worktree = root
            self.repo_root = root
            self.sandbox = None
            self.index = None

    runner = ReActRunner(client=_Client(), router=_Router(), budgets=_Budgets(),
                         tracer=_Tracer(), max_steps=1)
    asyncio.run(runner.run("goal", _State(), ctx=_Ctx(tmp_path)))
    assert runner._obs_cache.root == tmp_path, (
        "the cache was built without a root, so it can never invalidate"
    )


# ── the token win ───────────────────────────────────────────────────────────
def test_a_cache_hit_does_not_repeat_the_observation_text(tmp_path: Path) -> None:
    """A hit must cost a pointer, not the payload again."""
    from gigaforge.agents.react import ReActRunner

    big = "x" * 4000

    class _Client:
        def __init__(self):
            self.calls = 0
            #: Every message list the loop actually sent, so the test can look
            #: at what the MODEL was charged for — not at result.steps, which
            #: does not carry the trajectory.
            self.sent: list[list[dict]] = []

        async def chat(self, *a, **k):
            from gigaforge.client.giga_client import ChatResult

            messages = a[1] if len(a) > 1 else k.get("messages", [])
            self.sent.append([dict(m) for m in messages])
            self.calls += 1
            if self.calls == 1:
                body = '{"thought":"look","action":"grep","action_input":"needle"}'
            elif self.calls == 2:
                body = '{"thought":"look again","action":"grep","action_input":"needle"}'
            else:
                body = '{"thought":"done","action":"finish","action_input":"ok"}'
            return ChatResult(content=body, usage={"prompt_tokens": 1, "completion_tokens": 1})

    class _Budgets:
        def remaining(self, phase):
            return 10**9

        def task_remaining_for(self, task_id):
            return 10**9

        def check(self, *a, **k):
            return None

        def commit_task(self, *a, **k):
            return None

        def check_overshoot(self, *a, **k):
            return None

        def reserve(self, *a, **k):
            from tests.budget_stubs import NullClaim

            return NullClaim()

    class _Router:
        def route(self, phase):
            return "m"

    class _Tracer:
        def trace(self, *a, **k):
            return None

    class _State:
        task_id = "t"

        def serialize_prefix(self):
            return {}

    class _Ctx:
        def __init__(self, root):
            self.worktree = root
            self.repo_root = root
            self.sandbox = None
            self.index = None

    class _Grep:
        name = "grep"
        calls = 0

        async def run(self, action_input, *, ctx):
            _Grep.calls += 1
            return big

    client = _Client()
    runner = ReActRunner(client=client, router=_Router(), budgets=_Budgets(),
                         tracer=_Tracer(), max_steps=4)
    runner._registry.register(_Grep())
    asyncio.run(runner.run("find needle", _State(), ctx=_Ctx(tmp_path)))

    assert _Grep.calls == 1, "the second identical grep must be served from the cache"
    assert runner._obs_cache.hits >= 1

    # The payload must appear ONCE across everything the model was ever sent.
    # A hit that re-appends the text pays for the same tokens on every
    # subsequent request — the cache would save a tool call and cost the budget
    # it existed to protect.
    total = sum(
        str(message.get("content", "")).count("x" * 100)
        for messages in client.sent
        for message in messages
    )
    assert total <= 1, (
        f"the cached observation reached the model {total} times; a hit must "
        "cost a pointer, not the payload again"
    )
    # And the pointer must actually be there, so the model knows where to look.
    assert any(
        "identical to an earlier step" in str(message.get("content", ""))
        for messages in client.sent
        for message in messages
    )

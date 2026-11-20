"""The observation cache and the harness profiles.

The cache is the one place in the ReAct loop where a stale answer is possible,
so its invalidation is tested rather than assumed: a write must be observed, and
a read-only tool must not be cached across a change to the tree.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from gigaforge.agents.observation_cache import CACHEABLE_TOOLS, ObservationCache
from gigaforge.harness.profiles import PROFILES, resolve_profile


# ── the cache ───────────────────────────────────────────────────────────────
def test_a_repeated_read_is_served_from_the_cache(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    cache = ObservationCache(root=tmp_path)
    cache.put("grep", "x", "a.py:1:x = 1")
    assert cache.get("grep", "x") == "a.py:1:x = 1"
    assert cache.hits == 1


def test_a_write_tool_is_never_cached(tmp_path: Path) -> None:
    """A tool that can change the tree must always be observed."""
    (tmp_path / "a.py").write_text("x = 1\n")
    cache = ObservationCache(root=tmp_path)
    cache.put("edit_function", {"name": "f"}, "ok")
    assert cache.get("edit_function", {"name": "f"}) is None
    assert "edit_function" not in CACHEABLE_TOOLS


def test_a_change_to_the_tree_invalidates_the_cache(tmp_path: Path) -> None:
    """The whole point: a stale observation is a correctness bug."""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    cache = ObservationCache(root=tmp_path)
    cache.put("read_structure", "a.py", "def f(): ...")
    assert cache.get("read_structure", "a.py") is not None

    time.sleep(0.01)
    target.write_text("x = 2\ny = 3\n")

    assert cache.get("read_structure", "a.py") is None, (
        "a cached observation survived a change to the file it describes"
    )


def test_the_cache_is_bounded(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    cache = ObservationCache(root=tmp_path, max_entries=3)
    for i in range(10):
        cache.put("grep", f"q{i}", f"result {i}")
    assert len(cache._entries) == 3
    # The newest survive, the oldest are evicted.
    assert cache.get("grep", "q9") == "result 9"
    assert cache.get("grep", "q0") is None


def test_the_key_is_order_insensitive_for_dict_inputs(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    cache = ObservationCache(root=tmp_path)
    cache.put("grep", {"a": 1, "b": 2}, "hit")
    assert cache.get("grep", {"b": 2, "a": 1}) == "hit"


def test_a_cache_without_a_root_still_works(tmp_path: Path) -> None:
    """No workspace means no invalidation — but the cache must not crash."""
    cache = ObservationCache(root=None)
    cache.put("grep", "x", "hit")
    assert cache.get("grep", "x") == "hit"


# ── the profiles ────────────────────────────────────────────────────────────
def test_the_default_profile_is_balanced() -> None:
    profile = resolve_profile("default")
    assert profile.allow_writes is True
    assert profile.max_steps == 6


def test_read_only_refuses_writes() -> None:
    assert resolve_profile("read_only").allow_writes is False


def test_an_unknown_profile_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in an env var must not stop a run."""
    monkeypatch.setenv("GIGAFORGE_HARNESS_PROFILE", "no-such-profile")
    profile = resolve_profile()
    assert profile.max_steps == PROFILES["default"].max_steps
    assert "unknown" in profile.name


def test_the_profile_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIGAFORGE_HARNESS_PROFILE", "large_repo")
    assert resolve_profile().max_steps == 10


def test_every_profile_is_described() -> None:
    """``gigaforge stack`` prints these; an empty one is a blank line."""
    for name, profile in PROFILES.items():
        assert profile.description, name
        assert profile.name == name
        assert 0 < profile.compact_at < 1

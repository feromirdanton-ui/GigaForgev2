"""The auth key's scheme prefix is not part of the secret.

**The defect this pins.**  ``.env`` files in the wild — including the one this
project is tested against — carry the key as ``Basic MDE5...``, because that is
how the provider's own docs show it.  The client then sent
``Authorization: Basic Basic MDE5...`` and the provider answered 401.  The
failure looks like a bad credential, so it sends the operator hunting for a new
key; measured live, it cost the whole first live run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.client.auth import _bare_auth_key  # noqa: E402


def test_a_prefixed_key_is_stripped() -> None:
    assert _bare_auth_key("Basic MDE5Zm9v") == "MDE5Zm9v"


def test_a_bare_key_is_unchanged() -> None:
    assert _bare_auth_key("MDE5Zm9v") == "MDE5Zm9v"


def test_lowercase_and_padded_prefixes_are_stripped() -> None:
    assert _bare_auth_key("basic MDE5Zm9v") == "MDE5Zm9v"
    assert _bare_auth_key("  Basic   MDE5Zm9v  ") == "MDE5Zm9v"


def test_an_empty_key_stays_empty() -> None:
    assert _bare_auth_key("") == ""
    assert _bare_auth_key("Basic ") == ""


def test_the_prefix_is_stripped_exactly_once() -> None:
    """A key that genuinely begins with the letters is not mangled twice."""
    assert _bare_auth_key("Basic BasicX") == "BasicX"

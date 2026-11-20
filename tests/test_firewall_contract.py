"""The Firewall's public ``compress()`` has ONE return type.

The dispatcher routes to two generations of compressors: the newer ones in
``gigaforge.firewall.compress`` return a ``CompressionResult``, the legacy ones
in ``gigaforge.firewall.compressors`` return a bare ``dict``.  It used to return
whatever came back, so the documented usage —

    packed = compress("grep", raw)
    prompt += packed.text

— worked on one branch and raised ``AttributeError: 'dict' object has no
attribute 'text'`` on the other.  These tests pin the contract so the branch
cannot become visible again.
"""
from __future__ import annotations

import json

import pytest

from gigaforge.firewall import compress
from gigaforge.firewall.compress.base import CompressionResult, coerce_result

GREP_RAW = "src/a.py:1:def f():\nsrc/b.py:2:    return 1\n"


# ── the contract ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["grep", "pytest", "gitdiff", "builderr"])
def test_every_kind_returns_a_result_envelope(kind: str) -> None:
    """No branch may hand back a bare dict."""
    result = compress(kind, GREP_RAW)
    assert isinstance(result, CompressionResult), (
        f"compress({kind!r}, ...) returned {type(result).__name__}, not a "
        "CompressionResult — a caller cannot rely on .text"
    )


def test_text_is_non_empty_json_for_every_kind() -> None:
    """``.text`` is the documented way to get the prompt payload."""
    for kind in ("grep", "pytest", "gitdiff", "builderr"):
        text = compress(kind, GREP_RAW).text
        assert text, f"compress({kind!r}, ...).text is empty"
        json.loads(text)  # must be valid JSON, not a repr


def test_both_call_shapes_agree() -> None:
    """``compress(kind, raw)`` and ``compress(raw, kind)`` are the same call."""
    forward = compress("grep", GREP_RAW)
    reverse = compress(GREP_RAW, "grep")
    assert forward.kind == reverse.kind == "grep"
    assert forward.text == reverse.text


def test_token_accounting_is_honest_for_a_wrapped_payload() -> None:
    """A wrapped legacy payload still reports what it saved."""
    result = compress("grep", GREP_RAW)
    assert result.source_chars == len(GREP_RAW)
    assert result.compressed_chars > 0
    assert result.tokens_before >= result.tokens_after


def test_a_small_payload_is_never_expanded() -> None:
    """The firewall must not cost more tokens than the raw output it replaced.

    Measured: two grep lines (44 chars) became a 142-char structured object,
    because the schema's field names cost more than the data.  A compressor
    that expands a short observation is doing the opposite of its job.
    """
    result = compress("grep", GREP_RAW)
    assert result.compressed_chars <= result.source_chars, (
        f"compress() expanded {result.source_chars} chars into "
        f"{result.compressed_chars} — the model pays for the schema"
    )
    assert result.tokens_before >= result.tokens_after
    assert result.meta.get("passthrough"), "the result must say it passed through"


def test_a_large_payload_is_still_compressed() -> None:
    """The passthrough must not disable real compression on a big input."""
    big = "\n".join(f"src/mod{i}.py:{i}:def handler_{i}(request):" for i in range(400))
    result = compress("grep", big)
    assert result.compressed_chars < result.source_chars
    assert result.savings_ratio > 0.5
    assert not result.meta.get("passthrough")


# ── the coercion itself ─────────────────────────────────────────────────────
def test_a_legacy_dict_is_wrapped_not_reparsed() -> None:
    """Wrapping must not re-run a compressor over its own output."""
    legacy = {"matches": [{"file": "a.py", "line": 1}], "_truncated": 1}
    result = coerce_result(legacy, kind="grep", source="a.py:1:x")
    assert isinstance(result, CompressionResult)
    assert result.data == {"matches": [{"file": "a.py", "line": 1}]}
    # The legacy private counter is lifted into the envelope's flag, so a
    # caller reading ``.truncated`` is not told a cut payload was complete.
    assert result.truncated is True
    assert "_truncated" not in result.data


def test_a_result_passes_through_unchanged() -> None:
    original = CompressionResult(kind="grep", data={"a": 1}, source_chars=10, compressed_chars=5)
    assert coerce_result(original, kind="grep") is original


def test_a_string_payload_is_still_usable() -> None:
    """A compressor returning a string must not crash a best-effort path."""
    result = coerce_result("raw text", kind="grep", source="raw text")
    assert result.text == '{"text":"raw text"}'

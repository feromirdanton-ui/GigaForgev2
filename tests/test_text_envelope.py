"""A diff envelope around prose is a computation, not a failure.

**The defect this pins.**  Measured live on the document goal: the model wrote
the document correctly and then wrapped it in a patch, because every other phase
of this harness speaks diff.  The reply was

    ```diff
    diff --git a/gigaforge_run_explanation.md b/...
    @@ -0,0 +1,1023 @@
    +## GigaForge Run Explanation
    ```

Rejecting that throws away a correct document and pays for a second call to get
the same text without the wrapper.  The harness can unwrap it exactly, so it
does.

**The other half.**  A reply that is not a diff envelope must come back
unchanged — a document that legitimately contains a diff sample is not an
envelope.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.pipeline import _unwrap_text_envelope  # noqa: E402


def test_a_diff_envelope_is_unwrapped() -> None:
    text = (
        "```diff\n"
        "diff --git a/x.md b/x.md\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/x.md\n"
        "@@ -0,0 +1,3 @@\n"
        "+## Intake\n"
        "+Real prose here.\n"
        "+## Plan\n"
        "+More prose.\n"
        "```"
    )
    out = _unwrap_text_envelope(text)
    assert out.startswith("## Intake")
    assert "Real prose here." in out
    assert "diff --git" not in out
    assert "```" not in out


def test_plain_prose_is_unchanged() -> None:
    text = "## Intake\n\nReal prose about the subject."
    assert _unwrap_text_envelope(text) == text


def test_a_short_diff_sample_inside_prose_is_unchanged() -> None:
    """A document that explains diffs is not an envelope."""
    text = (
        "A patch looks like this:\n\n"
        "```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n\n"
        "The plus lines are the additions.\n"
    )
    assert _unwrap_text_envelope(text) == text


def test_a_fenced_block_that_is_not_a_diff_is_unchanged() -> None:
    text = "```python\nprint(1)\n```"
    assert _unwrap_text_envelope(text) == text


def test_an_empty_reply_is_unchanged() -> None:
    assert _unwrap_text_envelope("") == ""

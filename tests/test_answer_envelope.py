"""A reply that is a tool call is not a reply.

**The defect this pins.**  Measured live on the document goal: the answer was

    ```diff
    write_file("how_gigaforge_run_works.md", \"\"\"# How ...\"\"\")
    ```

and it was accepted, because the text was non-empty and every section title was
in it.  The person asked for a document and got a code block describing how to
write one.  The section check passed on the *envelope*, which is the same class
of defect as a contract that grades its own vocabulary instead of the artifact.

**The other half.**  Prose that contains a code sample is not an envelope.  A
check that rejected any answer with a fenced block would forbid explaining code,
which is a large part of what a harness is for.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.pipeline import _tool_envelope_issue  # noqa: E402


def test_a_write_file_envelope_is_rejected() -> None:
    text = (
        "```diff\n"
        'write_file(\n  "how_gigaforge_run_works.md",\n'
        '  """# How a GigaForge Run Works\n\n### Intake\ntext\n"""\n)\n'
        "```"
    )
    issue = _tool_envelope_issue(text)
    assert issue
    assert "tool call" in issue


def test_a_create_file_envelope_is_rejected() -> None:
    text = '```python\ncreate_file("x.md", "# Doc")\n```'
    assert _tool_envelope_issue(text)


def test_plain_prose_passes() -> None:
    text = "# Doc\n\n## Intake\n\n" + "Real prose about the subject. " * 10
    assert _tool_envelope_issue(text) == ""


def test_prose_with_a_code_sample_passes() -> None:
    """Explaining code is a large part of what a harness is for."""
    text = (
        "Here is how the cache works.\n\n"
        "```python\ncache[key] = value\n```\n\n"
        "The key is the prompt hash, so a re-sent prompt is a hit.\n"
    )
    assert _tool_envelope_issue(text) == ""


def test_a_short_fenced_block_inside_a_long_answer_passes() -> None:
    text = "A long explanation. " * 40 + "\n```python\nprint(1)\n```\n"
    assert _tool_envelope_issue(text) == ""


def test_an_empty_reply_is_not_an_envelope() -> None:
    """Emptiness is a different failure with its own message."""
    assert _tool_envelope_issue("") == ""

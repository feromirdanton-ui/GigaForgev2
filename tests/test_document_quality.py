"""A document made of sentences about itself is not a document.

**The defect this pins.**  Live, ``"Write a design document in docs/design.md
describing a plugin architecture for the calculator with three sections."``
produced a file whose every section read::

    ## Section 2: Design Details
    This section delves into the specifics of how plugins will be integrated...

The file was valid Markdown with the right headings and the right number of
sections, so every existing check passed and the run reported success.  Nothing
looked at the text, because the pipeline's only notion of a product was a diff.

The check is deterministic and narrow: it refuses prose that describes its own
structure instead of its subject.  It does not judge quality.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.pipeline import _document_placeholder_issue  # noqa: E402

GOAL = "Write a design document describing a plugin architecture for the calculator."


def test_the_exact_live_document_is_refused() -> None:
    diff = """diff --git a/docs/design.md b/docs/design.md
new file mode 100644
--- /dev/null
+++ b/docs/design.md
@@ -0,0 +1,9 @@
+# Plugin Architecture for Calculator
+## Section 1: Introduction
+This section provides an overview of the plugin architecture and its benefits.
+
+## Section 2: Design Details
+This section delves into the specifics of how plugins will be integrated into the calculator application.
+
+## Section 3: Implementation Plan
+This section outlines the steps needed to implement the plugin architecture.
"""
    issue = _document_placeholder_issue(diff, GOAL)
    assert issue, "the exact document that shipped live must be refused"
    assert "describes its own sections" in issue


def test_real_writing_passes() -> None:
    diff = """diff --git a/docs/design.md b/docs/design.md
--- /dev/null
+++ b/docs/design.md
@@ -0,0 +1,6 @@
+# Plugin Architecture
+## Loading
+A plugin is a Python module exposing `register(calc)`. The loader scans
+`plugins/`, imports each module and calls `register`, so a new operation is a
+file, not an edit to the core.
+
+## Isolation
+Each plugin runs in its own namespace and receives a read-only view of the
+operation table, so a broken plugin cannot shadow `add`.
"""
    assert _document_placeholder_issue(diff, GOAL) == ""


def test_one_self_describing_sentence_among_real_ones_passes() -> None:
    """A real document may open a section by naming it; that is normal prose."""
    diff = """--- /dev/null
+++ b/docs/design.md
@@ -0,0 +1,5 @@
+# Architecture
+## Loading
+This section covers the loader. The loader scans `plugins/` and imports every
+module it finds, then calls `register(calc)` on each one.
+
+## Isolation
+Plugins get a read-only view of the operation table.
"""
    assert _document_placeholder_issue(diff, GOAL) == ""


def test_a_diff_with_no_prose_is_not_judged() -> None:
    """A document that is only headings is a different problem, not this one."""
    diff = "--- /dev/null\n+++ b/docs/x.md\n@@ -0,0 +1,2 @@\n+# Title\n+## Section\n"
    assert _document_placeholder_issue(diff, GOAL) == ""

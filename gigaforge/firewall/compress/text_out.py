"""A compressor for text that is already the answer: file contents.

**Why this exists.**  ``read_file`` fell through to the ``builderr`` compressor,
which parses its input as compiler output.  A source file contains no
diagnostic, so the compressor returned

    {"error": "no parseable compiler diagnostic found", "raw_truncated": "..."}

and the model — correctly — concluded the tool was broken.  Measured on
SWE-bench: the episode asked to read a 900-line file, received that envelope,
retried twice, and never saw the function it needed on line 66.

A file's contents are not a diagnostic and not a search result.  The honest
bound is: keep the head, keep the tail, and say exactly how much was dropped —
so a model that needs the middle asks for a narrower range instead of guessing.
"""
from __future__ import annotations

from typing import Any

from gigaforge.firewall.compress.base import CompressionResult, Truncator

__all__ = ["TextCompressor"]


class TextCompressor:
    """Bound a plain-text payload by characters, keeping head and tail."""

    kind = "text"

    #: Roughly four characters per token, the same ratio the other compressors
    #: use, so a budget expressed in tokens means the same thing here.
    CHARS_PER_TOKEN = 4.0

    def compress(
        self, source: str, *, limit_tokens: int = 4000, **_: Any
    ) -> CompressionResult:
        text = source or ""
        limit = max(200, int(limit_tokens * self.CHARS_PER_TOKEN))
        truncator = Truncator()
        if len(text) <= limit:
            return CompressionResult(
                kind=self.kind,
                data={"text": text},
                source_chars=len(text),
                compressed_chars=len(text),
                truncated=False,
                chars_per_token=self.CHARS_PER_TOKEN,
                raw=text,
            )

        # Head and tail, not just the head: a file's imports and its last
        # function are both worth keeping, and a model that sees only the first
        # 4 000 characters of a 900-line module learns nothing about its end.
        head_chars = int(limit * 0.7)
        tail_chars = limit - head_chars
        head = text[:head_chars]
        tail = text[-tail_chars:] if tail_chars > 0 else ""
        dropped = len(text) - len(head) - len(tail)
        marker = (
            f"\n[... {dropped} characters omitted; "
            f"read a narrower range to see them ...]\n"
        )
        truncator.note(True)
        body = head + marker + tail
        return CompressionResult(
            kind=self.kind,
            data={
                "text": body,
                "omitted_chars": dropped,
                "total_chars": len(text),
            },
            source_chars=len(text),
            compressed_chars=len(body),
            truncated=True,
            truncated_fields=["text"],
            chars_per_token=self.CHARS_PER_TOKEN,
            raw=body,
        )

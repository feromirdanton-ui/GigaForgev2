"""Incremental Server-Sent Events parser.

The reference ``app.py`` parsed SSE with a single ``line.startswith("data: ")`` check
over ``resp.iter_lines()``. That survives the happy path and fails on everything
else: a ``data:`` field with no space after the colon, an event whose payload is
split across several ``data:`` lines, a ``:`` keep-alive comment, or a stream that
ends without ever sending ``[DONE]``.

This parser follows the parts of the SSE grammar the chat endpoint actually uses,
and - importantly - it is *incremental*: the caller feeds lines or raw chunks and
receives completed events. It also remembers whether ``[DONE]`` was seen, so the
stream layer can tell "finished cleanly" from "the connection just stopped".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, List, Optional

DONE_SENTINEL = "[DONE]"


@dataclass
class SSEEvent:
    """One dispatched SSE event (the block between two blank lines)."""

    data: str = ""
    event: str = ""
    id: Optional[str] = None
    retry: Optional[int] = None
    comments: List[str] = field(default_factory=list)

    @property
    def is_done(self) -> bool:
        return self.data.strip() == DONE_SENTINEL

    @property
    def is_empty(self) -> bool:
        return not self.data.strip()

    def json(self) -> Any:
        """Parsed payload, or ``None`` when the data is not JSON."""
        text = self.data.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        preview = self.data[:48]
        return f"SSEEvent(event={self.event!r}, data={preview!r})"


class SSEParser:
    """Line-oriented, incremental SSE parser.

    Typical use::

        parser = SSEParser()
        for line in response.iter_lines():
            for event in parser.feed_lines([line]):
                handle(event)
        for event in parser.flush():   # EOF without a trailing blank line
            handle(event)
    """

    def __init__(self) -> None:
        self._data: List[str] = []
        self._event = ""
        self._id: Optional[str] = None
        self._retry: Optional[int] = None
        self._comments: List[str] = []
        self._buffer = ""
        self._pending = False
        self._emitted = 0
        self._saw_done = False

    # -- feed ------------------------------------------------------------------
    def feed_line(self, raw_line: Any) -> Optional[SSEEvent]:
        """Feed a single line. Returns a completed event, or ``None``."""
        line = self._decode(raw_line)
        if line is None:
            return None
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            # A comment; providers use these as keep-alives on idle streams.
            self._comments.append(line[1:].lstrip())
            return None
        field_name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        self._pending = True
        if field_name == "data":
            self._data.append(value)
        elif field_name == "event":
            self._event = value
        elif field_name == "id":
            self._id = value
        elif field_name == "retry":
            try:
                self._retry = int(value)
            except ValueError:
                pass
        # Unknown field names are ignored, as the spec requires.
        return None

    def feed_lines(self, lines: Iterable[Any]) -> Iterator[SSEEvent]:
        """Feed an iterable of lines, yielding every completed event."""
        for line in lines or ():
            event = self.feed_line(line)
            if event is not None:
                yield event

    def feed_chunk(self, chunk: Any) -> Iterator[SSEEvent]:
        """Feed raw bytes/str, handling partial lines across chunk boundaries."""
        if not chunk:
            return
        if isinstance(chunk, (bytes, bytearray)):
            chunk = bytes(chunk).decode("utf-8", "replace")
        text = self._buffer + str(chunk)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        parts = text.split("\n")
        self._buffer = parts.pop()  # trailing partial line stays buffered
        for line in parts:
            event = self.feed_line(line)
            if event is not None:
                yield event

    def flush(self) -> List[SSEEvent]:
        """Drain everything still buffered when the stream ends.

        Covers a final event with no trailing blank line, and a trailing partial
        line that the transport never terminated.
        """
        events: List[SSEEvent] = []
        if self._buffer:
            pending, self._buffer = self._buffer, ""
            event = self.feed_line(pending.replace("\r", ""))
            if event is not None:
                events.append(event)
        if self._pending or self._data or self._event or self._comments:
            event = self._dispatch()
            if event is not None:
                events.append(event)
        return events

    # -- introspection ---------------------------------------------------------
    @property
    def saw_done(self) -> bool:
        return self._saw_done

    @property
    def events_emitted(self) -> int:
        return self._emitted

    @property
    def has_buffered_data(self) -> bool:
        return bool(self._pending or self._data or self._buffer)

    # -- internals -------------------------------------------------------------
    def _dispatch(self) -> Optional[SSEEvent]:
        if not (self._pending or self._data or self._event or self._comments):
            return None
        event = SSEEvent(
            data="\n".join(self._data),
            event=self._event,
            id=self._id,
            retry=self._retry,
            comments=list(self._comments),
        )
        self._data = []
        self._event = ""
        self._id = None
        self._retry = None
        self._comments = []
        self._pending = False
        self._emitted += 1
        if event.is_done:
            self._saw_done = True
        return event

    @staticmethod
    def _decode(raw_line: Any) -> Optional[str]:
        if raw_line is None:
            return None
        if isinstance(raw_line, (bytes, bytearray)):
            line = bytes(raw_line).decode("utf-8", "replace")
        else:
            line = str(raw_line)
        if line.endswith("\n"):
            line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
        return line


def _normalise(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def parse_event_block(block: str) -> dict[str, str]:
    """Parse one complete event block (no trailing blank line) into a dict."""
    event_type: str | None = None
    data_lines: list[str] = []
    for raw_line in block.split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field = line
            value = ""
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_type = value
    out: dict[str, str] = {}
    if event_type is not None:
        out["event"] = event_type
    if data_lines:
        out["data"] = "\n".join(data_lines)
    return out


async def parse_sse_stream(byte_chunks: Any) -> Any:
    """Yield parsed SSE events from an async iterator of byte/string chunks."""
    buffer = ""
    async for chunk in byte_chunks:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        buffer += _normalise(chunk)
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            event = parse_event_block(block)
            if event:
                yield event
    remaining = buffer.strip("\n")
    if remaining:
        event = parse_event_block(remaining)
        if event:
            yield event


__all__ = ["DONE_SENTINEL", "SSEEvent", "SSEParser", "parse_sse_stream", "parse_event_block"]

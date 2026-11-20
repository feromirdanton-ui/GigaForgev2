"""Aggregation of an SSE chat stream into a :class:`ChatResult`.

Three failure modes of the reference ``app.py`` streaming path are handled here,
and each one is a real interoperability problem rather than a hypothetical:

* **empty ``delta.content``** - many frames carry only a role, only a
  ``function_call``, or nothing at all. They are not errors, and they must not
  append an empty string to the reply. They are counted instead.
* **no ``[DONE]``** - the stream can simply end: an idle timeout, a proxy closing
  the connection, a provider bug. The aggregator finalises on EOF *or* on
  ``finish_reason``, whichever arrives first, and records which one it was.
* **no ``usage``** - GigaChat frequently omits ``usage`` on streamed responses.
  Reporting ``0`` tokens is worse than useless: it corrupts the TPM bucket and the
  cost ledger. So the aggregator falls back to the ``chars / 4`` heuristic and marks
  the result ``estimated=True`` so downstream code can tell the two apart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .functions import finalize_function_call, parse_function_call
from .sse import SSEEvent
from .types import ChatResult, FunctionCall, StreamDelta, Usage, estimate_token_count


@dataclass
class StreamStats:
    """Everything unusual that happened while reading the stream."""

    frames: int = 0
    content_frames: int = 0
    empty_content_frames: int = 0
    malformed_frames: int = 0
    unknown_frames: int = 0
    usage_frames: int = 0
    comment_frames: int = 0
    saw_done: bool = False
    saw_finish_reason: Optional[str] = None
    content_chars: int = 0
    function_fragments: int = 0

    @property
    def ended_without_done(self) -> bool:
        return not self.saw_done

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frames": self.frames,
            "content_frames": self.content_frames,
            "empty_content_frames": self.empty_content_frames,
            "malformed_frames": self.malformed_frames,
            "unknown_frames": self.unknown_frames,
            "usage_frames": self.usage_frames,
            "comment_frames": self.comment_frames,
            "saw_done": self.saw_done,
            "saw_finish_reason": self.saw_finish_reason,
            "content_chars": self.content_chars,
            "function_fragments": self.function_fragments,
            "ended_without_done": self.ended_without_done,
        }


class StreamAggregator:
    """Turns a sequence of :class:`SSEEvent` into deltas plus one final result."""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        estimate_ratio: float = 4.0,
        prompt_text: str = "",
        prompt_tokens: int = 0,
        request_id: Optional[str] = None,
    ) -> None:
        self.model = model
        self.estimate_ratio = estimate_ratio
        self.prompt_text = prompt_text or ""
        self.prompt_tokens = int(prompt_tokens or 0)
        self.request_id = request_id
        self.stats = StreamStats()

        self._content_parts: List[str] = []
        self._tool_name_parts: List[str] = []
        self._tool_arg_parts: List[str] = []
        self._tool_call: Optional[FunctionCall] = None
        self._usage: Optional[Usage] = None
        self._finish_reason: Optional[str] = None
        self._result: Optional[ChatResult] = None

    # -- feeding ---------------------------------------------------------------
    def accept(self, event: Optional[SSEEvent]) -> Optional[StreamDelta]:
        """Consume one SSE event; return a delta when it carried something."""
        if event is None:
            return None
        if event.comments:
            self.stats.comment_frames += 1
        if event.is_done:
            self.stats.saw_done = True
            return None
        if event.is_empty:
            return None
        payload = event.json()
        if payload is None:
            self.stats.malformed_frames += 1
            return None
        return self.accept_payload(payload)

    def accept_payload(self, payload: Any) -> Optional[StreamDelta]:
        """Consume one decoded JSON frame; return a delta when it carried something."""
        if not isinstance(payload, Mapping):
            self.stats.malformed_frames += 1
            return None
        self.stats.frames += 1

        model = payload.get("model")
        if isinstance(model, str) and model:
            self.model = model

        usage = Usage.from_api(payload.get("usage"), model=self.model)
        if usage is not None:
            self._usage = usage
            self.stats.usage_frames += 1

        delta = StreamDelta(model=self.model, usage=usage)

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            # A usage-only frame is legitimate and common as the last frame.
            if usage is None:
                self.stats.unknown_frames += 1
            return None if delta.is_empty() else delta

        choice = choices[0] if isinstance(choices[0], Mapping) else {}
        try:
            delta.index = int(choice.get("index") or 0)
        except (TypeError, ValueError):
            delta.index = 0

        body = choice.get("delta")
        if not isinstance(body, Mapping):
            body = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}

        content = body.get("content")
        if isinstance(content, str) and content:
            self._content_parts.append(content)
            self.stats.content_frames += 1
            self.stats.content_chars += len(content)
            delta.content = content
        else:
            # The "empty delta.content" case: a frame that is valid but carries no text.
            self.stats.empty_content_frames += 1

        reasoning = body.get("reasoning_content") or body.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            delta.reasoning = reasoning

        call = parse_function_call(body)
        if call is None:
            # A continuation fragment of a split call often carries arguments only,
            # with an empty/absent name. parse_function_call rejects that (no name),
            # so absorb the arguments here and keep the name from earlier frames.
            raw_call = body.get("function_call")
            if isinstance(raw_call, Mapping) and raw_call.get("arguments"):
                call = FunctionCall(
                    name="", arguments=raw_call.get("arguments"), raw=dict(raw_call)
                )
        if call is not None:
            self.stats.function_fragments += 1
            self._absorb_function_fragment(call)
            delta.function_call = self._snapshot_call()

        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish:
            self._finish_reason = finish
            self.stats.saw_finish_reason = finish
            delta.finish_reason = finish

        return None if delta.is_empty() else delta

    # -- finalisation ----------------------------------------------------------
    def finalize(
        self,
        *,
        streamed: bool = True,
        latency_ms: float = 0.0,
        attempts: int = 1,
    ) -> ChatResult:
        """Build the aggregated result. Idempotent: later calls return the cached one."""
        if self._result is not None:
            return self._result

        content = "".join(self._content_parts)
        function_call = self._build_call()

        usage = self._usage
        if usage is None:
            completion_text = content
            if function_call is not None and isinstance(function_call.arguments, str):
                completion_text = content + function_call.arguments
            usage = Usage.estimate(
                self.prompt_text,
                completion_text,
                ratio=self.estimate_ratio,
                model=self.model,
            )
            if self.prompt_tokens:
                usage.prompt_tokens = self.prompt_tokens
                usage.completion_tokens = max(0, usage.total_tokens - usage.prompt_tokens)
                usage.total_tokens = usage.prompt_tokens + usage.completion_tokens

        finish_reason = self._finish_reason
        if finish_reason is None:
            # No explicit finish_reason: infer it, and keep the nuance visible in
            # stats so a truncated stream is not silently treated as a clean stop.
            finish_reason = "stop" if self.stats.saw_done else "eof"

        self._result = ChatResult(
            content=content,
            function_call=function_call,
            usage=usage,
            finish_reason=finish_reason,
            model=self.model,
            streamed=streamed,
            latency_ms=latency_ms,
            attempts=attempts,
            request_id=self.request_id,
            metrics=self.stats.to_dict(),
        )
        return self._result

    # -- internals -------------------------------------------------------------
    def _absorb_function_fragment(self, call: FunctionCall) -> None:
        """Merge one streamed fragment of a function call."""
        if call.partial and call.raw.get("origin") == "stream_fragment":
            return
        if call.name and call.name not in self._tool_name_parts:
            self._tool_name_parts.append(call.name)
        arguments = call.arguments
        if isinstance(arguments, str) and arguments:
            self._tool_arg_parts.append(arguments)
        elif isinstance(arguments, Mapping) and arguments:
            self._tool_call = call

    def _snapshot_call(self) -> Optional[FunctionCall]:
        name = "".join(self._tool_name_parts)
        if not name:
            return None
        return FunctionCall(name=name, arguments="".join(self._tool_arg_parts), partial=True)

    def _build_call(self) -> Optional[FunctionCall]:
        if self._tool_call is not None:
            return self._tool_call
        name = "".join(self._tool_name_parts)
        if not name:
            return None
        return finalize_function_call(name, "".join(self._tool_arg_parts))


__all__ = ["StreamAggregator", "StreamStats"]

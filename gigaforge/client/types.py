"""Data-transfer objects shared across the client layer.

Deliberately plain (dataclasses + stdlib): the reference ``app.py`` returned
untyped dicts and tuples, which is why its token accounting drifted. Here every
value that crosses a boundary has a name and a shape.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence


def _as_dict(value: Any) -> Dict[str, Any]:
    """Coerce a mapping / JSON string / junk into a dict, never raising."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    return {"_raw": value}


def estimate_token_count(text: Optional[str], ratio: float = 4.0) -> int:
    """The ``chars / 4`` heuristic used when the provider omits ``usage``."""
    if not text:
        return 0
    safe_ratio = float(ratio) if ratio and ratio > 0 else 4.0
    return max(1, int(math.ceil(len(text) / safe_ratio)))


# --------------------------------------------------------------------------- #
# Function calling
# --------------------------------------------------------------------------- #
@dataclass
class FunctionCall:
    """A structured function call returned by the model."""

    name: str
    arguments: Any = None
    raw: Dict[str, Any] = field(default_factory=dict)
    partial: bool = False

    @property
    def args(self) -> Dict[str, Any]:
        return _as_dict(self.arguments)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "arguments": self.args, "partial": self.partial}

    def to_message_payload(self) -> Dict[str, Any]:
        """Shape expected in a ``function_call``-bearing assistant message."""
        args = self.args
        return {"name": self.name, "arguments": json.dumps(args, ensure_ascii=False)}


def _as_schema(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {"type": "object", "properties": {}}


@dataclass
class FunctionSpec:
    """A flat function declaration (GigaChat style, not the OpenAI nested shape)."""

    name: str
    description: str = ""
    parameters: Dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    few_shot_examples: Optional[List[Dict[str, Any]]] = None
    returns: Optional[Dict[str, Any]] = None

    def to_flat(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": _as_schema(self.parameters),
        }
        if self.few_shot_examples:
            out["few_shot_examples"] = list(self.few_shot_examples)
        if self.returns:
            out["return_parameters"] = dict(self.returns)
        return out

    @classmethod
    def coerce(cls, spec: Any) -> "FunctionSpec":
        """Accept a FunctionSpec, a flat dict or an OpenAI-nested dict."""
        if isinstance(spec, FunctionSpec):
            return spec
        if isinstance(spec, Mapping):
            node = spec.get("function") if isinstance(spec.get("function"), Mapping) else spec
            return cls(
                name=str(node.get("name") or ""),
                description=str(node.get("description") or ""),
                parameters=_as_schema(
                    node.get("parameters") or node.get("input_schema")
                ),
                few_shot_examples=node.get("few_shot_examples"),
                returns=node.get("return_parameters") or node.get("returns"),
            )
        raise TypeError(f"cannot build a FunctionSpec from {type(spec).__name__}")


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
@dataclass
class ChatMessage:
    role: str
    content: str = ""
    name: Optional[str] = None
    function_call: Optional[FunctionCall] = None
    functions_state_id: Optional[str] = None
    attachments: Optional[List[str]] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"role": self.role, "content": self.content or ""}
        if self.name:
            payload["name"] = self.name
        if self.function_call is not None:
            payload["function_call"] = self.function_call.to_message_payload()
        if self.functions_state_id:
            payload["functions_state_id"] = self.functions_state_id
        if self.attachments:
            payload["attachments"] = list(self.attachments)
        return payload

    @classmethod
    def user(cls, content: str, **kw: Any) -> "ChatMessage":
        return cls("user", content, **kw)

    @classmethod
    def system(cls, content: str, **kw: Any) -> "ChatMessage":
        return cls("system", content, **kw)

    @classmethod
    def assistant(cls, content: str = "", **kw: Any) -> "ChatMessage":
        return cls("assistant", content, **kw)

    @classmethod
    def function_result(cls, name: str, content: str) -> "ChatMessage":
        return cls("function", content, name=name)

    @classmethod
    def coerce(cls, value: Any) -> "ChatMessage":
        if isinstance(value, ChatMessage):
            return value
        if isinstance(value, Mapping):
            return cls(
                role=str(value.get("role") or "user"),
                content=str(value.get("content") or ""),
                name=value.get("name"),
                functions_state_id=value.get("functions_state_id"),
                attachments=value.get("attachments"),
            )
        return cls("user", str(value))


def coerce_messages(messages: Sequence[Any]) -> List[ChatMessage]:
    return [ChatMessage.coerce(m) for m in messages]


def join_message_text(messages: Sequence[Any]) -> str:
    parts = []
    for m in messages:
        msg = ChatMessage.coerce(m)
        if msg.content:
            parts.append(msg.content)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    source: str = "usage"  # 'usage' (provider) | 'estimate' (heuristic)
    estimated: bool = False
    model: Optional[str] = None

    @classmethod
    def from_api(cls, payload: Any, *, model: Optional[str] = None) -> Optional["Usage"]:
        if not isinstance(payload, Mapping) or not payload:
            return None
        prompt = int(payload.get("prompt_tokens") or 0)
        completion = int(payload.get("completion_tokens") or 0)
        total = int(payload.get("total_tokens") or (prompt + completion))
        if total == 0 and prompt == 0 and completion == 0:
            return None
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            source="usage",
            estimated=False,
            model=model,
        )

    @classmethod
    def estimate(
        cls,
        prompt_text: str = "",
        completion_text: str = "",
        *,
        ratio: float = 4.0,
        model: Optional[str] = None,
    ) -> "Usage":
        prompt = estimate_token_count(prompt_text, ratio)
        completion = estimate_token_count(completion_text, ratio)
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            source="estimate",
            estimated=True,
            model=model,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "source": self.source,
            "estimated": self.estimated,
            "model": self.model,
        }


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #
@dataclass
class StreamDelta:
    """One increment of a streamed completion."""

    content: str = ""
    reasoning: str = ""
    function_call: Optional[FunctionCall] = None
    finish_reason: Optional[str] = None
    usage: Optional[Usage] = None
    model: Optional[str] = None
    index: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (
            self.content or self.reasoning or self.function_call or self.finish_reason or self.usage
        )


@dataclass
class ChatResult:
    """The aggregated outcome of a completion (streamed or not)."""

    content: str = ""
    function_call: Optional[FunctionCall] = None
    usage: Usage = field(default_factory=Usage)
    finish_reason: Optional[str] = None
    model: Optional[str] = None
    streamed: bool = False
    latency_ms: float = 0.0
    attempts: int = 1
    recovered_text_call: bool = False
    request_id: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_function_call(self) -> bool:
        return self.function_call is not None and bool(self.function_call.name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "function_call": self.function_call.to_dict() if self.function_call else None,
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
            "model": self.model,
            "streamed": self.streamed,
            "latency_ms": round(self.latency_ms, 3),
            "attempts": self.attempts,
            "recovered_text_call": self.recovered_text_call,
            "request_id": self.request_id,
        }


@dataclass
class ModelInfo:
    id: str
    created: Optional[int] = None
    owned_by: str = "GigaChat"

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "created": self.created, "owned_by": self.owned_by}


def usage_tokens(usage: Any) -> tuple[int, int]:
    """``(prompt_tokens, completion_tokens)`` from either usage shape.

    ``ChatResult.usage`` is a :class:`Usage` dataclass on the real client and a
    plain dict on the fake.  Eight call sites did ``usage.get("prompt_tokens")``,
    which raises ``AttributeError`` on the real client — and the exception was
    swallowed by a best-effort ``except``, so every live run recorded zero
    tokens and the budget never saw the spend.  One helper, one shape.
    """
    if usage is None:
        return 0, 0
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if prompt is None and isinstance(usage, Mapping):
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
    return int(prompt or 0), int(completion or 0)


__all__ = [
    "ChatMessage",
    "ChatResult",
    "FunctionCall",
    "FunctionSpec",
    "ModelInfo",
    "StreamDelta",
    "Usage",
    "coerce_messages",
    "estimate_token_count",
    "join_message_text",
    "usage_tokens",
]

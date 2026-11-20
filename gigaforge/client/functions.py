"""Function-calling support for GigaChat's *flat* function schema.

Two properties of the provider make tool use fiddly, and both are handled here:

**Flat declarations.** GigaChat expects ``{"name", "description", "parameters"}``,
not OpenAI's ``{"type": "function", "function": {...}}`` envelope.
:func:`normalize_functions` flattens either shape so a caller can paste either.

**Unreliable placement of the call.** The model does not always populate the
documented ``function_call`` field. In practice a call also shows up in
``additional_kwargs``, under ``tool_calls``, or - often - written out as prose or a
JSON fence inside ``content``. :func:`parse_function_call` checks the structured
locations; :func:`detect_text_function_call` recovers the textual shape so the
caller can either use it directly or re-ask (see ``GigaClient.complete``).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

from .types import FunctionCall, FunctionSpec

_CALL_TAG_RE = re.compile(r"<function_call>\s*(.*?)\s*</function_call>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json|tool_code|tool|python)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_NAME_KEYS = ("name", "function", "function_name", "tool", "tool_name", "action")
_ARG_KEYS = ("arguments", "args", "parameters", "input", "params")


def _loads(text: str) -> Any:
    """Parse JSON, tolerating trailing prose after the value."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        decoder = json.JSONDecoder()
        value, _ = decoder.raw_decode(text)
        return value
    except (json.JSONDecodeError, ValueError):
        return None


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield every balanced ``{...}`` span, ignoring braces inside strings."""
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start : index + 1]
                    start = -1


def _coerce_arguments(value: Any) -> Any:
    """Turn arguments into a dict when they are JSON text, else keep them raw."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = _loads(value)
        return parsed if parsed is not None else value
    return value


def _from_mapping(node: Mapping[str, Any]) -> Optional[FunctionCall]:
    inner = node.get("function")
    if isinstance(inner, Mapping):
        node = inner
    name = None
    for key in _NAME_KEYS:
        candidate = node.get(key)
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()
            break
    if not name:
        return None
    arguments: Any = None
    for key in _ARG_KEYS:
        if key in node:
            arguments = node.get(key)
            break
    return FunctionCall(name=name, arguments=_coerce_arguments(arguments), raw=dict(node))


def _from_field(value: Any) -> Optional[FunctionCall]:
    if value is None:
        return None
    if isinstance(value, FunctionCall):
        return value if value.name else None
    if isinstance(value, str):
        parsed = _loads(value)
        if isinstance(parsed, Mapping):
            return _from_mapping(parsed)
        return None
    if isinstance(value, Mapping):
        return _from_mapping(value)
    return None


def parse_function_call(source: Any) -> Optional[FunctionCall]:
    """Read a structured function call out of a message or delta.

    Checked in order: the documented ``function_call`` field, ``additional_kwargs``,
    then an OpenAI-style ``tool_calls`` list. Returns ``None`` when the message is a
    plain text answer.
    """
    if source is None:
        return None
    if isinstance(source, FunctionCall):
        return source if source.name else None
    if not isinstance(source, Mapping):
        return None

    call = _from_field(source.get("function_call"))
    if call is not None:
        call.raw.setdefault("origin", "function_call")
        return call

    kwargs = source.get("additional_kwargs")
    if isinstance(kwargs, Mapping):
        call = _from_field(kwargs.get("function_call"))
        if call is None:
            call = _from_mapping(kwargs)
        if call is not None:
            call.raw.setdefault("origin", "additional_kwargs")
            return call

    tool_calls = source.get("tool_calls")
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes)):
        for entry in tool_calls:
            call = _from_field(entry)
            if call is not None:
                call.raw.setdefault("origin", "tool_calls")
                return call

    # Some frames carry the call and the content in the same object, unlabelled.
    origin = source.get("origin")
    if isinstance(origin, str) and origin == "legacy":  # pragma: no cover - escape hatch
        return _from_mapping(source)
    return None


def finalize_function_call(name: str, arguments: Any) -> Optional[FunctionCall]:
    """Build a call from accumulated stream fragments."""
    if not name:
        return None
    return FunctionCall(name=name, arguments=_coerce_arguments(arguments), partial=False)


def normalize_functions(functions: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """Flatten any accepted function declaration into GigaChat's flat shape."""
    normalized: List[Dict[str, Any]] = []
    for spec in functions or ():
        normalized.append(FunctionSpec.coerce(spec).to_flat())
    return normalized


def function_names(functions: Optional[Iterable[Any]]) -> List[str]:
    names: List[str] = []
    for spec in functions or ():
        name = FunctionSpec.coerce(spec).name
        if name:
            names.append(name)
    return names


def build_tool_schema(
    name: str,
    description: str = "",
    properties: Optional[Mapping[str, Any]] = None,
    required: Optional[Sequence[str]] = None,
    *,
    returns: Optional[Mapping[str, Any]] = None,
    examples: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Convenience builder for a flat function declaration."""
    parameters: Dict[str, Any] = {"type": "object", "properties": dict(properties or {})}
    if required:
        parameters["required"] = list(required)
    return FunctionSpec(
        name=name,
        description=description,
        parameters=parameters,
        few_shot_examples=examples,
        returns=dict(returns) if returns else None,
    ).to_flat()


def detect_text_function_call(
    content: Optional[str], allowed_names: Optional[Iterable[str]] = None
) -> Optional[FunctionCall]:
    """Recover a call the model wrote as text instead of a structured field.

    Recognises three shapes, in order of specificity: a ``<function_call>`` tag, a
    fenced JSON block, and a bare JSON object anywhere in the body. When
    ``allowed_names`` is given, only those names are accepted, which keeps ordinary
    JSON in an answer from being mistaken for a tool call.
    """
    if not content or not isinstance(content, str):
        return None
    allowed = {name for name in (allowed_names or ()) if name}

    candidates: List[str] = [m.group(1) for m in _CALL_TAG_RE.finditer(content)]
    candidates.extend(m.group(1) for m in _FENCE_RE.finditer(content))
    candidates.append(content)

    for text in candidates:
        if not text:
            continue
        for blob in _balanced_objects(text):
            payload = _loads(blob)
            if not isinstance(payload, Mapping):
                continue
            call = _from_mapping(payload)
            if call is None:
                continue
            if allowed and call.name not in allowed:
                continue
            call.raw.setdefault("origin", "text")
            call.partial = False
            return call
    return None


__all__ = [
    "build_tool_schema",
    "detect_text_function_call",
    "finalize_function_call",
    "function_names",
    "normalize_functions",
    "parse_function_call",
]

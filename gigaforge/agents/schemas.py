"""JSON Schemas for the agents' structured output.

The merge plan (§7.2.3) asks for native structured output instead of the
harness repairing almost-JSON with ``json_repair``.  The transport has carried
``response_format`` end to end since commit ``99967d2``, but nothing ever
*produced* one — so the feature was inert.  This module is the missing source.

Two rules shape the schemas:

* **They must be strict enough to be worth sending.**  A schema that accepts
  anything buys nothing over ``json_repair``; these pin the exact field names
  and the enum of allowed tools, which is precisely what the repair path used
  to guess at.
* **They must be permissive where the model legitimately varies.**  GigaChat
  rejects a schema it cannot satisfy, and a rejected request costs a whole
  round trip.  Optional fields stay optional and no ``additionalProperties:
  false`` is set, so a model that adds a helpful extra key is not punished.

``json_repair`` stays in the code as the *fallback*: a provider that ignores
``response_format`` (or a model that emits a fenced block anyway) must still
produce a usable plan.  Structured output is an optimisation, not a
correctness requirement.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "PLAN_SCHEMA",
    "REVIEW_SCHEMA",
    "REACT_STEP_SCHEMA",
    "json_schema_format",
    "plan_response_format",
    "review_response_format",
    "react_step_response_format",
    "normalize_schema",
    "to_function_spec",
]


def normalize_schema(schema: Any) -> Any:
    """Make *schema* acceptable to GigaChat's function validator.

    Measured against the live API (2026-11-20): a bare ``{"type": "object"}``
    is rejected with

        422 Field 'properties.steps.items.properties.args.properties' is missing

    The server requires every object node to carry a ``properties`` key, even
    when it is empty.  JSON Schema itself allows omitting it, so this is a
    GigaChat-specific quirk that has to be normalised before sending \u2014
    otherwise the whole structured-output path fails with a 422 that looks
    like a schema bug in our code.
    """
    if isinstance(schema, Mapping):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            out[key] = normalize_schema(value)
        if out.get("type") == "object" and "properties" not in out:
            out["properties"] = {}
        return out
    if isinstance(schema, list):
        return [normalize_schema(item) for item in schema]
    return schema


def to_function_spec(
    response_format: Mapping[str, Any],
    *,
    name: str = "structured_output",
    description: str = "Return the answer as a single JSON object.",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Translate a ``response_format`` envelope into GigaChat's function pair.

    **Why this exists.**  The live API accepts ``response_format`` and then
    ignores it: a request carrying a strict JSON Schema still came back as a
    fenced ```` ```json ```` block, and once as free-form prose.  The one
    mechanism the server actually enforces is ``functions`` +
    ``function_call`` \u2014 it validates the schema (returning a precise 422 when
    it cannot) and returns the answer in ``message.function_call.arguments``
    with an **empty** ``content``.

    So a caller that wants real structured output has to send the schema as a
    function.  Returns ``(function_spec, function_call)`` ready to drop into
    the request payload.
    """
    schema = response_format.get("schema") or response_format.get("schema_") or {}
    spec = {
        "name": name,
        "description": description,
        "parameters": normalize_schema(dict(schema)),
    }
    return spec, {"name": name}


def json_schema_format(schema: Mapping[str, Any], *, name: str, strict: bool = True) -> dict[str, Any]:
    """Wrap *schema* in the ``response_format`` envelope GigaChat expects.

    Verified against ``gigachat==0.3.0a1``: ``JsonSchemaResponseFormat`` has
    ``type``, ``schema_`` (aliased to ``schema``) and ``strict``.  A plain dict
    passes through the SDK's passthrough validation unchanged.
    """
    return {
        "type": "json_schema",
        "schema": dict(schema),
        "strict": bool(strict),
    }


#: The planner's answer: an ordered list of tool calls.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": ["read_structure", "edit_function", "run_tests", "grep"],
                    },
                    "args": {"type": "object"},
                    "done": {"type": "boolean"},
                },
                "required": ["tool"],
            },
        }
    },
    "required": ["steps"],
}

#: The reviewer's answer: a verdict plus the issues that produced it.
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "reject"]},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict"],
}

#: One ReAct step: the thought, the tool, and its input.
REACT_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {
            "type": "string",
            "enum": ["grep", "read_structure", "run_tests", "finish"],
        },
        "action_input": {"type": "string"},
    },
    "required": ["action"],
}


def plan_response_format(*, strict: bool = True) -> dict[str, Any]:
    return json_schema_format(PLAN_SCHEMA, name="plan", strict=strict)


def review_response_format(*, strict: bool = True) -> dict[str, Any]:
    return json_schema_format(REVIEW_SCHEMA, name="review", strict=strict)


def react_step_response_format(*, strict: bool = True) -> dict[str, Any]:
    return json_schema_format(REACT_STEP_SCHEMA, name="react_step", strict=strict)

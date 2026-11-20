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

from typing import Any, Mapping

__all__ = [
    "PLAN_SCHEMA",
    "REVIEW_SCHEMA",
    "REACT_STEP_SCHEMA",
    "json_schema_format",
    "plan_response_format",
    "review_response_format",
    "react_step_response_format",
]


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

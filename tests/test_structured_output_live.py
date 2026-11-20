"""Structured output as the LIVE GigaChat API actually implements it.

Measured against the real server on 2026-11-20:

* ``response_format`` is accepted and then **ignored** — a strict JSON Schema
  still came back as a fenced ```` ```json ```` block, and once as free-form
  prose.  Sending it alone buys nothing.
* ``functions`` + ``function_call`` **is** enforced: the server validates the
  schema (422 with a precise field path when it cannot) and returns the answer
  in ``message.function_call.arguments`` with an **empty** ``content``.
* The server requires every object node to carry ``properties``, even an empty
  one.  A bare ``{"type": "object"}`` is a 422.

These tests pin that behaviour so the adapter cannot silently regress to the
mechanism the server ignores.
"""
from __future__ import annotations

from typing import Any

from gigaforge.agents.schemas import (
    PLAN_SCHEMA,
    normalize_schema,
    plan_response_format,
    to_function_spec,
)


def test_normalize_adds_empty_properties_to_bare_objects() -> None:
    """A bare object node is a 422 on the live server; it must gain ``properties``."""
    assert normalize_schema({"type": "object"}) == {"type": "object", "properties": {}}


def test_normalize_recurses_into_nested_objects() -> None:
    """The real 422 pointed at ``steps.items.properties.args.properties``."""
    out = normalize_schema(
        {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"args": {"type": "object"}},
                    },
                }
            },
        }
    )
    args = out["properties"]["steps"]["items"]["properties"]["args"]
    assert args["properties"] == {}


def test_normalize_leaves_non_object_nodes_alone() -> None:
    assert normalize_schema({"type": "string"}) == {"type": "string"}
    assert normalize_schema({"type": "array", "items": {"type": "string"}}) == {
        "type": "array",
        "items": {"type": "string"},
    }


def test_normalize_does_not_mutate_the_input() -> None:
    original = {"type": "object"}
    normalize_schema(original)
    assert original == {"type": "object"}, "the shared schema constant must stay clean"


def test_to_function_spec_wraps_the_schema_as_a_function() -> None:
    spec, call = to_function_spec(plan_response_format(), name="plan")
    assert spec["name"] == "plan"
    assert call == {"name": "plan"}
    assert spec["parameters"]["type"] == "object"
    assert "steps" in spec["parameters"]["properties"]


def test_to_function_spec_normalises_the_parameters() -> None:
    """Every object node in the sent schema must carry ``properties``."""
    spec, _ = to_function_spec(plan_response_format())
    args = spec["parameters"]["properties"]["steps"]["items"]["properties"]["args"]
    assert args["properties"] == {}


def test_to_function_spec_accepts_the_sdk_alias_key() -> None:
    """``JsonSchemaResponseFormat`` exposes the schema as ``schema_`` too."""
    spec, _ = to_function_spec({"type": "json_schema", "schema_": PLAN_SCHEMA})
    assert "steps" in spec["parameters"]["properties"]


def test_from_sdk_completion_lifts_function_arguments_into_content() -> None:
    """A structured answer has empty ``content``; consumers read ``content``."""

    class _Call:
        name = "plan"
        arguments = {"steps": [{"tool": "grep", "args": {"pattern": "TODO"}}]}

    class _Message:
        content = ""
        function_call = _Call()

    class _Choice:
        message = _Message()
        finish_reason = "function_call"

    class _Completion:
        choices = [_Choice()]
        usage = None

    from gigaforge.providers.gigachat_sdk_adapter import from_sdk_completion

    result = from_sdk_completion(_Completion())
    assert result.content, "content must not be empty when arguments are present"
    import json

    parsed: Any = json.loads(result.content)
    assert parsed["steps"][0]["tool"] == "grep"
    assert result.function_call == {
        "name": "plan",
        "arguments": {"steps": [{"tool": "grep", "args": {"pattern": "TODO"}}]},
    }


def test_from_sdk_completion_keeps_plain_content_untouched() -> None:
    """A normal answer must not be rewritten."""

    class _Message:
        content = "hello"
        function_call = None

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Completion:
        choices = [_Choice()]
        usage = None

    from gigaforge.providers.gigachat_sdk_adapter import from_sdk_completion

    result = from_sdk_completion(_Completion())
    assert result.content == "hello"
    assert result.function_call is None

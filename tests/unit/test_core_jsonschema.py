"""The minimal JSON Schema validator used for tool arguments."""

from __future__ import annotations

import re
from typing import Any

import pytest

from switchboard.jsonschema import check_schema, validate
from switchboard.policy import Tier
from switchboard.tools import Tool

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "maxLength": 5},
        "count": {"type": "integer", "minimum": 1, "maximum": 3},
        "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "maxItems": 2},
        "flag": {"type": "boolean"},
    },
    "required": ["name"],
    "additionalProperties": False,
}


def test_valid_instance_has_no_problems() -> None:
    assert validate({"name": "abc", "count": 2, "tags": ["a"], "flag": True}, SCHEMA) == []


def test_problems_are_reported_with_paths() -> None:
    problems = validate({"count": 9, "tags": ["a", "c", "b"], "extra": 1, "flag": 1}, SCHEMA)
    assert "$.name: is required" in problems
    assert "$.count: must be <= 3" in problems
    assert "$.tags: allows at most 2 items" in problems
    assert "$.tags[1]: must be one of ['a', 'b']" in problems
    assert "$.extra: is not an allowed property" in problems
    assert "$.flag: expected boolean" in problems


def test_booleans_are_not_integers() -> None:
    assert validate(True, {"type": "integer"}) == ["$: expected integer"]


def test_patterns_and_type_lists() -> None:
    assert validate("2026-11-11", {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}) == []
    assert validate(None, {"type": ["string", "null"]}) == []
    assert validate(3, {"type": ["string", "null"]}) == ["$: expected string or null"]
    assert validate("toolong", {"type": "string", "maxLength": 3}) == ["$: longer than 3 characters"]


def test_anchored_patterns_reject_a_trailing_newline() -> None:
    schema = {"type": "string", "pattern": "^[0-9a-f]{8}$"}
    assert validate("1234abcd", schema) == []
    assert validate("1234abcd\n", schema) == ["$: does not match pattern '^[0-9a-f]{8}$'"]
    assert validate("costs $5", {"type": "string", "pattern": r"\$"}) == []


def test_object_size_and_property_names() -> None:
    schema = {
        "type": "object",
        "maxProperties": 2,
        "propertyNames": {"type": "string", "pattern": "^[a-z_]+$"},
    }
    assert validate({"a": 1, "b": 2}, schema) == []
    assert validate({"a": 1, "b": 2, "c": 3}, schema) == ["$: allows at most 2 properties"]
    assert validate({"Bad-Key": 1}, schema) == ["$.Bad-Key (name): does not match pattern '^[a-z_]+$'"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_cannot_bypass_bounds(value: float) -> None:
    assert validate(value, {"type": "number", "minimum": 0, "maximum": 100}) == [
        "$: must be a finite JSON number"
    ]


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "object", "anyOf": [{"required": ["name"]}]}, "$: unsupported schema keyword(s): anyOf"),
        (
            {"type": "object", "properties": {"name": {"type": "string", "maxLenght": 10}}},
            "$.properties.name: unsupported schema keyword(s): maxLenght",
        ),
        (
            {"type": "object", "additionalProperties": {"type": "string"}},
            "$.additionalProperties: only booleans are supported",
        ),
        (
            {"type": "object", "properties": {"name": True}},
            "$.properties.name: schemas must be objects in the supported subset",
        ),
        ({"type": "object", "properties": []}, "$.properties: expected a table of property schemas"),
        ({"type": "object", "required": "name"}, "$.required: expected a list of property names"),
        ({"type": "object", "description": 7}, "$.description: expected a string"),
        ({"type": "object", "items": []}, "$.items: schemas must be objects in the supported subset"),
        (
            {"type": "object", "propertyNames": False},
            "$.propertyNames: schemas must be objects in the supported subset",
        ),
        ({"type": "object", "pattern": "["}, "$.pattern: invalid regular expression"),
        ({"type": "object", "pattern": "a{99999999999999999999}"}, "$.pattern: invalid regular expression"),
        ({"type": "object", "pattern": "(" * 5000 + ")" * 5000}, "$.pattern: invalid regular expression"),
        ({"type": "object", "minProperties": True}, "$.minProperties: expected a non-negative integer"),
        ({"type": "object", "maximum": float("inf")}, "$.maximum: expected a finite number"),
        ({"type": "object", "enum": []}, "$.enum: expected a non-empty list"),
        ({"type": "object", "$schema": []}, "$: this subset accepts only the 2020-12 schema identifier"),
        (
            {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"},
            "$: this subset accepts only the 2020-12 schema identifier",
        ),
        (
            {"type": "object", "properties": {"count": {"type": []}}},
            "$.properties.count.type: expected a supported type or non-empty list of types",
        ),
    ],
)
def test_invalid_or_unsupported_tool_schemas_are_refused_at_registration(
    schema: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        Tool("demo_tool", "Demo.", Tier.READ, schema, lambda _args: None)


def test_annotations_are_allowed_but_do_not_become_assertions() -> None:
    schema = {"type": "string", "format": "email", "description": "A contact.", "x-owner": "demo"}
    check_schema(schema)
    assert validate("not-an-email-address", schema) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, 1), (False, 0), ({"flag": True}, {"flag": 1}), ([False], [0])],
)
def test_enum_and_const_keep_booleans_distinct_from_numbers(value: Any, expected: Any) -> None:
    assert validate(value, {"enum": [expected]})
    assert validate(value, {"const": expected})


def test_numeric_equivalence_is_preserved_in_enum_values() -> None:
    assert validate(1, {"enum": [1.0]}) == []
    assert validate({"count": 1.0}, {"const": {"count": 1}}) == []


def test_tool_names_cannot_end_with_a_newline() -> None:
    with pytest.raises(ValueError, match="tool name"):
        Tool("demo_tool\n", "Demo.", Tier.READ, {"type": "object"}, lambda _args: None)

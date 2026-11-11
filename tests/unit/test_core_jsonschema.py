"""The minimal JSON Schema validator used for tool arguments."""

from __future__ import annotations

from switchboard.jsonschema import validate

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

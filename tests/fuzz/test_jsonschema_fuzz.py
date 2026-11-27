"""Seeded fuzzing of ``jsonschema.check_schema`` and ``jsonschema.validate``.

``check_schema`` receives generated definitions with wrong types, unknown keywords, and random patterns, and
must either accept them or raise ``ValueError``. ``validate`` receives constructed valid schemas (whose
patterns are bounded and free of nested quantifiers, because schemas are trusted code) and generated JSON
values; it must return a deterministic list of messages, never raise, and agree with the schema's ``type``,
``enum``, ``const``, ``required``, and ``additionalProperties`` constraints when it reports no problems.
"""

from __future__ import annotations

import copy
import random
from typing import Any

import pytest

from switchboard.jsonschema import check_schema, validate
from switchboard.util import canonical_json, json_equal, json_loads
from tests.fuzz.mutation import SAFE_PATTERNS, generator, iterations, json_value, pattern, text

pytestmark = pytest.mark.fuzz

TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")
NAMES = ("id", "name", "limit", "tags", "on", "note")
KEYWORDS = (
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "propertyNames",
    "items",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
    "title",
    "description",
    "format",
    "$schema",
    "$comment",
    "default",
)
SIZE_KEYWORDS = ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties")
JUNK: tuple[Any, ...] = (None, True, 0, -1, 2.5, float("nan"), float("inf"), "", "string", [], ["string"], {})
EXAMPLES = {
    "^[0-9a-f]{8}$": "0123abcd",
    "^[a-z][a-z0-9_]{0,63}$": "switchboard_status",
    "^[0-9]{4}-[0-9]{2}-[0-9]{2}$": "2026-11-11",
    "^[A-Z]{3}$": "USD",
    "x": "box",
    "^$": "",
}
TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def keyword_value(rng: random.Random, key: str, depth: int) -> Any:
    """A plausible or deliberately wrong value for one keyword."""
    if rng.random() < 0.2:
        return rng.choice(JUNK)
    if key == "type":
        return rng.choice((rng.choice(TYPES), [rng.choice(TYPES), "null"], [], "strin", ["string", 3]))
    if key in SIZE_KEYWORDS:
        return rng.choice((0, 1, 5, 64, -1, 2.5, True, "3", 10**30))
    if key in ("minimum", "maximum"):
        return rng.choice((0, -5, 3.5, float("nan"), float("-inf"), True, "1", 10**400))
    if key == "enum":
        return rng.choice(([], [1, "a", None], "abc", [[1], {"k": True}]))
    if key == "properties":
        return {rng.choice((*NAMES, 7)): random_schema(rng, depth + 1) for _ in range(rng.randint(0, 3))}
    if key == "required":
        return rng.choice((["id"], [], "id", [1], ["id", "name"]))
    if key == "additionalProperties":
        return rng.choice((True, False, {}, "no"))
    if key in ("items", "propertyNames"):
        return random_schema(rng, depth + 1)
    if key == "pattern":
        return pattern(rng) if rng.random() < 0.8 else rng.choice(SAFE_PATTERNS)
    if key == "$schema":
        return rng.choice(
            ("https://json-schema.org/draft/2020-12/schema", "http://json-schema.org/draft-07/schema#", 5)
        )
    return json_value(rng, 2)


def random_schema(rng: random.Random, depth: int = 0) -> Any:
    if depth > 3 or rng.random() < 0.05:
        return rng.choice(JUNK)
    schema: dict[Any, Any] = {}
    for _ in range(rng.randint(0, 4)):
        key: Any = rng.choice((*KEYWORDS, "x-note", "maxLenght", "$ref", 7))
        schema[key] = keyword_value(rng, key, depth) if isinstance(key, str) else None
    return schema


def valid_schema(rng: random.Random, depth: int = 0) -> dict[str, Any]:
    """A definition that the supported subset accepts, with only bounded, linear-time patterns."""
    kind = rng.choice((*TYPES, "any"))
    schema: dict[str, Any] = {}
    if kind != "any":
        schema["type"] = kind if rng.random() < 0.7 else [kind, "null"]
    if kind == "string":
        if rng.random() < 0.4:
            schema["minLength"] = rng.randint(0, 3)
        if rng.random() < 0.4:
            schema["maxLength"] = rng.randint(0, 24)
        if rng.random() < 0.5:
            schema["pattern"] = rng.choice(SAFE_PATTERNS)
    elif kind in ("integer", "number"):
        if rng.random() < 0.5:
            schema["minimum"] = rng.choice((-10, 0, 1.5))
        if rng.random() < 0.5:
            schema["maximum"] = rng.choice((0, 10, 100.25))
    elif kind == "array":
        if rng.random() < 0.4:
            schema["minItems"] = rng.randint(0, 2)
        if rng.random() < 0.4:
            schema["maxItems"] = rng.randint(0, 4)
        if depth < 3 and rng.random() < 0.7:
            schema["items"] = valid_schema(rng, depth + 1)
    elif kind == "object":
        names = rng.sample(NAMES, rng.randint(0, 3))
        if depth < 3:
            schema["properties"] = {name: valid_schema(rng, depth + 1) for name in names}
        if names and rng.random() < 0.5:
            schema["required"] = rng.sample(names, rng.randint(1, len(names)))
        if rng.random() < 0.5:
            schema["additionalProperties"] = rng.random() < 0.5
        if rng.random() < 0.3:
            schema["minProperties"] = rng.randint(0, 2)
        if rng.random() < 0.3:
            schema["maxProperties"] = rng.randint(0, 4)
        if rng.random() < 0.2:
            schema["propertyNames"] = {"type": "string", "maxLength": rng.randint(1, 8)}
    if rng.random() < 0.15:
        schema["enum"] = [json_value(rng, 2) for _ in range(rng.randint(1, 3))]
    if rng.random() < 0.05:
        schema["const"] = json_value(rng, 2)
    if rng.random() < 0.2:
        schema["description"] = "synthetic"
    return schema


def guided_value(rng: random.Random, schema: dict[str, Any], depth: int = 0) -> Any:
    """A value that often, but not always, satisfies ``schema``."""
    if "const" in schema and rng.random() < 0.7:
        return copy.deepcopy(schema["const"])
    if "enum" in schema and rng.random() < 0.7:
        return copy.deepcopy(rng.choice(schema["enum"]))
    if rng.random() < 0.15 or depth > 4:
        return json_value(rng, 2)
    kind = schema.get("type", rng.choice(TYPES))
    if isinstance(kind, list):
        kind = rng.choice(kind)
    if kind == "string":
        example = EXAMPLES.get(schema.get("pattern", ""))
        return example if example is not None and rng.random() < 0.8 else text(rng, 30)
    if kind == "integer":
        return rng.randint(-20, 120)
    if kind == "number":
        return rng.choice((rng.randint(-20, 120), rng.uniform(-20.0, 120.0)))
    if kind == "boolean":
        return rng.random() < 0.5
    if kind == "null":
        return None
    if kind == "array":
        items = schema.get("items", {})
        return [guided_value(rng, items, depth + 1) for _ in range(rng.randint(0, 5))]
    properties: dict[str, Any] = schema.get("properties", {})
    value = {
        name: guided_value(rng, child, depth + 1) for name, child in properties.items() if rng.random() < 0.8
    }
    if rng.random() < 0.2:
        value[text(rng, 6)] = json_value(rng, 2)
    return value


def consistent(instance: Any, schema: dict[str, Any]) -> bool:
    """Constraints that must hold whenever ``validate`` reports no problems."""
    kind = schema.get("type")
    if kind is not None:
        names = [kind] if isinstance(kind, str) else kind
        if not any(TYPE_CHECKS[name](instance) for name in names):
            return False
    if "enum" in schema and not any(json_equal(instance, item) for item in schema["enum"]):
        return False
    if "const" in schema and not json_equal(instance, schema["const"]):
        return False
    if isinstance(instance, dict):
        if any(name not in instance for name in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and any(name not in properties for name in instance):
            return False
    return True


def test_check_schema_accepts_or_raises_value_error() -> None:
    rng = generator("check_schema")
    accepted = rejected = 0
    for _ in range(iterations()):
        schema = random_schema(rng)
        try:
            check_schema(schema)
        except ValueError:
            rejected += 1
        else:
            accepted += 1
    assert accepted > 0
    assert rejected > 0


def test_validate_is_total_deterministic_and_sound() -> None:
    rng = generator("validate")
    valid = invalid = 0
    for _ in range(iterations()):
        schema = valid_schema(rng)
        check_schema(schema)
        instance = guided_value(rng, schema) if rng.random() < 0.8 else json_value(rng)
        problems = validate(instance, schema)
        assert isinstance(problems, list)
        assert all(isinstance(problem, str) for problem in problems)
        assert validate(instance, schema) == problems
        if problems:
            invalid += 1
            continue
        valid += 1
        assert consistent(instance, schema), (schema, instance)
        assert validate(json_loads(canonical_json(instance)), schema) == []
    assert valid > 0
    assert invalid > 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validate_reports_non_finite_numbers(value: float) -> None:
    assert validate(value, {"type": "number"})
    assert validate(value, {})

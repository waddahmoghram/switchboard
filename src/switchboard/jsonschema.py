"""A deliberately small JSON Schema validator for tool arguments.

It supports the keywords tool schemas here use: ``type``, ``enum``, ``const``, ``properties``,
``required``, ``additionalProperties`` (boolean), ``propertyNames``, ``minProperties``,
``maxProperties``, ``items``, ``minLength``, ``maxLength``, ``pattern``, ``minimum``, ``maximum``,
``minItems``, and ``maxItems``. Tool registration checks schema definitions and refuses unsupported
constraints. Recognized annotations, including ``description`` and ``format``, are not assertions.

Patterns follow JSON Schema's search semantics, except that a pattern ending in ``$`` must reach the
very end of the string: Python's ``$`` would otherwise accept a trailing newline.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from switchboard.util import json_equal

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}

_SIZE_KEYWORDS = frozenset(
    {"minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"}
)
_ANNOTATIONS = frozenset(
    {
        "title",
        "description",
        "default",
        "examples",
        "format",
        "deprecated",
        "readOnly",
        "writeOnly",
        "$comment",
        "$id",
    }
)
_KEYWORDS = (
    _SIZE_KEYWORDS
    | _ANNOTATIONS
    | {
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "items",
        "pattern",
        "minimum",
        "maximum",
        "$schema",
    }
)


def check_schema(schema: Any, path: str = "$") -> None:
    """Validate the supported definition subset before a tool can be registered."""
    if not isinstance(schema, Mapping):
        raise ValueError(f"{path}: schemas must be objects in the supported subset")
    unknown = [str(key) for key in schema if key not in _KEYWORDS and not str(key).startswith("x-")]
    if unknown:
        raise ValueError(f"{path}: unsupported schema keyword(s): {', '.join(sorted(unknown))}")
    for key in ("title", "description", "format", "$comment", "$id"):
        if key in schema and not isinstance(schema[key], str):
            raise ValueError(f"{path}.{key}: expected a string")
    if "$schema" in schema and (
        not isinstance(schema["$schema"], str)
        or schema["$schema"]
        not in {
            "https://json-schema.org/draft/2020-12/schema",
            "https://json-schema.org/draft/2020-12/schema#",
        }
    ):
        raise ValueError(f"{path}: this subset accepts only the 2020-12 schema identifier")
    if "type" in schema:
        value = schema["type"]
        names = [value] if isinstance(value, str) else value
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) and name in _TYPE_CHECKS for name in names)
        ):
            raise ValueError(f"{path}.type: expected a supported type or non-empty list of types")
    for key in _SIZE_KEYWORDS:
        if key in schema:
            value = schema[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{path}.{key}: expected a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in schema:
            value = schema[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise ValueError(f"{path}.{key}: expected a finite number")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise ValueError(f"{path}.additionalProperties: only booleans are supported")
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
            raise ValueError(f"{path}.required: expected a list of property names")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError(f"{path}.enum: expected a non-empty list")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise ValueError(f"{path}.pattern: expected a string")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise ValueError(f"{path}.pattern: invalid regular expression") from exc
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping) or not all(isinstance(name, str) for name in properties):
        raise ValueError(f"{path}.properties: expected a table of property schemas")
    for name, child in properties.items():
        check_schema(child, f"{path}.properties.{name}")
    for key in ("items", "propertyNames"):
        if key in schema:
            check_schema(schema[key], f"{path}.{key}")


def validate(instance: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    if isinstance(instance, float) and not math.isfinite(instance):
        return [f"{path}: must be a finite JSON number"]
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(_TYPE_CHECKS.get(name, lambda _v: False)(instance) for name in names):
            return [f"{path}: expected {' or '.join(names)}"]
    if "enum" in schema and not any(json_equal(instance, item) for item in schema["enum"]):
        errors.append(f"{path}: must be one of {list(schema['enum'])}")
    if "const" in schema and not json_equal(instance, schema["const"]):
        errors.append(f"{path}: must equal {schema['const']!r}")
    if isinstance(instance, str):
        errors.extend(_check_string(instance, schema, path))
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    elif isinstance(instance, list):
        errors.extend(_check_array(instance, schema, path))
    elif isinstance(instance, dict):
        errors.extend(_check_object(instance, schema, path))
    return errors


def _check_string(value: str, schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(f"{path}: shorter than {schema['minLength']} characters")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path}: longer than {schema['maxLength']} characters")
    if "pattern" in schema and not _pattern_matches(str(schema["pattern"]), value):
        errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
    return errors


def _pattern_matches(pattern: str, value: str) -> bool:
    match = re.search(pattern, value)
    if match is None:
        return False
    anchored_end = pattern.endswith("$") and not pattern.endswith("\\$")
    return not anchored_end or match.end() == len(value)


def _check_array(value: Sequence[Any], schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if "minItems" in schema and len(value) < schema["minItems"]:
        errors.append(f"{path}: needs at least {schema['minItems']} items")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        errors.append(f"{path}: allows at most {schema['maxItems']} items")
    item_schema = schema.get("items")
    if isinstance(item_schema, Mapping):
        for index, item in enumerate(value):
            errors.extend(validate(item, item_schema, f"{path}[{index}]"))
    return errors


def _check_object(value: Mapping[str, Any], schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if "minProperties" in schema and len(value) < schema["minProperties"]:
        errors.append(f"{path}: needs at least {schema['minProperties']} properties")
    if "maxProperties" in schema and len(value) > schema["maxProperties"]:
        errors.append(f"{path}: allows at most {schema['maxProperties']} properties")
    names_schema = schema.get("propertyNames")
    if isinstance(names_schema, Mapping):
        for name in value:
            errors.extend(validate(name, names_schema, f"{path}.{name} (name)"))
    properties: Mapping[str, Any] = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in value:
            errors.append(f"{path}.{name}: is required")
    for name, item in value.items():
        if name in properties:
            errors.extend(validate(item, properties[name], f"{path}.{name}"))
        elif schema.get("additionalProperties") is False:
            errors.append(f"{path}.{name}: is not an allowed property")
    return errors

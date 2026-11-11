"""A deliberately small JSON Schema validator for tool arguments.

It supports the keywords tool schemas here use: ``type``, ``enum``, ``const``, ``properties``,
``required``, ``additionalProperties`` (boolean), ``propertyNames``, ``minProperties``,
``maxProperties``, ``items``, ``minLength``, ``maxLength``, ``pattern``, ``minimum``, ``maximum``,
``minItems``, and ``maxItems``. Unknown keywords are ignored, so a schema can still carry
``description`` and ``format`` for the assistant's benefit.

Patterns follow JSON Schema's search semantics, except that a pattern ending in ``$`` must reach the
very end of the string: Python's ``$`` would otherwise accept a trailing newline.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def validate(instance: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(_TYPE_CHECKS.get(name, lambda _v: False)(instance) for name in names):
            return [f"{path}: expected {' or '.join(names)}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: must be one of {list(schema['enum'])}")
    if "const" in schema and instance != schema["const"]:
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

"""Bounded input contracts for the opt-in home tools."""

from __future__ import annotations

from typing import Any

from switchboard.errors import ToolError
from switchboard.jsonschema import validate

ENTITY: dict[str, Any] = {
    "type": "string",
    "minLength": 3,
    "maxLength": 255,
    "pattern": "^[a-z0-9_]{1,64}\\.[a-z0-9_]{1,190}$",
}
ROOM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
    "pattern": "^[a-z0-9_]{1,64}$",
}
BINDING: dict[str, Any] = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
    "pattern": "^[0-9a-f]{64}$",
}
LIGHT_FIELDS: dict[str, Any] = {
    "state": {"type": "string", "enum": ["on", "off"], "maxLength": 3},
    "brightness_pct": {"type": "integer", "minimum": 1, "maximum": 100},
    "rgb_color": {
        "type": "array",
        "items": {"type": "integer", "minimum": 0, "maximum": 255},
        "minItems": 3,
        "maxItems": 3,
    },
    "color_temp_kelvin": {"type": "integer", "minimum": 1000, "maximum": 20000},
    "transition": {"type": "number", "minimum": 0, "maximum": 300},
}
EMPTY: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
LIGHTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "binding": BINDING,
        "entity_ids": {"type": "array", "items": ENTITY, "minItems": 1, "maxItems": 16},
        "room": ROOM,
        **LIGHT_FIELDS,
    },
    "required": ["binding", "entity_ids", "state"],
    "additionalProperties": False,
}
CLIMATE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "binding": BINDING,
        "entity_id": ENTITY,
        "room": ROOM,
        "unit": {"type": "string", "enum": ["C", "F"], "maxLength": 1},
        "temperature": {"type": "number", "minimum": -58, "maximum": 212},
        "target_temp_low": {"type": "number", "minimum": -58, "maximum": 212},
        "target_temp_high": {"type": "number", "minimum": -58, "maximum": 212},
        "hvac_mode": {
            "type": "string",
            "enum": ["off", "heat", "cool", "heat_cool", "fan_only"],
            "maxLength": 9,
        },
    },
    "required": ["binding", "entity_id", "unit"],
    "additionalProperties": False,
}
FAN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "binding": BINDING,
        "entity_id": ENTITY,
        "room": ROOM,
        "percentage": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["binding", "entity_id", "percentage"],
    "additionalProperties": False,
}
MEDIA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "binding": BINDING,
        "entity_id": ENTITY,
        "room": ROOM,
        "action": {
            "type": "string",
            "enum": ["play", "pause", "stop", "volume", "mute"],
            "maxLength": 6,
        },
        "volume_level": {"type": "number", "minimum": 0, "maximum": 1},
        "is_volume_muted": {"type": "boolean"},
    },
    "required": ["binding", "entity_id", "action"],
    "additionalProperties": False,
}
PRESET: dict[str, Any] = {
    "type": "object",
    "properties": {
        "binding": BINDING,
        "name": ROOM,
        "fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$", "minLength": 64, "maxLength": 64},
    },
    "required": ["binding", "name", "fingerprint"],
    "additionalProperties": False,
}
SUMMARY: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": ENTITY,
        "room": ROOM,
        "source": {"type": "string", "enum": ["live", "cached"], "maxLength": 6},
        "kind": {
            "type": "string",
            "enum": ["temperature", "humidity", "motion", "leak", "battery", "power", "energy"],
            "maxLength": 11,
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "additionalProperties": False,
}
ANNOUNCE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "binding": BINDING,
        "engine": {**ENTITY, "maxLength": 64},
        "entity_id": {**ENTITY, "maxLength": 64},
        "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 140,
            "pattern": "^[\\x20-\\x7e]{1,140}$",
        },
        "volume_level": {"type": "number", "minimum": 0, "maximum": 1},
        "sensitive": {"type": "boolean"},
    },
    "required": ["binding", "engine", "entity_id", "text", "volume_level", "sensitive"],
    "additionalProperties": False,
}


def checked(args: dict[str, Any], schema: dict[str, Any]) -> None:
    """Keep the same target and value gates for trusted internal callers as for dispatched tools."""
    problems = validate(args, schema)
    if problems:
        raise ToolError("; ".join(problems))


def bound(schema: dict[str, Any], binding: str) -> dict[str, Any]:
    return {**schema, "properties": {**schema["properties"], "binding": {**BINDING, "enum": [binding]}}}


def checked_action(args: dict[str, Any], schema: dict[str, Any], binding: str) -> None:
    checked(args, schema)
    if args["binding"] != binding:
        raise ToolError("home configuration changed; read home_targets and submit a new action for approval")

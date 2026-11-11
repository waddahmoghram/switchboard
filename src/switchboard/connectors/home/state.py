"""Small, secret-free observations; timestamps are not proof that a physical device responded."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from switchboard.errors import ToolError
from switchboard.util import truncate

KEPT_ATTRIBUTES = (
    "friendly_name",
    "device_class",
    "unit_of_measurement",
    "current_temperature",
    "temperature",
    "hvac_action",
    "brightness",
)
_SCALARS = (
    *KEPT_ATTRIBUTES,
    "state_class",
    "temperature_unit",
    "target_temp_low",
    "target_temp_high",
    "min_temp",
    "max_temp",
    "target_temp_step",
    "current_humidity",
    "humidity",
    "percentage",
    "percentage_step",
    "color_mode",
    "color_temp_kelvin",
    "min_color_temp_kelvin",
    "max_color_temp_kelvin",
    "volume_level",
    "is_volume_muted",
    "supported_features",
)
_LISTS = ("rgb_color", "supported_color_modes", "hvac_modes")


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def minimal_state(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve the legacy tool's small result shape."""
    attributes = raw.get("attributes")
    kept = _attributes(attributes, KEPT_ATTRIBUTES)
    changed = raw.get("last_changed")
    return {
        "entity_id": str(raw.get("entity_id", "")),
        "state": truncate(str(raw.get("state", "")), 255),
        "attributes": kept,
        "last_changed": changed if isinstance(changed, str) else None,
    }


def observation(entity_id: str, raw: Mapping[str, Any] | None, observed_at: str) -> dict[str, Any]:
    if raw is None:
        return {
            "entity_id": entity_id,
            "state": None,
            "attributes": {},
            "availability": "missing",
            "last_changed": None,
            "last_updated": None,
            "observed_at": observed_at,
        }
    if raw.get("entity_id") != entity_id or not isinstance(raw.get("state"), str):
        raise ToolError("Home Assistant returned a malformed entity state")
    if not isinstance(raw.get("attributes"), Mapping):
        raise ToolError("Home Assistant returned malformed entity attributes")
    state = raw["state"]
    availability = state if state in {"unknown", "unavailable"} else "available"
    attributes = _attributes(raw["attributes"], _SCALARS)
    for key in _LISTS:
        value = raw["attributes"].get(key)
        if not isinstance(value, list) or len(value) > 16:
            continue
        if (key == "rgb_color" and len(value) == 3 and all(number(item) is not None for item in value)) or (
            key != "rgb_color" and all(isinstance(item, str) and len(item) <= 64 for item in value)
        ):
            attributes[key] = list(value)
    return {
        "entity_id": entity_id,
        "state": truncate(state, 255),
        "attributes": attributes,
        "availability": availability,
        "last_changed": _timestamp(raw.get("last_changed")),
        "last_updated": _timestamp(raw.get("last_updated")),
        "observed_at": observed_at,
    }


def comparable(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """A provider timestamp changing alone is not a new local state change."""
    return {key: snapshot[key] for key in ("state", "attributes", "availability")}


def stale(observed_at: Any, moment: datetime, seconds: int) -> bool:
    if not isinstance(observed_at, str):
        return True
    try:
        parsed = datetime.fromisoformat(observed_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        return True
    age = (moment - parsed).total_seconds()
    return age < 0 or age > seconds


def require_available(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    state = raw.get("state")
    attributes = raw.get("attributes")
    if not isinstance(state, str) or state in {"unknown", "unavailable"}:
        raise ToolError("the target has no available observed state; no action was sent")
    if not isinstance(attributes, Mapping):
        raise ToolError("the target has malformed attributes; no action was sent")
    return attributes


def require_leaf(raw: Mapping[str, Any], intended: tuple[str, ...]) -> None:
    """Refuse reported groups instead of silently controlling their unrequested members."""
    attributes = require_available(raw)
    for key in ("entity_id", "group_members"):
        members = attributes.get(key)
        if members is None:
            continue
        if (
            not isinstance(members, list)
            or len(members) > 16
            or any(not isinstance(member, str) or member not in intended for member in members)
        ):
            raise ToolError("the target reports a group with unrequested members; no action was sent")


def require_feature(attributes: Mapping[str, Any], feature: int, label: str) -> None:
    flags = attributes.get("supported_features")
    if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0 or flags & feature != feature:
        raise ToolError(f"the target does not report support for {label}; no action was sent")


def _attributes(raw: Any, names: tuple[str, ...]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        for key in names:
            value = raw.get(key)
            if isinstance(value, str):
                kept[key] = truncate(value, 200)
            elif isinstance(value, bool) or number(value) is not None:
                kept[key] = value
    return kept


def _timestamp(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 80 else None

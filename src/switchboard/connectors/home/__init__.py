"""Home: read and control the Home Assistant entities you expose, through its REST API.

``home_states`` (read) lists exposed entities with a few attributes. ``home_call_service`` (act) runs
one action on one exposed entity and waits for your approval by default. Locks, alarm panels, covers
(garage doors), and cameras stay blocked unless you change ``deny_domains``. See docs/connectors/home.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from switchboard.connectors.home.client import HomeAssistant
from switchboard.connectors.home.settings import DOMAIN, ENTITY_ID, HomeSettings, load_settings
from switchboard.context import AppContext
from switchboard.errors import ConfigError, SecretError, ToolError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry
from switchboard.util import canonical_json, truncate

NAME = "home"

KEPT_ATTRIBUTES = (
    "friendly_name",
    "device_class",
    "unit_of_measurement",
    "current_temperature",
    "temperature",
    "hvac_action",
    "brightness",
)
# Keys that would let ``data`` aim the action at entities, devices, or areas other than entity_id.
TARGET_KEYS = frozenset({"entity_id", "device_id", "area_id", "floor_id", "label_id"})

_ENTITY_PATTERN = "^[a-z0-9_]{1,64}\\.[a-z0-9_]{1,190}$"
_SLUG_PATTERN = "^[a-z0-9_]{1,64}$"
_TEMPERATURE = {"type": "number", "minimum": -50, "maximum": 100}

# Action fields from Home Assistant's action reference (light.turn_on, climate.set_temperature,
# fan.set_percentage, media_player.volume_set, select.select_option, number.set_value, ...).
DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "brightness_pct": {"type": "integer", "minimum": 0, "maximum": 100},
        "brightness": {"type": "integer", "minimum": 0, "maximum": 255},
        "color_temp_kelvin": {"type": "integer", "minimum": 1000, "maximum": 20000},
        "rgb_color": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 255},
            "minItems": 3,
            "maxItems": 3,
        },
        "transition": {"type": "number", "minimum": 0, "maximum": 300},
        "temperature": _TEMPERATURE,
        "target_temp_low": _TEMPERATURE,
        "target_temp_high": _TEMPERATURE,
        "hvac_mode": {"type": "string", "pattern": "^[a-z_]{1,32}$"},
        "preset_mode": {"type": "string", "minLength": 1, "maxLength": 64},
        "fan_mode": {"type": "string", "minLength": 1, "maxLength": 64},
        "percentage": {"type": "integer", "minimum": 0, "maximum": 100},
        "humidity": {"type": "integer", "minimum": 0, "maximum": 100},
        "volume_level": {"type": "number", "minimum": 0, "maximum": 1},
        "is_volume_muted": {"type": "boolean"},
        "source": {"type": "string", "minLength": 1, "maxLength": 100},
        "option": {"type": "string", "minLength": 1, "maxLength": 100},
        "value": {
            "type": ["number", "string", "boolean"],
            "maxLength": 255,
            "minimum": -1_000_000,
            "maximum": 1_000_000,
        },
    },
    "additionalProperties": False,
}

STATES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {"type": "string", "pattern": _ENTITY_PATTERN, "description": "One exposed entity."},
        "domain": {
            "type": "string",
            "pattern": _SLUG_PATTERN,
            "description": "Only this domain, e.g. light.",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "additionalProperties": False,
}

CALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "pattern": _SLUG_PATTERN, "description": "Same as the entity's domain."},
        "service": {"type": "string", "pattern": _SLUG_PATTERN, "description": "For example turn_on."},
        "entity_id": {"type": "string", "pattern": _ENTITY_PATTERN},
        "data": DATA_SCHEMA,
    },
    "required": ["domain", "service", "entity_id"],
    "additionalProperties": False,
}


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    settings = load_settings(ctx.connector_config(NAME))
    client = HomeAssistant(ctx, settings)
    registry.add(
        Tool(
            name="home_states",
            description=(
                "List the Home Assistant entities the user exposed (or one of them) with their state and a "
                "few attributes. Entities that are not exposed, and blocked domains such as locks and "
                "cameras, are invisible."
            ),
            tier=Tier.READ,
            input_schema=STATES_SCHEMA,
            handler=_states(client, settings),
            title="Home states",
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            name="home_call_service",
            description=(
                "Run one Home Assistant action on one exposed entity, for example domain light, service "
                'turn_on, entity_id light.kitchen, data {"brightness_pct": 40}. The domain must match the '
                "entity's domain. Locks, alarm panels, covers (garage doors), and cameras are blocked by "
                "default. The user approves each call unless they configured otherwise."
            ),
            tier=Tier.ACT,
            input_schema=CALL_SCHEMA,
            handler=_call_service(client, settings),
            title="Control a home device",
            summarize=summarize_call,
            connector=NAME,
        )
    )


def doctor(ctx: AppContext) -> list[str]:
    try:
        settings = load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    problems: list[str] = []
    try:
        ctx.secret(settings.token)
    except SecretError as exc:
        problems.append(f"token: {exc}")
    if not settings.expose:
        problems.append("expose is empty, so the tools can see no entities")
    return problems


def summarize_call(args: Mapping[str, Any]) -> str:
    data = args.get("data") or {}
    detail = f" with {truncate(canonical_json(data), 120)}" if data else ""
    return f"Home Assistant {args.get('domain')}.{args.get('service')} on {args.get('entity_id')}{detail}"


def minimal_state(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Entity id, state, last change, and only the attributes in KEPT_ATTRIBUTES (scalars only)."""
    attributes = raw.get("attributes")
    kept: dict[str, Any] = {}
    if isinstance(attributes, Mapping):
        for key in KEPT_ATTRIBUTES:
            value = attributes.get(key)
            if isinstance(value, str):
                kept[key] = truncate(value, 200)
            elif isinstance(value, (bool, int, float)):
                kept[key] = value
    changed = raw.get("last_changed")
    return {
        "entity_id": str(raw.get("entity_id", "")),
        "state": truncate(str(raw.get("state", "")), 255),
        "attributes": kept,
        "last_changed": changed if isinstance(changed, str) else None,
    }


def _states(client: HomeAssistant, settings: HomeSettings) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        entity_id = args.get("entity_id")
        if entity_id is not None:
            _require(ENTITY_ID, entity_id, "entity_id")
            if not settings.visible(entity_id):
                raise ToolError(f"{entity_id} is not exposed to assistants")
            return {"entities": [minimal_state(client.state(entity_id))], "truncated": False}
        domain = args.get("domain")
        if domain is not None:
            _require(DOMAIN, domain, "domain")
        limit = int(args.get("limit", 100))
        entities = sorted(
            (
                minimal_state(item)
                for item in client.states()
                if settings.visible(item["entity_id"])
                and (domain is None or item["entity_id"].startswith(f"{domain}."))
            ),
            key=lambda state: str(state["entity_id"]),
        )
        return {"entities": entities[:limit], "truncated": len(entities) > limit}

    return handler


def _call_service(
    client: HomeAssistant, settings: HomeSettings
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        domain = _require(DOMAIN, args["domain"], "domain")
        service = _require(DOMAIN, args["service"], "service")
        entity_id = _require(ENTITY_ID, args["entity_id"], "entity_id")
        entity_domain = entity_id.partition(".")[0]
        if entity_domain in settings.deny_domains:
            raise ToolError(f"{entity_domain} entities are blocked by deny_domains")
        if domain != entity_domain:
            raise ToolError(f"domain must be {entity_domain} to act on {entity_id}")
        if not settings.visible(entity_id):
            raise ToolError(f"{entity_id} is not exposed to assistants")
        data = dict(args.get("data") or {})
        if TARGET_KEYS & data.keys():
            raise ToolError("data cannot choose targets; name the entity in entity_id")
        changed = client.call_service(domain, service, {"entity_id": entity_id, **data})
        return {
            "called": f"{domain}.{service}",
            "entity_id": entity_id,
            "changed": [minimal_state(item) for item in changed if settings.visible(item["entity_id"])],
        }

    return handler


def _require(pattern: re.Pattern[str], value: Any, label: str) -> str:
    """The JSON Schema pattern is a hint for clients; this full match is the gate before a URL path."""
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ToolError(f"{label} has an unexpected form")
    return value

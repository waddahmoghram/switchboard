"""Opt-in direct Shelly status and approval-gated controls for explicitly configured safe plugs."""

from __future__ import annotations

from switchboard.context import AppContext
from switchboard.errors import ConfigError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry

from .service import ShellyTools
from .settings import MAX_SWITCH_ID, load_settings

NAME = "shelly"
MIGRATIONS: list[tuple[int, tuple[str, ...]]] = []


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Register tools offline; no secret resolution, network, jobs, events or named services."""
    settings = load_settings(ctx.connector_config(NAME))
    tools = ShellyTools(ctx, settings)
    devices = [device.name for device in settings.devices]
    ids = sorted({switch_id for device in settings.devices for switch_id in device.switch_ids})
    device_schema = {"type": "string", "minLength": 1, "maxLength": 32, "enum": devices}
    id_schema = {"type": "integer", "minimum": 0, "maximum": MAX_SWITCH_ID, "enum": ids}
    registry.add(
        Tool(
            name="shelly_devices",
            description=(
                "List configured Shelly aliases, permitted switch identifiers and safe-control target "
                "fingerprints. Configuration only, not current device health; no addresses or secrets."
            ),
            tier=Tier.READ,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=tools.devices,
            title="Configured Shelly devices",
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            name="shelly_status",
            description=(
                "Read one explicitly permitted Shelly switch from a fixed configured device. Returns output, "
                "available power in watts and cumulative energy in watt-hours, not billing or energy history."
            ),
            tier=Tier.READ,
            input_schema={
                "type": "object",
                "properties": {"device": device_schema, "switch_id": id_schema},
                "required": ["device", "switch_id"],
                "additionalProperties": False,
            },
            handler=tools.status,
            title="Read Shelly switch status",
            connector=NAME,
        )
    )
    controlled = [device for device in settings.devices if device.controls]
    if not controlled:
        return
    registry.add(
        Tool(
            name="shelly_switch_set",
            description=(
                "Explicitly set ON or OFF on a permitted low-power, nonessential-lighting plug. Use the "
                "current target fingerprint from shelly_devices. Approval is required by default. No toggle, "
                "timers, heaters, security equipment, arbitrary operations or configuration changes. "
                "Inspect ok, command_accepted and observation; a returned report alone is not success."
            ),
            tier=Tier.ACT,
            input_schema={
                "type": "object",
                "properties": {
                    "device": {**device_schema, "enum": [device.name for device in controlled]},
                    "switch_id": {
                        **id_schema,
                        "enum": sorted(
                            {control.switch_id for device in controlled for control in device.controls}
                        ),
                    },
                    "target": {
                        "type": "string",
                        "minLength": 64,
                        "maxLength": 64,
                        "pattern": "^[0-9a-f]{64}$",
                        "enum": [
                            device.target(control) for device in controlled for control in device.controls
                        ],
                    },
                    "on": {"type": "boolean"},
                },
                "required": ["device", "switch_id", "target", "on"],
                "additionalProperties": False,
            },
            handler=tools.set_output,
            summarize=tools.summarize,
            title="Set a safe Shelly plug output",
            connector=NAME,
        )
    )


def doctor(ctx: AppContext) -> list[str]:
    """Check configuration only; successful diagnostics do not prove reachability or authentication."""
    try:
        load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    return []

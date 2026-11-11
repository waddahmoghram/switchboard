"""Opt-in local WLED controls with fixed targets and observed-state confirmation."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from switchboard.connectors.wled.client import (
    COLOR_SCHEMA,
    WledClient,
    matches,
    public_snapshot,
    validate_patch,
)
from switchboard.connectors.wled.settings import (
    NAME_PATTERN,
    Device,
    Preset,
    Settings,
    load_settings,
    target_digest,
)
from switchboard.context import AppContext
from switchboard.errors import ConfigError, ToolError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry
from switchboard.util import canonical_json

NAME = "wled"
_SEGMENT: dict[str, Any] = {"type": "integer", "minimum": 0, "maximum": 63}
_BRIGHTNESS: dict[str, Any] = {"type": "integer", "minimum": 1, "maximum": 255}
_TRANSITION: dict[str, Any] = {"type": "integer", "minimum": 0, "maximum": 100}


class WledTools:
    """Tool handlers only; no named service bypasses the policy gate."""

    def __init__(self, ctx: AppContext, settings: Settings) -> None:
        self.ctx = ctx
        self.settings = settings

    def devices(self, _args: dict[str, Any]) -> dict[str, Any]:
        """List configured aliases without contacting devices."""
        return {
            "devices": [
                {
                    "device": device.name,
                    "target_digest": target_digest(self.settings, device),
                    "segments": list(device.segments),
                    "global_control": device.allow_global,
                    "presets": [{"name": preset.name, "id": preset.id} for preset in device.presets],
                }
                for device in self.settings.devices
            ],
            "live_discovery": False,
            "polling": False,
        }

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        device = self._device(args["device"])
        return public_snapshot(device, WledClient(self.ctx, self.settings, device).read())

    def power(self, args: dict[str, Any]) -> dict[str, Any]:
        device = self._action_device(args)
        patch = self._targeted(device, args, {"on": args["on"]})
        return self._perform(device, "set_power", patch)

    def brightness(self, args: dict[str, Any]) -> dict[str, Any]:
        device = self._action_device(args)
        patch = self._targeted(device, args, {"bri": args["brightness"]})
        return self._perform(device, "set_brightness", patch)

    def color(self, args: dict[str, Any]) -> dict[str, Any]:
        device = self._action_device(args)
        patch = self._targeted(device, args, {"col": [args["color"]], "fx": 0})
        return self._perform(device, "set_color", patch)

    def transition(self, args: dict[str, Any]) -> dict[str, Any]:
        device = self._action_device(args)
        patch = self._targeted(device, args, {"transition": args["transition_deciseconds"]})
        return self._perform(device, "set_transition", patch)

    def preset(self, args: dict[str, Any]) -> dict[str, Any]:
        device = self._action_device(args)
        preset = next((preset for preset in device.presets if preset.name == args["preset"]), None)
        if preset is None:
            raise ToolError("WLED preset is not configured for this device")
        return self._perform(device, "apply_preset", None, preset=preset)

    def summarize(self, operation: str, args: Mapping[str, Any]) -> str:
        target = f"segment {args['segment']}" if "segment" in args else "the whole device (all segments)"
        values = {
            key: value for key, value in args.items() if key not in {"device", "segment", "target_digest"}
        }
        if operation == "apply_preset":
            target = "only the reviewed static preset scopes; no firmware macro execution"
        return f"WLED {args['device']}: {operation} on {target}, {canonical_json(values)}"

    def _device(self, name: str) -> Device:
        device = next((device for device in self.settings.devices if device.name == name), None)
        if device is None:
            raise ToolError("WLED device is not configured")
        return device

    def _action_device(self, args: dict[str, Any]) -> Device:
        device = self._device(args["device"])
        if args.get("target_digest") != target_digest(self.settings, device):
            raise ToolError("WLED configuration changed; read devices again and request fresh approval")
        return device

    @staticmethod
    def _targeted(device: Device, args: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        if "segment" in args:
            if args["segment"] not in device.segments:
                raise ToolError("WLED target segment is not configured")
            return {"seg": [{"id": args["segment"], **patch}]}
        if not device.allow_global:
            raise ToolError("WLED global control is not enabled for this device")
        return patch

    def _perform(
        self,
        device: Device,
        operation: str,
        patch: dict[str, Any] | None,
        *,
        preset: Preset | None = None,
    ) -> dict[str, Any]:
        client = WledClient(self.ctx, self.settings, device)
        if preset is not None:
            patch = client.preset(preset)
        if patch is None:
            raise ToolError("WLED action has no static state changes")
        before = client.read()
        validate_patch(device, before, patch)
        result: dict[str, Any] = {
            "device": device.name,
            "target_digest": target_digest(self.settings, device),
            "operation": operation,
            "requested": patch,
            "attempted": True,
            "status": "requested",
            "confirmed": False,
            "confirmation": "unconfirmed",
            "effect_unknown": True,
            "physical_output_verified": False,
            "acknowledged": None,
            "observation": None,
            "stale": True,
        }
        if preset is not None:
            result["preset"] = {"name": preset.name, "id": preset.id, "sha256": preset.sha256}
        try:
            result["acknowledged"] = client.write(patch)
        except ToolError as exc:
            result.update(status="error", error=str(exc))
        else:
            try:
                after = client.read()
            except ToolError as exc:
                result.update(status="unconfirmed", error=f"WLED readback unavailable: {exc}")
            else:
                confirmed = matches(after, patch)
                result.update(
                    status="confirmed" if confirmed else "unconfirmed",
                    confirmed=confirmed,
                    confirmation="state_matched" if confirmed else "state_mismatch",
                    effect_unknown=not confirmed,
                    observation=public_snapshot(device, after),
                    stale=False,
                )
        self.ctx.events.emit(NAME, "wled.action_result", result)
        return result


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Register tools offline; devices and presets are contacted only inside dispatched operations."""
    settings = load_settings(ctx.connector_config(NAME))
    handlers = WledTools(ctx, settings)
    device = {
        "type": "string",
        "pattern": NAME_PATTERN,
        "maxLength": 32,
        "enum": [device.name for device in settings.devices],
    }

    def schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"device": device, **properties},
            "required": ["device", *required],
            "additionalProperties": False,
        }

    registry.add(
        Tool(
            name="wled_devices",
            description="List configured WLED aliases, control scopes and reviewed preset names, offline.",
            tier=Tier.READ,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=handlers.devices,
            connector=NAME,
        )
    )
    binding = {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$",
        "minLength": 64,
        "maxLength": 64,
        "enum": [target_digest(settings, device) for device in settings.devices],
        "description": "Copy this device's current target_digest from wled_devices or wled_status.",
    }
    registry.add(
        Tool(
            name="wled_status",
            description=(
                "Read one configured local WLED device's current state and segment capabilities. "
                "No discovery, arbitrary addresses, configuration, or cached history."
            ),
            tier=Tier.READ,
            input_schema=schema({}, []),
            handler=handlers.status,
            connector=NAME,
        )
    )
    actions = (
        (
            "set_power",
            "Explicitly set on or off; never toggle. Omit segment only for enabled whole-device control.",
            schema({"on": {"type": "boolean"}, "segment": _SEGMENT}, ["on"]),
            handlers.power,
        ),
        (
            "set_brightness",
            "Set brightness 1-255 without implicitly powering on. Use set_power to turn off.",
            schema({"brightness": _BRIGHTNESS, "segment": _SEGMENT}, ["brightness"]),
            handlers.brightness,
        ),
        (
            "set_color",
            "Set one configured segment's fixed red/green/blue[/white] color and Solid effect. "
            "No random colors, effect selection, segment creation or implicit power-on.",
            schema({"segment": _SEGMENT, "color": COLOR_SCHEMA}, ["segment", "color"]),
            handlers.color,
        ),
        (
            "set_transition",
            "Set the whole-device persistent transition duration, 0-100 deciseconds (0-10 seconds). "
            "Readback verifies the setting, not a physical fade.",
            schema({"transition_deciseconds": _TRANSITION}, ["transition_deciseconds"]),
            handlers.transition,
        ),
        (
            "apply_preset",
            "Apply only a configured, fingerprint-pinned static preset's reviewed state. "
            "Does not execute firmware preset identifiers, macros, playlists or arbitrary commands.",
            schema(
                {"preset": {"type": "string", "pattern": NAME_PATTERN, "maxLength": 32}},
                ["preset"],
            ),
            handlers.preset,
        ),
    )
    for operation, description, input_schema, handler in actions:
        input_schema["properties"]["target_digest"] = binding
        input_schema["required"].append("target_digest")
        registry.add(
            Tool(
                name=f"wled_{operation}",
                description=f"{description} Requires approval by default; inspect result.confirmed.",
                tier=Tier.ACT,
                input_schema=input_schema,
                handler=handler,
                summarize=partial(handlers.summarize, operation),
                connector=NAME,
            )
        )


def doctor(ctx: AppContext) -> list[str]:
    """Check configuration only; do not resolve secrets, discover devices, or prove reachability."""
    try:
        load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    return []

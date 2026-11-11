"""Reviewed leaf-device actions with bounded requests and one-shot observed-state readback."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.connectors.home import schemas
from switchboard.connectors.home.client import HomeAssistant
from switchboard.connectors.home.settings import HomeSettings
from switchboard.connectors.home.state import (
    minimal_state,
    number,
    observation,
    require_available,
    require_feature,
    require_leaf,
)
from switchboard.errors import ToolError
from switchboard.util import json_equal, now

_COLOR_MODES = frozenset({"rgb", "rgbw", "rgbww", "hs", "xy"})
_DIMMABLE = _COLOR_MODES | {"brightness", "color_temp", "white"}
_LIGHT_DATA = frozenset({"brightness_pct", "rgb_color", "color_temp_kelvin", "transition"})
_TEMPERATURES = frozenset({"temperature", "target_temp_low", "target_temp_high"})
_UNITS = {"C": "\u00b0C", "F": "\u00b0F"}
_MEDIA_ACTIONS: dict[str, tuple[str, int, dict[str, Any]]] = {
    "play": ("media_play", 16384, {"state": "playing"}),
    "pause": ("media_pause", 1, {"state": "paused"}),
    "stop": ("media_stop", 4096, {"state": ("idle", "off")}),
    "volume": ("volume_set", 4, {}),
    "mute": ("volume_mute", 8, {}),
}


@dataclass(frozen=True)
class ActionPlan:
    domain: str
    service: str
    entities: tuple[str, ...]
    data: dict[str, Any]
    expected: Mapping[str, Mapping[str, Any]]
    asynchronous: bool = False


class HomeControls:
    def __init__(self, client: HomeAssistant, settings: HomeSettings) -> None:
        self.client = client
        self.settings = settings

    def lights(self, args: dict[str, Any]) -> dict[str, Any]:
        schemas.checked_action(args, schemas.LIGHTS, self.settings.binding)
        return self.execute(self._light_plan(tuple(args["entity_ids"]), args, args.get("room")))

    def climate(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execute(self._climate_plan(args))

    def fan(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execute(self._fan_plan(args))

    def media(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execute(self._media_plan(args))

    def preset(self, args: dict[str, Any]) -> dict[str, Any]:
        schemas.checked_action(args, schemas.PRESET, self.settings.binding)
        preset = self.settings.lighting_presets.get(args["name"])
        if preset is None or preset.fingerprint != args["fingerprint"]:
            raise ToolError(
                "the lighting preset is missing or changed; read home_targets before requesting it again"
            )
        plans = [self._light_plan((action.entity_id,), action.arguments()) for action in preset.actions]
        results: list[dict[str, Any]] = []
        for plan in plans:
            result = self.execute(plan)
            results.append(result)
            if result["request_status"] != "accepted":
                break
        not_attempted = [plan.entities[0] for plan in plans[len(results) :]]
        confirmed = not not_attempted and all(result["confirmed"] for result in results)
        return {
            "name": args["name"],
            "fingerprint": preset.fingerprint,
            "status": "observed" if confirmed else "requested_unconfirmed",
            "confirmed": confirmed,
            "results": results,
            "not_attempted": not_attempted,
            "atomic": False,
            "retry_safe": False,
        }

    def require_legacy_target(self, domain: str, entity_id: str) -> None:
        """Newly reviewed targets must use configuration-bound tools, never the legacy escape hatch."""
        tools = {
            "light": "home_lights",
            "climate": "home_climate",
            "fan": "home_fan",
            "media_player": "home_media",
        }
        profiled = (
            entity_id in self.settings.lights
            or entity_id in self.settings.climate
            or entity_id in self.settings.fans
            or entity_id in self.settings.media_players
            or (domain in tools and any(entity_id in members for members in self.settings.rooms.values()))
        )
        if profiled:
            raise ToolError(
                f"{entity_id} has a reviewed home profile; use {tools[domain]} "
                "with the current home_targets binding"
            )
        if domain == "tts" and self.settings.speech.enabled:
            raise ToolError("use the explicit approval-gated home_announce tool for speech")

    def execute(self, plan: ActionPlan) -> dict[str, Any]:
        target: str | list[str] = plan.entities[0] if len(plan.entities) == 1 else list(plan.entities)
        receipt = self.client.submit(plan.domain, plan.service, {"entity_id": target, **plan.data})
        observations: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        if receipt.accepted is not False:
            for entity_id in plan.entities:
                try:
                    raw = self.client.state(entity_id)
                    snapshot = observation(entity_id, raw, now().isoformat(timespec="microseconds"))
                    if snapshot["availability"] == "available":
                        require_leaf(raw, (entity_id,))
                    observations.append(
                        {
                            **snapshot,
                            "matches_request": matches(snapshot, plan.expected[entity_id]),
                            "within_profile": self._within_profile(plan, snapshot),
                        }
                    )
                except ToolError:
                    errors.append(
                        {"entity_id": entity_id, "error": "readback failed; the effect was not resent"}
                    )
        confirmed = (
            receipt.accepted is True
            and not plan.asynchronous
            and not errors
            and len(observations) == len(plan.entities)
            and all(snapshot["matches_request"] and snapshot["within_profile"] for snapshot in observations)
        )
        status = "observed" if confirmed else "requested_unconfirmed"
        if receipt.accepted is False:
            status = "rejected"
        return {
            "called": f"{plan.domain}.{plan.service}",
            "entity_ids": list(plan.entities),
            "request_status": "accepted"
            if receipt.accepted is True
            else ("rejected" if receipt.accepted is False else "unknown"),
            "status": status,
            "confirmed": confirmed,
            "confirmation_scope": "reported requested state, not physical completion or causal proof",
            "asynchronous": plan.asynchronous,
            "observations": observations,
            "changed": [
                minimal_state(item) for item in receipt.changed if item["entity_id"] in plan.entities
            ],
            "error": receipt.error,
            "readback_errors": errors,
            "retry_safe": False,
        }

    def _within_profile(self, plan: ActionPlan, snapshot: Mapping[str, Any]) -> bool:
        entity_id = snapshot["entity_id"]
        attributes = snapshot["attributes"]
        if snapshot["availability"] != "available":
            return False
        if plan.domain == "climate" and "temperature_unit" in plan.expected[entity_id]:
            climate = self.settings.climate[entity_id]
            lower, upper = number(attributes.get("min_temp")), number(attributes.get("max_temp"))
            if lower is None or upper is None or attributes.get("temperature_unit") != _UNITS[climate.unit]:
                return False
            values: dict[str, float] = {}
            for key in _TEMPERATURES & plan.expected[entity_id].keys():
                value = number(attributes.get(key))
                if value is None or not max(climate.minimum, lower) <= value <= min(climate.maximum, upper):
                    return False
                values[key] = value
            if "target_temp_low" in values and values["target_temp_low"] > values["target_temp_high"]:
                return False
        elif plan.domain == "fan":
            fan = self.settings.fans[entity_id]
            percentage = number(attributes.get("percentage"))
            if percentage is None or not fan.minimum <= percentage <= fan.maximum:
                return False
        elif plan.domain == "media_player" and (
            plan.service in {"volume_set", "media_play"} or plan.data.get("is_volume_muted") is False
        ):
            media = self.settings.media_players[entity_id]
            volume = number(attributes.get("volume_level"))
            if volume is None or not media.minimum <= volume <= media.maximum:
                return False
        elif plan.domain == "light":
            if "brightness" in plan.expected[entity_id]:
                brightness = number(attributes.get("brightness"))
                if brightness is None or not 0 <= brightness <= 255:
                    return False
            if "color_temp_kelvin" in plan.data:
                kelvin = number(attributes.get("color_temp_kelvin"))
                lower = number(attributes.get("min_color_temp_kelvin"))
                upper = number(attributes.get("max_color_temp_kelvin"))
                if kelvin is None or lower is None or upper is None or not lower <= kelvin <= upper:
                    return False
        return True

    def target(self, entity_id: str, domain: str, room: str | None = None) -> None:
        approved: Collection[str]
        approved = {
            "light": self.settings.lights,
            "climate": self.settings.climate,
            "fan": self.settings.fans,
            "media_player": self.settings.media_players,
        }[domain]
        if entity_id not in approved or not self.settings.visible(entity_id):
            raise ToolError(f"{entity_id} is not an explicitly approved {domain} target")
        if room is not None:
            members = self.settings.rooms.get(room)
            if members is None or entity_id not in members:
                raise ToolError("the explicit target is not in that approved room alias")

    def read_target(self, entity_id: str) -> dict[str, Any]:
        raw = self.client.state(entity_id)
        require_leaf(raw, (entity_id,))
        return raw

    def safe_volume(self, entity_id: str, attributes: Mapping[str, Any]) -> float:
        limits = self.settings.media_players[entity_id]
        volume = number(attributes.get("volume_level"))
        if volume is None or not limits.minimum <= volume <= limits.maximum:
            raise ToolError("the observed speaker volume is unknown or outside its configured safe bounds")
        return volume

    def _light_plan(
        self, entities: tuple[str, ...], args: Mapping[str, Any], room: str | None = None
    ) -> ActionPlan:
        if len(set(entities)) != len(entities):
            raise ToolError("light targets cannot contain duplicates")
        if args["state"] == "off" and set(args) & (_LIGHT_DATA - {"transition"}):
            raise ToolError("turning lights off cannot also set brightness or color")
        if "rgb_color" in args and "color_temp_kelvin" in args:
            raise ToolError("choose a color or a color temperature, not both")
        for entity_id in entities:
            self.target(entity_id, "light", room)
        transition = args.get("transition", 0)
        data = {key: args[key] for key in _LIGHT_DATA if key in args}
        expected: dict[str, dict[str, Any]] = {}
        for entity_id in entities:
            raw = self.read_target(entity_id)
            attributes = require_available(raw)
            modes = attributes.get("supported_color_modes")
            supported = (
                set(modes)
                if isinstance(modes, list) and all(isinstance(mode, str) for mode in modes)
                else set()
            )
            wanted: dict[str, Any] = {"state": args["state"]}
            if "brightness_pct" in args:
                if not supported & _DIMMABLE:
                    raise ToolError("the light does not report brightness support")
                wanted["brightness"] = round(args["brightness_pct"] * 255 / 100)
            if "rgb_color" in args:
                if not supported & _COLOR_MODES:
                    raise ToolError("the light does not report color support")
                wanted["rgb_color"] = args["rgb_color"]
            if "color_temp_kelvin" in args:
                lower = number(attributes.get("min_color_temp_kelvin"))
                upper = number(attributes.get("max_color_temp_kelvin"))
                kelvin = args["color_temp_kelvin"]
                if (
                    "color_temp" not in supported
                    or lower is None
                    or upper is None
                    or not lower <= kelvin <= upper
                ):
                    raise ToolError("the requested kelvin value is outside the light's reported capability")
                wanted["color_temp_kelvin"] = kelvin
                wanted["color_mode"] = "color_temp"
            if transition > 0:
                require_feature(attributes, 32, "lighting transitions")
            expected[entity_id] = wanted
        return ActionPlan("light", f"turn_{args['state']}", entities, data, expected, transition > 0)

    def _climate_plan(self, args: dict[str, Any]) -> ActionPlan:
        schemas.checked_action(args, schemas.CLIMATE, self.settings.binding)
        entity_id = args["entity_id"]
        self.target(entity_id, "climate", args.get("room"))
        limits = self.settings.climate[entity_id]
        if args["unit"] != limits.unit:
            raise ToolError(
                "the requested climate unit differs from the configured unit; no conversion is performed"
            )
        temperatures = {key: args[key] for key in _TEMPERATURES if key in args}
        mode = args.get("hvac_mode")
        if not temperatures and mode is None:
            raise ToolError("supply a temperature, a complete target range, or a climate mode")
        if ("temperature" in temperatures and len(temperatures) != 1) or (
            "temperature" not in temperatures
            and temperatures.keys() not in (set(), {"target_temp_low", "target_temp_high"})
        ):
            raise ToolError("use one temperature or both ordered range endpoints")
        if (
            "target_temp_low" in temperatures
            and temperatures["target_temp_low"] > temperatures["target_temp_high"]
        ):
            raise ToolError("the lower target temperature must not exceed the upper target")
        if mode is not None and mode not in limits.modes:
            raise ToolError("the climate mode is not in the device's reviewed modes")
        raw = self.read_target(entity_id)
        attributes = require_available(raw)
        if mode is not None:
            supported_modes = attributes.get("hvac_modes")
            if not isinstance(supported_modes, list) or mode not in supported_modes:
                raise ToolError("the climate device does not report support for that mode")
        wanted = dict(temperatures)
        if temperatures or mode in {"heat", "cool", "heat_cool"}:
            effective_mode = mode if mode is not None else raw["state"]
            if effective_mode not in limits.modes or effective_mode not in {"heat", "cool", "heat_cool"}:
                raise ToolError("temperature adjustments need a reviewed heating or cooling mode")
            ranged = effective_mode == "heat_cool"
            if temperatures and ("target_temp_low" in temperatures) != ranged:
                raise ToolError(
                    "heat_cool requires a complete target range; heat/cool require one temperature"
                )
            require_feature(attributes, 2 if ranged else 1, "the requested climate setpoint")
            if attributes.get("temperature_unit") != _UNITS[limits.unit]:
                raise ToolError(
                    "the observed climate temperature unit is missing or differs; no conversion is performed"
                )
            lower, upper = number(attributes.get("min_temp")), number(attributes.get("max_temp"))
            if lower is None or upper is None:
                raise ToolError("the climate device does not report temperature bounds")
            keys = ("target_temp_low", "target_temp_high") if ranged else ("temperature",)
            candidates = temperatures or {key: attributes.get(key) for key in keys}
            values: dict[str, float] = {}
            for key, candidate in candidates.items():
                value = number(candidate)
                previous = number(attributes.get(key))
                if value is None or not max(limits.minimum, lower) <= value <= min(limits.maximum, upper):
                    raise ToolError("the setpoint is unknown or exceeds configured or observed device bounds")
                if temperatures and (previous is None or abs(value - previous) > limits.max_change):
                    raise ToolError(
                        "the temperature change exceeds max_change or the previous setpoint is unknown"
                    )
                values[key] = value
            if ranged and values["target_temp_low"] > values["target_temp_high"]:
                raise ToolError("the observed climate range is not ordered; no action was sent")
            wanted.update(values)
            wanted["temperature_unit"] = _UNITS[limits.unit]
        data = dict(temperatures)
        if mode is not None:
            data["hvac_mode"] = mode
            wanted["state"] = mode
        service = "set_temperature" if temperatures else "set_hvac_mode"
        return ActionPlan("climate", service, (entity_id,), data, {entity_id: wanted})

    def _fan_plan(self, args: dict[str, Any]) -> ActionPlan:
        schemas.checked_action(args, schemas.FAN, self.settings.binding)
        entity_id = args["entity_id"]
        self.target(entity_id, "fan", args.get("room"))
        limits = self.settings.fans[entity_id]
        percentage = args["percentage"]
        if not limits.minimum <= percentage <= limits.maximum:
            raise ToolError("the fan percentage exceeds its configured limits")
        raw = self.read_target(entity_id)
        attributes = require_available(raw)
        require_feature(attributes, 1, "fan percentage control")
        previous = number(attributes.get("percentage"))
        if previous is None or abs(percentage - previous) > limits.max_change:
            raise ToolError("the fan change exceeds max_change or its previous percentage is unknown")
        return ActionPlan(
            "fan",
            "set_percentage",
            (entity_id,),
            {"percentage": percentage},
            {entity_id: {"percentage": percentage, "state": "off" if percentage == 0 else "on"}},
        )

    def _media_plan(self, args: dict[str, Any]) -> ActionPlan:
        schemas.checked_action(args, schemas.MEDIA, self.settings.binding)
        entity_id, action = args["entity_id"], args["action"]
        self.target(entity_id, "media_player", args.get("room"))
        supplied = set(args) & {"volume_level", "is_volume_muted"}
        required = (
            {"volume_level"} if action == "volume" else ({"is_volume_muted"} if action == "mute" else set())
        )
        if supplied != required:
            raise ToolError("supply only the value required by the selected media action")
        raw = self.read_target(entity_id)
        attributes = require_available(raw)
        service, feature, wanted_base = _MEDIA_ACTIONS[action]
        require_feature(attributes, feature, f"media {action}")
        wanted = dict(wanted_base)
        data: dict[str, Any] = {}
        quiet = self.settings.quiet_hours.active(now())
        if action == "play" or (action == "mute" and not args["is_volume_muted"]):
            self.safe_volume(entity_id, attributes)
            if quiet:
                raise ToolError("quiet hours block playback and unmuting")
        if action == "volume":
            volume = args["volume_level"]
            limits = self.settings.media_players[entity_id]
            if not limits.minimum <= volume <= limits.maximum:
                raise ToolError("the requested volume exceeds configured safe bounds")
            current = number(attributes.get("volume_level"))
            if quiet and (current is None or volume > current):
                raise ToolError("quiet hours permit only a known volume reduction")
            data["volume_level"] = wanted["volume_level"] = volume
        elif action == "mute":
            data["is_volume_muted"] = wanted["is_volume_muted"] = args["is_volume_muted"]
        return ActionPlan("media_player", service, (entity_id,), data, {entity_id: wanted})


def matches(snapshot: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if snapshot.get("availability") != "available":
        return False
    attributes = snapshot["attributes"]
    for key, wanted in expected.items():
        actual = snapshot.get("state") if key == "state" else attributes.get(key)
        if key == "state" and isinstance(wanted, tuple):
            if actual not in wanted:
                return False
        elif key == "rgb_color":
            if not isinstance(actual, list) or len(actual) != 3:
                return False
            if any(number(channel) is None or not 0 <= channel <= 255 for channel in actual) or any(
                not _close(left, right, 1) for left, right in zip(actual, wanted, strict=True)
            ):
                return False
        elif number(wanted) is not None:
            tolerance = 1 if key == "brightness" else (0.01 if key == "volume_level" else 0.05)
            if not _close(actual, wanted, tolerance):
                return False
        elif not json_equal(actual, wanted):
            return False
    return True


def _close(left: Any, right: Any, tolerance: float) -> bool:
    first, second = number(left), number(right)
    return first is not None and second is not None and abs(first - second) <= tolerance

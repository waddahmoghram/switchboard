"""On-demand status and narrowly approved plug controls; no background writes or cached success."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from switchboard.context import AppContext
from switchboard.errors import ToolError
from switchboard.util import now_iso

from .client import ShellyClient, ShellyError
from .settings import Device, Settings

_ERRORS = frozenset({"overtemp", "overpower", "overvoltage", "undervoltage", "overcurrent"})
_TARGET = re.compile(r"[0-9a-f]{64}")


class ShellyTools:
    def __init__(self, ctx: AppContext, settings: Settings) -> None:
        self.ctx = ctx
        self.settings = settings

    def devices(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            raise ToolError("Shelly device listing takes no arguments")
        return {
            "configured_only": True,
            "live_validation": False,
            "devices": [
                {
                    "device": device.name,
                    "model": device.expected_model,
                    "generation": device.expected_generation,
                    "switch_ids": list(device.switch_ids),
                    "transport": device.base_url.partition(":")[0],
                    "authentication_configured": device.password is not None,
                    "controls": [
                        {
                            "switch_id": control.switch_id,
                            "target": device.target(control),
                            "safe_load": control.safe_load,
                            "max_power_watts": control.max_power_watts,
                        }
                        for control in device.controls
                    ],
                }
                for device in self.settings.devices
            ],
        }

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        device, switch_id = self._selection(args, control=False)
        client = ShellyClient(self.ctx, device, self.settings.timeout_seconds)
        client.identify()
        return self._snapshot(client, device, switch_id)

    def set_output(self, args: dict[str, Any]) -> dict[str, Any]:
        device, switch_id = self._selection(args, control=True)
        control = device.control(switch_id)
        target = args["target"]
        if not isinstance(target, str) or not _TARGET.fullmatch(target) or target != device.target(control):
            raise ToolError("Shelly target changed; list configured devices and request a new approval")
        on = args["on"]
        if not isinstance(on, bool):
            raise ToolError("Shelly output must be an explicit boolean")
        client = ShellyClient(self.ctx, device, self.settings.timeout_seconds)
        client.identify()
        before = self._snapshot(client, device, switch_id)
        power = before["power"]["value"]
        if before["errors"] or power is None or not 0 <= power <= control.max_power_watts:
            raise ToolError("Shelly control refused: power unavailable, unsafe power or component error")
        accepted: bool | None = None
        was_on: bool | None = None
        command_error: str | None = None
        try:
            result = client.set_output(switch_id, on)
            if not isinstance(result.get("was_on"), bool):
                raise ShellyError("protocol_error", uncertain=True)
            accepted = True
            was_on = result["was_on"]
        except ShellyError as exc:
            accepted = None if exc.uncertain else False
            command_error = exc.code
        observation: dict[str, Any] | None = None
        readback_error: str | None = None
        try:
            observation = self._snapshot(client, device, switch_id)
        except ShellyError as exc:
            readback_error = exc.code
        matches = observation["output"] == on if observation is not None else None
        confirmed = (
            accepted is True and matches is True and observation is not None and not observation["errors"]
        )
        return {
            "ok": confirmed,
            "status": "confirmed" if confirmed else ("uncertain" if accepted is None else "unconfirmed"),
            "device": device.name,
            "switch_id": switch_id,
            "requested_output": on,
            "command_accepted": accepted,
            "was_on": was_on,
            "command_error": command_error,
            "observation": observation,
            "readback_error": readback_error,
            "matches_requested": matches,
            "automatic_resend": False,
        }

    def summarize(self, args: Mapping[str, Any]) -> str:
        device = self.settings.device(str(args["device"]))
        try:
            control = device.control(args["switch_id"])
        except ToolError:
            return "Refuse a non-permitted Shelly plug target"
        state = "ON" if args["on"] is True else "OFF"
        return (
            f"Set Shelly {device.name} switch:{control.switch_id} {state}; nonessential lighting "
            f"<= {control.max_power_watts} W; target {str(args['target'])[:12]}; read back once"
        )

    def _selection(self, args: dict[str, Any], *, control: bool) -> tuple[Device, int]:
        expected = {"device", "switch_id", "target", "on"} if control else {"device", "switch_id"}
        if set(args) != expected or not isinstance(args["device"], str) or type(args["switch_id"]) is not int:
            raise ToolError("invalid fixed-device Shelly arguments")
        device = self.settings.device(args["device"])
        switch_id = args["switch_id"]
        if switch_id not in device.switch_ids:
            raise ToolError("this Shelly switch component is not exposed")
        return device, switch_id

    @staticmethod
    def _snapshot(client: ShellyClient, device: Device, switch_id: int) -> dict[str, Any]:
        raw = client.status(switch_id)
        result = minimal_status(raw, switch_id)
        return {
            "device": device.name,
            "switch_id": switch_id,
            "observed_at": now_iso(),
            "cached": False,
            "measurement_age_seconds": None,
            **result,
        }


def minimal_status(raw: dict[str, Any], switch_id: int) -> dict[str, Any]:
    """Keep only typed state, scalar measurements and safe error names; never raw provider strings."""
    if type(raw.get("id")) is not int or raw["id"] != switch_id or not isinstance(raw.get("output"), bool):
        raise ShellyError("protocol_error", uncertain=True)
    errors = raw.get("errors", [])
    if (
        not isinstance(errors, list)
        or len(errors) > 16
        or any(not isinstance(error, str) or not 1 <= len(error) <= 64 for error in errors)
    ):
        raise ShellyError("protocol_error", uncertain=True)
    safe_errors = sorted({error if error in _ERRORS else "unrecognized_error" for error in errors})
    return {
        "output": raw["output"],
        "power": _measurement(raw.get("apower"), "W", -1_000_000, 1_000_000),
        "energy": _energy(raw, "aenergy"),
        "returned_energy": _energy(raw, "ret_aenergy"),
        "voltage": _measurement(raw.get("voltage"), "V", 0, 1000),
        "current": _measurement(raw.get("current"), "A", -10_000, 10_000),
        "errors": safe_errors,
    }


def _energy(raw: dict[str, Any], key: str) -> dict[str, Any]:
    energy = raw.get(key)
    if energy is not None and not isinstance(energy, dict):
        raise ShellyError("protocol_error", uncertain=True)
    total = energy.get("total") if energy is not None else None
    return {**_measurement(total, "Wh", 0, 1_000_000_000_000), "kind": "device_cumulative_counter"}


def _measurement(value: Any, unit: str, minimum: float, maximum: float) -> dict[str, Any]:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= maximum
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise ShellyError("protocol_error", uncertain=True)
    return {"available": value is not None, "value": value, "unit": unit}

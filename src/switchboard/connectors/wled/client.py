"""Bounded WLED reads and typed static-state writes through the core transport."""

from __future__ import annotations

from typing import Any

from switchboard.connectors.wled.settings import Device, Preset, Settings, target_digest
from switchboard.context import AppContext
from switchboard.errors import HttpError, ToolError
from switchboard.httpclient import HttpResponse
from switchboard.jsonschema import validate
from switchboard.util import canonical_json, json_equal, now_iso, printable, sha256_hex

_BYTE: dict[str, Any] = {"type": "integer", "minimum": 0, "maximum": 255}
COLOR_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 3,
    "maxItems": 4,
    "items": _BYTE,
}
_SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "minimum": 0, "maximum": 63},
        "on": {"type": "boolean"},
        "bri": {"type": "integer", "minimum": 1, "maximum": 255},
        "col": {"type": "array", "minItems": 1, "maxItems": 3, "items": COLOR_SCHEMA},
        "fx": {"type": "integer", "enum": [0]},
    },
    "required": ["id"],
    "minProperties": 2,
    "additionalProperties": False,
}
_PRESET_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "n": {"type": "string", "maxLength": 128},
        "ql": {"type": "string", "maxLength": 16},
        "on": {"type": "boolean"},
        "bri": {"type": "integer", "minimum": 1, "maximum": 255},
        "transition": {"type": "integer", "minimum": 0, "maximum": 100},
        "seg": {"type": "array", "minItems": 1, "maxItems": 16, "items": _SEGMENT_SCHEMA},
    },
    "additionalProperties": False,
}


class WledClient:
    """A client bound to a configured device; tool arguments never supply addresses."""

    def __init__(self, ctx: AppContext, settings: Settings, device: Device) -> None:
        self.ctx = ctx
        self.settings = settings
        self.device = device

    def read(self) -> dict[str, Any]:
        """Read only state and capabilities, not configuration, effects, or network details."""
        response = self._request("GET", "/json/si")
        data = _json(response)
        _check_provider_error(data)
        if not isinstance(data, dict) or not isinstance(data.get("state"), dict):
            raise ToolError("WLED returned an invalid state snapshot")
        state = data["state"]
        info = data.get("info")
        if (
            not isinstance(info, dict)
            or not isinstance(info.get("leds"), dict)
            or not isinstance(state.get("on"), bool)
            or _integer(state.get("bri"), 1, 255) is None
        ):
            raise ToolError("WLED returned an invalid state or capability snapshot")
        raw_segments = state.get("seg")
        if not isinstance(raw_segments, list) or len(raw_segments) > 64:
            raise ToolError("WLED returned an invalid or oversized segment list")
        leds = info["leds"]
        seglc = leds.get("seglc")
        if not isinstance(seglc, list) or len(seglc) > 64:
            seglc = []
        segments: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in raw_segments:
            if not isinstance(raw, dict):
                raise ToolError("WLED returned an invalid segment")
            segment_id = _integer(raw.get("id"), 0, 63)
            if segment_id is None or segment_id in seen:
                raise ToolError("WLED returned missing, invalid, or duplicate segment identifiers")
            seen.add(segment_id)
            start = _integer(raw.get("start"), 0, 65535)
            stop = _integer(raw.get("stop"), 0, 65535)
            colors = raw.get("col")
            if validate(colors, _SEGMENT_SCHEMA["properties"]["col"]):
                colors = None
            segments.append(
                {
                    "id": segment_id,
                    "active": start is not None and stop is not None and stop > start,
                    "on": raw.get("on") if isinstance(raw.get("on"), bool) else None,
                    "bri": _integer(raw.get("bri"), 0, 255),
                    "col": colors,
                    "fx": _integer(raw.get("fx"), 0, 65535),
                    "lc": _integer(seglc[segment_id], 0, 255) if segment_id < len(seglc) else None,
                }
            )
        nightlight = state.get("nl")
        nightlight_on = nightlight.get("on") if isinstance(nightlight, dict) else None
        return {
            "target_digest": target_digest(self.settings, self.device),
            "observed_at": now_iso(),
            "state": {
                "on": state["on"],
                "bri": state["bri"],
                "transition": _integer(state.get("transition"), 0, 65535),
                "ps": _integer(state.get("ps"), -1, 250),
                "pl": _integer(state.get("pl"), -1, 250),
                "nl": nightlight_on if isinstance(nightlight_on, bool) else None,
                "seg": segments,
            },
            "info": {
                "ver": printable(info["ver"][:64]) if isinstance(info.get("ver"), str) else None,
                "live": info.get("live") if isinstance(info.get("live"), bool) else None,
                "leds": {
                    "count": _integer(leds.get("count"), 1, 100000),
                    "maxseg": _integer(leds.get("maxseg"), 1, 255),
                },
            },
        }

    def preset(self, preset: Preset) -> dict[str, Any]:
        """Copy a pinned static preset; never ask firmware to execute a preset identifier."""
        data = _json(self._request("GET", "/presets.json"))
        if not isinstance(data, dict):
            raise ToolError("WLED returned an invalid presets file")
        raw = data.get(str(preset.id))
        if not isinstance(raw, dict):
            raise ToolError("WLED preset is missing")
        if validate(raw, _PRESET_STATE_SCHEMA):
            raise ToolError("WLED preset contains unsupported fields or non-static commands")
        if sha256_hex(canonical_json(raw)) != preset.sha256:
            raise ToolError("WLED preset differs from its reviewed fingerprint")
        patch = {key: value for key, value in raw.items() if key not in {"n", "ql"}}
        if not patch:
            raise ToolError("WLED preset contains no supported state changes")
        return patch

    def write(self, patch: dict[str, Any]) -> bool | None:
        """Send one state patch without broadcasts; acknowledgement alone is not confirmation."""
        payload = {**patch, "udpn": {"nn": True}}
        response = self._request("POST", "/json/state", canonical_json(payload).encode("ascii"))
        try:
            data = _json(response)
        except ToolError:
            return None
        _check_provider_error(data)
        return True if isinstance(data, dict) and data.get("success") is True else None

    def _request(self, method: str, path: str, body: bytes | None = None) -> HttpResponse:
        url = self.device.base_url + path
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.ctx.http.request(
                method,
                url,
                headers=headers,
                body=body,
                timeout=self.settings.timeout_seconds,
                max_bytes=self.settings.max_response_bytes,
                allow_http=self.device.allow_http,
                follow_redirects=False,
            )
        except HttpError as exc:
            raise ToolError("WLED transport failed; no automatic retry was made") from exc
        if 300 <= response.status < 400 or response.url != url:
            raise ToolError("WLED redirect refused; update the configured endpoint manually")
        if not response.ok:
            raise ToolError(f"WLED returned HTTP {response.status}; response body withheld")
        if len(response.body) > self.settings.max_response_bytes:
            raise ToolError("WLED response exceeds the configured byte limit")
        return response


def public_snapshot(device: Device, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project only allowed segment identifiers and a small set of non-sensitive device fields."""
    state = snapshot["state"]
    segments: list[dict[str, Any]] = []
    for segment in state["seg"]:
        if segment["id"] not in device.segments:
            continue
        capability = segment["lc"]
        segments.append(
            {
                "id": segment["id"],
                "active": segment["active"],
                "on": segment["on"],
                "brightness": segment["bri"],
                "colors": segment["col"],
                "effect": segment["fx"],
                "capabilities": {
                    "reported": capability is not None,
                    "rgb": bool(capability & 1) if capability is not None else None,
                    "white": bool(capability & 2) if capability is not None else None,
                    "color_temperature": bool(capability & 4) if capability is not None else None,
                },
            }
        )
    return {
        "device": device.name,
        "target_digest": snapshot["target_digest"],
        "status": "observed",
        "stale": False,
        "observed_at": snapshot["observed_at"],
        "firmware": snapshot["info"]["ver"],
        "realtime_active": snapshot["info"]["live"],
        "led_count": snapshot["info"]["leds"]["count"],
        "max_segments": snapshot["info"]["leds"]["maxseg"],
        "on": state["on"],
        "brightness": state["bri"],
        "transition_deciseconds": state["transition"],
        "preset_id": state["ps"],
        "playlist_id": state["pl"],
        "nightlight_active": state["nl"],
        "segments": segments,
        "missing_segments": sorted(set(device.segments) - {segment["id"] for segment in segments}),
    }


def validate_patch(device: Device, snapshot: dict[str, Any], patch: dict[str, Any]) -> None:
    """Refuse unsupported, inactive, or out-of-scope targets before a write."""
    if not controls_available(snapshot):
        raise ToolError("WLED control requires reported inactive realtime, playlist, and nightlight modes")
    state = snapshot["state"]
    if any(key in patch for key in ("on", "bri", "transition")):
        if not device.allow_global:
            raise ToolError("WLED global control is not enabled for this device")
        if "transition" in patch and state["transition"] is None:
            raise ToolError("WLED does not report a transition setting")
    segments = {segment["id"]: segment for segment in state["seg"]}
    max_segments = snapshot["info"]["leds"]["maxseg"]
    seen: set[int] = set()
    for requested in patch.get("seg", []):
        segment_id = requested["id"]
        if segment_id not in device.segments or segment_id in seen:
            raise ToolError("WLED preset or action targets an unconfigured or duplicate segment")
        seen.add(segment_id)
        actual = segments.get(segment_id)
        if actual is None or not actual["active"] or max_segments is None or segment_id >= max_segments:
            raise ToolError("WLED target segment is not active or its bounds are unverified")
        for key in ("on", "bri", "fx"):
            if key in requested and actual[key] is None:
                raise ToolError("WLED does not report the requested segment control")
        if "col" in requested:
            capability = actual["lc"]
            if actual["col"] is None or capability is None or not capability & 1:
                raise ToolError("WLED target segment does not report support for color")
            if any(len(color) == 4 for color in requested["col"]) and not capability & 2:
                raise ToolError("WLED target segment does not report a white channel")
            if requested.get("fx") != 0:
                raise ToolError("WLED static color presets must explicitly set the Solid effect (fx = 0)")


def controls_available(snapshot: dict[str, Any]) -> bool:
    """Fail closed when an overriding mode is active or its state cannot be verified."""
    return (
        snapshot["info"]["live"] is False
        and snapshot["state"]["pl"] == -1
        and snapshot["state"]["nl"] is False
    )


def matches(snapshot: dict[str, Any], patch: dict[str, Any]) -> bool:
    """Compare only requested values, not acknowledgements or an inferred physical light output."""
    if not controls_available(snapshot):
        return False
    state = snapshot["state"]
    for key in ("on", "bri", "transition"):
        if key in patch and not json_equal(state[key], patch[key]):
            return False
    segments = {segment["id"]: segment for segment in state["seg"]}
    for requested in patch.get("seg", []):
        actual = segments.get(requested["id"])
        if actual is None or not actual["active"]:
            return False
        for key in ("on", "bri", "fx"):
            if key in requested and not json_equal(actual[key], requested[key]):
                return False
        if "col" in requested:
            if actual["col"] is None or len(actual["col"]) < len(requested["col"]):
                return False
            for expected, observed in zip(requested["col"], actual["col"], strict=False):
                if expected + [0] * (4 - len(expected)) != observed + [0] * (4 - len(observed)):
                    return False
    return True


def _integer(value: object, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        return None
    return value


def _json(response: HttpResponse) -> Any:
    try:
        return response.json()
    except (ValueError, RecursionError) as exc:
        raise ToolError("WLED returned invalid or excessively nested JSON") from exc


def _check_provider_error(data: Any) -> None:
    if not isinstance(data, dict):
        return
    error = data.get("error")
    if any(json_equal(error, empty) for empty in (None, False, 0, "")) and data.get("success") is not False:
        return
    raise ToolError("WLED reported a provider error; response details withheld")

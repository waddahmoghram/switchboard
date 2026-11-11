"""Offline tool registration and the home connector's named internal service."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from switchboard.connectors.home import schemas
from switchboard.connectors.home.client import HomeAssistant
from switchboard.connectors.home.controls import HomeControls
from switchboard.connectors.home.poll import HomePoller
from switchboard.connectors.home.settings import HomeSettings
from switchboard.connectors.home.speech import HomeSpeech, require_speech_policy
from switchboard.connectors.home.state import observation
from switchboard.connectors.home.store import HomeStore
from switchboard.context import AppContext
from switchboard.errors import ToolError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry
from switchboard.util import canonical_json, now, truncate

_KINDS = ("temperature", "humidity", "motion", "leak", "battery", "power", "energy")
_UNITS = {
    "temperature": frozenset({"\u00b0C", "\u00b0F", "K"}),
    "humidity": frozenset({"%"}),
    "battery": frozenset({"%"}),
    "power": frozenset({"mW", "W", "kW", "MW", "GW", "TW"}),
    "energy": frozenset(
        {"J", "kJ", "MJ", "GJ", "mWh", "Wh", "kWh", "MWh", "GWh", "TWh", "cal", "kcal", "Mcal", "Gcal", "thm"}
    ),
}
_BINARY = {
    "motion": ("motion", "motion_detected"),
    "moisture": ("leak", "moisture_detected"),
    "battery": ("battery", "low_battery"),
}


class HomeService:
    def __init__(self, ctx: AppContext, settings: HomeSettings, client: HomeAssistant) -> None:
        self.ctx = ctx
        self.settings = settings
        self.client = client
        self.store = HomeStore(ctx, settings)
        self.poller = HomePoller(client, settings, self.store)
        self.controls = HomeControls(client, settings)
        self.speech = HomeSpeech(client, settings, self.controls)

    def targets(self, args: dict[str, Any]) -> dict[str, Any]:
        schemas.checked(args, schemas.EMPTY)
        return {
            "binding": self.settings.binding,
            "rooms": {name: list(entities) for name, entities in self.settings.rooms.items()},
            "lights": list(self.settings.lights),
            "climate": {entity_id: asdict(limits) for entity_id, limits in self.settings.climate.items()},
            "fans": {entity_id: asdict(limits) for entity_id, limits in self.settings.fans.items()},
            "media_players": {
                entity_id: asdict(limits) for entity_id, limits in self.settings.media_players.items()
            },
            "lighting_presets": {
                name: {
                    "fingerprint": preset.fingerprint,
                    "actions": [
                        {"entity_id": action.entity_id, **action.arguments()} for action in preset.actions
                    ],
                }
                for name, preset in self.settings.lighting_presets.items()
            },
            "quiet_hours": asdict(self.settings.quiet_hours),
            "speech": asdict(self.settings.speech),
            "room_targeting": (
                "supply explicit entity_id/entity_ids; a room only restricts them, never expands them"
            ),
        }

    def poll(self, args: dict[str, Any]) -> dict[str, Any]:
        schemas.checked(args, schemas.EMPTY)
        return self.poller.poll()

    def poll_job(self) -> None:
        self.poller.poll()

    def poll_status(self, args: dict[str, Any]) -> dict[str, Any]:
        schemas.checked(args, schemas.EMPTY)
        return self.poller.status()

    def sensor_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        schemas.checked(args, schemas.SUMMARY)
        room = args.get("room")
        members = self.settings.rooms.get(room) if room is not None else None
        if room is not None and members is None:
            raise ToolError("that room alias is not explicitly configured")
        entity_id = args.get("entity_id")
        if entity_id is not None and (
            not self.settings.visible(entity_id)
            or entity_id.partition(".")[0] not in {"sensor", "binary_sensor"}
            or (members is not None and entity_id not in members)
        ):
            raise ToolError("the requested sensor is not exposed in that scope")
        source = args.get("source", "live")
        source_error: str | None = None
        if source == "cached":
            if not self.settings.poll.enabled:
                raise ToolError("cached summaries require opt-in polling")
            if entity_id is not None and entity_id not in self.settings.poll.entities:
                raise ToolError("the requested sensor is not in the polling allowlist")
            health = self.poller.status()
            source_error = health["error"]
            snapshots = health["entities"]
        else:
            observed_at = now().isoformat(timespec="microseconds")
            raw_states = (
                [self.client.state(entity_id)] if entity_id is not None else self.client.states(strict=True)
            )
            seen: set[str] = set()
            snapshots = []
            for raw in raw_states:
                identifier = raw["entity_id"]
                if self.settings.visible(identifier) and identifier.partition(".")[0] in {
                    "sensor",
                    "binary_sensor",
                }:
                    if identifier in seen:
                        raise ToolError("Home Assistant returned a duplicate sensor")
                    seen.add(identifier)
                    snapshots.append({**observation(identifier, raw, observed_at), "stale": False})
        selected: list[dict[str, Any]] = []
        unclassified: list[dict[str, Any]] = []
        for snapshot in sorted(snapshots, key=lambda item: item["entity_id"]):
            identifier = snapshot["entity_id"]
            if identifier.partition(".")[0] not in {"sensor", "binary_sensor"}:
                continue
            if (entity_id is not None and identifier != entity_id) or (
                members is not None and identifier not in members
            ):
                continue
            reading = _reading(snapshot, source_error)
            if reading is None:
                unclassified.append(
                    {
                        "entity_id": identifier,
                        "availability": snapshot["availability"],
                        "stale": snapshot["stale"],
                        "reason": "no supported device_class was reported; no capability was inferred",
                    }
                )
            elif args.get("kind") in (None, reading["kind"]):
                selected.append(reading)
        limit = args.get("limit", 100)
        readings: dict[str, list[dict[str, Any]]] = {kind: [] for kind in _KINDS}
        for reading in selected[:limit]:
            readings[reading["kind"]].append(reading)
        remaining = max(0, limit - min(len(selected), limit))
        return {
            "source": source,
            "readings": readings,
            "unclassified": unclassified[:remaining],
            "truncated": len(selected) + len(unclassified) > limit,
            "source_error": source_error,
            "freshness_basis": "time of local state reads, not proof of device freshness",
            "energy_scope": (
                "reported instantaneous power and meter readings; no totals, costs, or conversions"
            ),
        }


def register_extension(
    ctx: AppContext, registry: ToolRegistry, settings: HomeSettings, client: HomeAssistant
) -> HomeService:
    require_speech_policy(ctx, settings)
    service = HomeService(ctx, settings, client)
    ctx.provide("home", service)
    definitions = (
        (
            "home_targets",
            "List approved leaf devices, room aliases, limits and pinned lighting presets; offline.",
            Tier.READ,
            schemas.EMPTY,
            service.targets,
        ),
        (
            "home_sensor_summary",
            (
                "Read exposed temperature/humidity, motion/leak, battery and available power/energy "
                "with original units; no inferred devices or totals."
            ),
            Tier.READ,
            schemas.SUMMARY,
            service.sensor_summary,
        ),
        (
            "home_poll_status",
            (
                "Read local polling health and snapshots, including unavailable, missing, stale and "
                "error indicators; no network."
            ),
            Tier.READ,
            schemas.EMPTY,
            service.poll_status,
        ),
        (
            "home_poll",
            (
                "Sample configured exposed entities and transactionally enqueue observed changes; "
                "first baselines are silent, history is incomplete."
            ),
            Tier.READ,
            schemas.EMPTY,
            service.poll,
        ),
        (
            "home_lights",
            (
                "Set explicitly approved lights on/off, brightness, color or a bounded fade; "
                "room aliases never add targets. Fades remain unconfirmed."
            ),
            Tier.ACT,
            schemas.LIGHTS,
            service.controls.lights,
        ),
        (
            "home_climate",
            (
                "Adjust one approved thermostat within its per-device unit, bounds, modes and change "
                "limit; observe the setpoint, not room comfort."
            ),
            Tier.ACT,
            schemas.CLIMATE,
            service.controls.climate,
        ),
        (
            "home_fan",
            "Set one approved fan's percentage within its limits; no inferred speed or universal conversion.",
            Tier.ACT,
            schemas.FAN,
            service.controls.fan,
        ),
        (
            "home_media",
            (
                "Play/pause/stop or set bounded volume/mute on one approved player; no media addresses, "
                "grouping or automatic unmute."
            ),
            Tier.ACT,
            schemas.MEDIA,
            service.controls.media,
        ),
        (
            "home_lighting_preset",
            (
                "Apply only a configured lighting-only preset pinned by its fingerprint; "
                "no Home Assistant scripts or scenes, no atomic multi-device promise."
            ),
            Tier.ACT,
            schemas.PRESET,
            service.controls.preset,
        ),
    )
    for name, description, tier, schema, handler in definitions:
        registry.add(
            Tool(
                name=name,
                description=description,
                tier=tier,
                input_schema=schemas.bound(schema, settings.binding) if tier is Tier.ACT else schema,
                handler=handler,
                summarize=_summarize if tier is Tier.ACT else None,
                connector="home",
            )
        )
    if settings.speech.enabled:
        registry.add(
            Tool(
                name="home_announce",
                description=(
                    "Speak approved short plain text using the exact configured engine and speaker at "
                    "an already observed safe volume; quiet/privacy gates apply. Playback cannot be verified."
                ),
                tier=Tier.ACT,
                input_schema=schemas.bound(schemas.ANNOUNCE, settings.binding),
                handler=service.speech.announce,
                summarize=service.speech.summarize,
                recipients=lambda args: [str(args["entity_id"])],
                connector="home",
            )
        )
    if settings.poll.enabled:
        ctx.scheduler.add("home.poll", settings.poll.interval_seconds, service.poll_job)
    return service


def _summarize(args: Mapping[str, Any]) -> str:
    return f"Home reviewed action {truncate(canonical_json(args), 465)}"


def _reading(snapshot: Mapping[str, Any], source_error: str | None) -> dict[str, Any] | None:
    attributes = snapshot.get("attributes", {})
    device_class = attributes.get("device_class")
    domain = snapshot["entity_id"].partition(".")[0]
    meaning: str | None = None
    unit = attributes.get("unit_of_measurement")
    value: float | bool | None = None
    reason: str | None = None
    if domain == "binary_sensor":
        if device_class not in _BINARY:
            return None
        kind, meaning = _BINARY[device_class]
        unit = None
        if snapshot.get("state") in {"on", "off"}:
            value = snapshot["state"] == "on"
        else:
            reason = "no known binary state"
    else:
        if device_class not in _UNITS:
            return None
        kind = device_class
        try:
            numeric = float(snapshot.get("state", ""))
            if math.isfinite(numeric):
                value = numeric
        except (ValueError, TypeError, OverflowError):
            pass
        if value is None:
            reason = "no finite numeric reading"
        elif unit not in _UNITS[kind]:
            reason = "unit missing or unrecognized; no conversion attempted"
        elif kind in {"battery", "humidity"} and not 0 <= value <= 100:
            reason = "reported percentage is outside 0 to 100"
    available = snapshot["availability"] == "available"
    is_stale = snapshot.get("stale", True)
    usable = available and not is_stale and source_error is None and reason is None
    return {
        "entity_id": snapshot["entity_id"],
        "kind": kind,
        "device_class": device_class,
        "state_class": attributes.get("state_class"),
        "state": snapshot.get("state"),
        "value": value if available else None,
        "unit": unit,
        "meaning": meaning,
        "availability": snapshot["availability"],
        "stale": is_stale,
        "usable": usable,
        "reason": reason,
        "observed_at": snapshot.get("observed_at"),
        "last_updated": snapshot.get("last_updated"),
    }

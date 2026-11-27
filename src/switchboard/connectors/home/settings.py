"""Offline configuration validation for the legacy and opt-in home capabilities."""

from __future__ import annotations

import math
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from switchboard.errors import ConfigError
from switchboard.secrets import is_reference
from switchboard.util import canonical_json, glob_any, sha256_hex

DEFAULT_DENY_DOMAINS = ("lock", "alarm_control_panel", "cover", "camera")
DOMAIN = re.compile(r"[a-z0-9_]{1,64}")
ENTITY_ID = re.compile(r"[a-z0-9_]{1,64}\.[a-z0-9_]{1,190}")
_LABEL = "[connectors.home]"
_READ_DOMAINS = frozenset({"sensor", "binary_sensor", "light", "climate", "fan", "media_player"})
_MODES = frozenset({"off", "heat", "cool", "heat_cool", "fan_only"})
_LIGHT_KEYS = frozenset(
    {"entity_id", "state", "brightness_pct", "rgb_color", "color_temp_kelvin", "transition"}
)
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


@dataclass(frozen=True)
class PollSettings:
    enabled: bool = False
    entities: tuple[str, ...] = ()
    interval_seconds: int = 60
    stale_after_seconds: int = 300


@dataclass(frozen=True)
class ClimateLimits:
    unit: str
    minimum: float
    maximum: float
    max_change: float
    modes: tuple[str, ...]


@dataclass(frozen=True)
class FanLimits:
    minimum: int = 0
    maximum: int = 100
    max_change: int = 100


@dataclass(frozen=True)
class MediaLimits:
    minimum: float
    maximum: float


@dataclass(frozen=True)
class QuietHours:
    enabled: bool = False
    timezone: str = "UTC"
    start: str = "22:00"
    end: str = "07:00"

    def active(self, moment: datetime) -> bool:
        if not self.enabled:
            return False
        local = moment.astimezone(ZoneInfo(self.timezone)).strftime("%H:%M")
        if self.start < self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


@dataclass(frozen=True)
class SpeechSettings:
    enabled: bool = False
    engine: str = ""
    destinations: tuple[str, ...] = ()
    external_processing: bool = False
    allow_external: bool = False
    allow_sensitive: bool = False


@dataclass(frozen=True)
class LightingAction:
    entity_id: str
    state: str
    brightness_pct: int | None = None
    rgb_color: tuple[int, ...] | None = None
    color_temp_kelvin: int | None = None
    transition: float = 0

    def arguments(self) -> dict[str, Any]:
        values: dict[str, Any] = {"state": self.state, "transition": self.transition}
        for key in ("brightness_pct", "color_temp_kelvin"):
            value = getattr(self, key)
            if value is not None:
                values[key] = value
        if self.rgb_color is not None:
            values["rgb_color"] = list(self.rgb_color)
        return values


@dataclass(frozen=True)
class LightingPreset:
    actions: tuple[LightingAction, ...]

    @property
    def fingerprint(self) -> str:
        return sha256_hex(
            canonical_json([{"entity_id": action.entity_id, **action.arguments()} for action in self.actions])
        )


@dataclass(frozen=True)
class HomeSettings:
    base_url: str
    token: str  # secret reference to a long-lived access token
    expose: tuple[str, ...] = ()
    deny_domains: frozenset[str] = frozenset(DEFAULT_DENY_DOMAINS)
    allow_http: bool = False
    lights: tuple[str, ...] = ()
    rooms: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    climate: Mapping[str, ClimateLimits] = field(default_factory=dict)
    fans: Mapping[str, FanLimits] = field(default_factory=dict)
    media_players: Mapping[str, MediaLimits] = field(default_factory=dict)
    lighting_presets: Mapping[str, LightingPreset] = field(default_factory=dict)
    poll: PollSettings = field(default_factory=PollSettings)
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    speech: SpeechSettings = field(default_factory=SpeechSettings)

    def visible(self, entity_id: str) -> bool:
        """Exposed by a glob and not in a denied domain."""
        domain = entity_id.partition(".")[0].lower()
        return bool(
            ENTITY_ID.fullmatch(entity_id)
            and domain not in self.deny_domains
            and glob_any(entity_id, self.expose)
        )

    @property
    def source(self) -> str:
        """Separate observations from different configured servers without storing their address."""
        return sha256_hex(self.base_url)

    @property
    def binding(self) -> str:
        """Pin the endpoint and reviewed action configuration, never a resolved secret or reference."""
        return sha256_hex(
            canonical_json(
                {
                    "version": 1,
                    "component": "home",
                    "endpoint": self.base_url,
                    "allow_http": self.allow_http,
                    "expose": self.expose,
                    "deny_domains": sorted(self.deny_domains),
                    "lights": self.lights,
                    "rooms": self.rooms,
                    "climate": {entity_id: asdict(limits) for entity_id, limits in self.climate.items()},
                    "fans": {entity_id: asdict(limits) for entity_id, limits in self.fans.items()},
                    "media_players": {
                        entity_id: asdict(limits) for entity_id, limits in self.media_players.items()
                    },
                    "lighting_presets": {
                        name: [
                            {"entity_id": action.entity_id, **action.arguments()} for action in preset.actions
                        ]
                        for name, preset in self.lighting_presets.items()
                    },
                    "quiet_hours": asdict(self.quiet_hours),
                    "speech": asdict(self.speech),
                }
            )
        )


def load_settings(table: Mapping[str, Any]) -> HomeSettings:
    """Parse and check the table without resolving the token or contacting Home Assistant."""
    _known(
        table,
        {
            "base_url",
            "token",
            "expose",
            "deny_domains",
            "allow_http",
            "lights",
            "rooms",
            "climate",
            "fans",
            "media_players",
            "lighting_presets",
            "poll",
            "quiet_hours",
            "speech",
        },
        _LABEL,
    )
    allow_http = table.get("allow_http", False)
    if not isinstance(allow_http, bool):
        raise ConfigError(f"{_LABEL} allow_http must be true or false")
    token = table.get("token")
    if not is_reference(token):
        raise ConfigError(
            f"{_LABEL} token must be a secret reference such as env:NAME, file:/path, or cmd:program"
        )
    settings = HomeSettings(
        base_url=_base_url(table.get("base_url"), allow_http),
        token=str(token),
        expose=_expose(table.get("expose", [])),
        deny_domains=_deny_domains(table.get("deny_domains", list(DEFAULT_DENY_DOMAINS))),
        allow_http=allow_http,
    )
    lights = _entities(table.get("lights", []), "lights", settings, {"light"})
    quiet = _quiet_hours(_table(table.get("quiet_hours", {}), "quiet_hours"))
    climate = _climate(_table(table.get("climate", {}), "climate"), settings)
    fans = _fans(_table(table.get("fans", {}), "fans"), settings)
    media = _media(_table(table.get("media_players", {}), "media_players"), settings)
    rooms: dict[str, tuple[str, ...]] = {}
    for name, members in _named(_table(table.get("rooms", {}), "rooms"), "rooms"):
        rooms[name] = _entities(members, f"rooms.{name}", settings, _READ_DOMAINS, nonempty=True)
    settings = replace(
        settings,
        lights=lights,
        quiet_hours=quiet,
        climate=climate,
        fans=fans,
        media_players=media,
        rooms=rooms,
    )
    return replace(
        settings,
        poll=_poll(_table(table.get("poll", {}), "poll"), settings),
        speech=_speech(_table(table.get("speech", {}), "speech"), settings),
        lighting_presets=_presets(_table(table.get("lighting_presets", {}), "lighting_presets"), settings),
    )


def _base_url(value: Any, allow_http: bool) -> str:
    message = f"{_LABEL} base_url must be an https:// address without credentials, query, or fragment"
    if not isinstance(value, str):
        raise ConfigError(message)
    try:
        parts = urllib.parse.urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise ConfigError(message) from exc
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ConfigError(message)
    if parts.query or parts.fragment or "@" in parts.netloc or any(ord(char) <= 32 for char in value):
        raise ConfigError(message)
    if parts.scheme == "http" and not allow_http:
        raise ConfigError(
            f"{_LABEL} base_url uses http, which sends the token in clear text; use https, or set "
            "allow_http = true only for a server on your own network"
        )
    return value.rstrip("/")


def _expose(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 100
        or not all(isinstance(item, str) and 0 < len(item) <= 255 for item in value)
    ):
        raise ConfigError(f'{_LABEL} expose must list entity globs such as "light.*" or "sensor.outdoor_*"')
    return tuple(value)


def _deny_domains(value: Any) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or len(value) > 100
        or not all(isinstance(item, str) and DOMAIN.fullmatch(item) for item in value)
    ):
        raise ConfigError(f'{_LABEL} deny_domains must list domains such as "lock" or "cover"')
    return frozenset(value)


def _known(table: Mapping[str, Any], allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"{label}: unknown setting(s): {', '.join(sorted(unknown))}")


def _table(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{_LABEL} {label} must be a table")
    if len(value) > 100:
        raise ConfigError(f"{_LABEL} {label} has more than 100 entries")
    return value


def _named(table: Mapping[str, Any], label: str) -> list[tuple[str, Any]]:
    for name in table:
        if not DOMAIN.fullmatch(name):
            raise ConfigError(f"{_LABEL} {label} names must be lowercase words of at most 64 characters")
    return list(table.items())


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        raise ConfigError(f"{_LABEL} {label} must be a finite number from {minimum} to {maximum}")
    return float(value)


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{_LABEL} {label} must be a whole number from {minimum} to {maximum}")
    return value


def _boolean(table: Mapping[str, Any], key: str, label: str, default: bool = False) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{_LABEL} {label}.{key} must be true or false")
    return value


def _entity(value: Any, label: str, settings: HomeSettings, domains: set[str] | frozenset[str]) -> str:
    if not isinstance(value, str) or not ENTITY_ID.fullmatch(value) or value.partition(".")[0] not in domains:
        raise ConfigError(f"{_LABEL} {label} must name an exact entity in {', '.join(sorted(domains))}")
    if not settings.visible(value):
        raise ConfigError(f"{_LABEL} {label}: {value} must be exposed and not denied")
    return value


def _entities(
    value: Any,
    label: str,
    settings: HomeSettings,
    domains: set[str] | frozenset[str],
    *,
    nonempty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 200 or (nonempty and not value):
        raise ConfigError(
            f"{_LABEL} {label} must list {'1 to ' if nonempty else 'at most '}200 exact entities"
        )
    result = tuple(_entity(item, label, settings, domains) for item in value)
    if len(set(result)) != len(result):
        raise ConfigError(f"{_LABEL} {label} cannot contain duplicate entities")
    return result


def _poll(table: Mapping[str, Any], settings: HomeSettings) -> PollSettings:
    _known(table, {"enabled", "entities", "interval_seconds", "stale_after_seconds"}, f"{_LABEL} poll")
    enabled = _boolean(table, "enabled", "poll")
    interval = _integer(table.get("interval_seconds", 60), "poll.interval_seconds", 30, 3600)
    stale = _integer(
        table.get("stale_after_seconds", max(300, interval * 3)), "poll.stale_after_seconds", 30, 86400
    )
    if stale < interval:
        raise ConfigError(f"{_LABEL} poll.stale_after_seconds must be at least interval_seconds")
    return PollSettings(
        enabled=enabled,
        entities=_entities(
            table.get("entities", []), "poll.entities", settings, _READ_DOMAINS, nonempty=enabled
        ),
        interval_seconds=interval,
        stale_after_seconds=stale,
    )


def _climate(table: Mapping[str, Any], settings: HomeSettings) -> dict[str, ClimateLimits]:
    result: dict[str, ClimateLimits] = {}
    for entity_id, raw in table.items():
        _entity(entity_id, "climate", settings, {"climate"})
        values = _table(raw, f"climate.{entity_id}")
        _known(values, {"unit", "minimum", "maximum", "max_change", "modes"}, f"{_LABEL} climate.{entity_id}")
        unit = values.get("unit")
        if not isinstance(unit, str) or unit not in {"C", "F"}:
            raise ConfigError(f"{_LABEL} climate.{entity_id}.unit must be C or F; no automatic conversion")
        floor, ceiling = (-50, 100) if unit == "C" else (-58, 212)
        minimum = _number(values.get("minimum"), "climate.minimum", floor, ceiling)
        maximum = _number(values.get("maximum"), "climate.maximum", minimum, ceiling)
        change = _number(values.get("max_change", 2), "climate.max_change", 0.1, 20)
        modes = values.get("modes")
        if (
            not isinstance(modes, list)
            or not modes
            or len(modes) > len(_MODES)
            or not all(isinstance(mode, str) and mode in _MODES for mode in modes)
            or len(set(modes)) != len(modes)
        ):
            raise ConfigError(
                f"{_LABEL} climate.{entity_id}.modes must explicitly list reviewed climate modes"
            )
        result[entity_id] = ClimateLimits(str(unit), minimum, maximum, change, tuple(modes))
    return result


def _fans(table: Mapping[str, Any], settings: HomeSettings) -> dict[str, FanLimits]:
    result: dict[str, FanLimits] = {}
    for entity_id, raw in table.items():
        _entity(entity_id, "fans", settings, {"fan"})
        values = _table(raw, f"fans.{entity_id}")
        _known(values, {"minimum", "maximum", "max_change"}, f"{_LABEL} fans.{entity_id}")
        minimum = _integer(values.get("minimum", 0), "fans.minimum", 0, 100)
        maximum = _integer(values.get("maximum", 100), "fans.maximum", minimum, 100)
        change = _integer(values.get("max_change", 100), "fans.max_change", 1, 100)
        result[entity_id] = FanLimits(minimum, maximum, change)
    return result


def _media(table: Mapping[str, Any], settings: HomeSettings) -> dict[str, MediaLimits]:
    result: dict[str, MediaLimits] = {}
    for entity_id, raw in table.items():
        _entity(entity_id, "media_players", settings, {"media_player"})
        values = _table(raw, f"media_players.{entity_id}")
        _known(values, {"minimum", "maximum"}, f"{_LABEL} media_players.{entity_id}")
        minimum = _number(values.get("minimum", 0), "media_players.minimum", 0, 1)
        maximum = _number(values.get("maximum"), "media_players.maximum", minimum, 1)
        result[entity_id] = MediaLimits(minimum, maximum)
    return result


def _quiet_hours(table: Mapping[str, Any]) -> QuietHours:
    _known(table, {"enabled", "timezone", "start", "end"}, f"{_LABEL} quiet_hours")
    enabled = _boolean(table, "enabled", "quiet_hours")
    timezone = table.get("timezone", "UTC")
    if not isinstance(timezone, str) or not 1 <= len(timezone) <= 100:
        raise ConfigError(f"{_LABEL} quiet_hours.timezone must name a system time zone")
    if enabled or "timezone" in table:
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ConfigError(f"{_LABEL} quiet_hours.timezone is not installed on this machine") from exc
    start, end = table.get("start", "22:00"), table.get("end", "07:00")
    if (
        not isinstance(start, str)
        or not isinstance(end, str)
        or not _TIME.fullmatch(start)
        or not _TIME.fullmatch(end)
    ):
        raise ConfigError(f"{_LABEL} quiet_hours.start and end must be HH:MM in 24-hour time")
    if start == end:
        raise ConfigError(f"{_LABEL} quiet_hours.start and end must differ")
    return QuietHours(enabled, timezone, start, end)


def _speech(table: Mapping[str, Any], settings: HomeSettings) -> SpeechSettings:
    _known(
        table,
        {"enabled", "engine", "destinations", "external_processing", "allow_external", "allow_sensitive"},
        f"{_LABEL} speech",
    )
    enabled = _boolean(table, "enabled", "speech")
    external = _boolean(table, "external_processing", "speech")
    allow_external = _boolean(table, "allow_external", "speech")
    allow_sensitive = _boolean(table, "allow_sensitive", "speech")
    engine = table.get("engine", "")
    if engine or enabled:
        engine = _entity(engine, "speech.engine", settings, {"tts"})
        if len(engine) > 64:
            raise ConfigError(f"{_LABEL} speech.engine must be at most 64 characters for a complete approval")
    elif not isinstance(engine, str):
        raise ConfigError(f"{_LABEL} speech.engine must be an entity identifier")
    destinations = _entities(
        table.get("destinations", []), "speech.destinations", settings, {"media_player"}, nonempty=enabled
    )
    if any(entity not in settings.media_players or len(entity) > 64 for entity in destinations):
        raise ConfigError(
            f"{_LABEL} speech destinations need media_players limits and names of at most 64 characters"
        )
    if enabled and ("external_processing" not in table or not settings.quiet_hours.enabled):
        raise ConfigError(
            f"{_LABEL} speech needs an explicit external_processing declaration and enabled quiet_hours"
        )
    if enabled and external and not allow_external:
        raise ConfigError(f"{_LABEL} speech external processing requires allow_external = true")
    if enabled and external and allow_sensitive:
        raise ConfigError(f"{_LABEL} speech cannot allow sensitive text with external processing")
    return SpeechSettings(enabled, engine, destinations, external, allow_external, allow_sensitive)


def _presets(table: Mapping[str, Any], settings: HomeSettings) -> dict[str, LightingPreset]:
    result: dict[str, LightingPreset] = {}
    for name, raw in _named(table, "lighting_presets"):
        values = _table(raw, f"lighting_presets.{name}")
        _known(values, {"actions"}, f"{_LABEL} lighting_presets.{name}")
        actions = values.get("actions")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 16:
            raise ConfigError(f"{_LABEL} lighting_presets.{name}.actions must list 1 to 16 light settings")
        parsed = tuple(
            _lighting_action(_table(action, f"lighting_presets.{name}.actions"), settings)
            for action in actions
        )
        if len({action.entity_id for action in parsed}) != len(parsed):
            raise ConfigError(f"{_LABEL} lighting_presets.{name} cannot repeat a target")
        result[name] = LightingPreset(parsed)
    return result


def _lighting_action(values: Mapping[str, Any], settings: HomeSettings) -> LightingAction:
    _known(values, _LIGHT_KEYS, f"{_LABEL} lighting preset action")
    entity_id = _entity(values.get("entity_id"), "lighting preset action", settings, {"light"})
    if entity_id not in settings.lights:
        raise ConfigError(f"{_LABEL} lighting preset targets must also be in lights")
    state = values.get("state")
    if not isinstance(state, str) or state not in {"on", "off"}:
        raise ConfigError(f"{_LABEL} lighting preset state must be on or off")
    brightness = values.get("brightness_pct")
    if brightness is not None:
        brightness = _integer(brightness, "lighting preset brightness_pct", 1, 100)
    kelvin = values.get("color_temp_kelvin")
    if kelvin is not None:
        kelvin = _integer(kelvin, "lighting preset color_temp_kelvin", 1000, 20000)
    color = values.get("rgb_color")
    parsed_color: tuple[int, ...] | None = None
    if color is not None:
        if not isinstance(color, list) or len(color) != 3:
            raise ConfigError(f"{_LABEL} lighting preset rgb_color needs three integers")
        parsed_color = tuple(_integer(channel, "lighting preset rgb_color", 0, 255) for channel in color)
    if kelvin is not None and color is not None:
        raise ConfigError(f"{_LABEL} lighting presets cannot mix color temperature and rgb_color")
    if state == "off" and any(value is not None for value in (brightness, kelvin, color)):
        raise ConfigError(f"{_LABEL} off lighting presets cannot set brightness or color")
    transition = _number(values.get("transition", 0), "lighting preset transition", 0, 300)
    return LightingAction(entity_id, state, brightness, parsed_color, kelvin, transition)

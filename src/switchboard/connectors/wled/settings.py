"""Strict, offline configuration for operator-owned local WLED devices."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from switchboard.errors import ConfigError
from switchboard.jsonschema import validate
from switchboard.util import canonical_json, sha256_hex

NAME_PATTERN = "^[a-z][a-z0-9_]{0,31}$"
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_LOCAL_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
)
_PRESET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "pattern": NAME_PATTERN, "maxLength": 32},
        "id": {"type": "integer", "minimum": 1, "maximum": 250},
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$", "maxLength": 64},
    },
    "required": ["name", "id", "sha256"],
    "additionalProperties": False,
}
_DEVICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "pattern": NAME_PATTERN, "maxLength": 32},
        "base_url": {"type": "string", "minLength": 1, "maxLength": 300},
        "allow_http": {"type": "boolean"},
        "allow_global": {"type": "boolean"},
        "segments": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {"type": "integer", "minimum": 0, "maximum": 63},
        },
        "presets": {"type": "array", "maxItems": 16, "items": _PRESET_SCHEMA},
    },
    "required": ["name", "base_url", "segments"],
    "additionalProperties": False,
}
_SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
        "max_response_bytes": {"type": "integer", "minimum": 4096, "maximum": 262144},
        "devices": {"type": "array", "minItems": 1, "maxItems": 16, "items": _DEVICE_SCHEMA},
    },
    "required": ["devices"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Preset:
    """A pinned, operator-reviewed preset object, not permission to execute a macro."""

    name: str
    id: int
    sha256: str


@dataclass(frozen=True)
class Device:
    """One fixed origin and its independently allowed control scopes."""

    name: str
    base_url: str
    segments: tuple[int, ...]
    allow_http: bool = False
    allow_global: bool = False
    presets: tuple[Preset, ...] = ()


@dataclass(frozen=True)
class Settings:
    devices: tuple[Device, ...]
    timeout_seconds: int = 5
    max_response_bytes: int = 65536


def target_digest(settings: Settings, device: Device) -> str:
    """Bind approved arguments to the configured native engine, target, scopes, and safety limits."""
    return sha256_hex(
        canonical_json(
            {
                "engine": "wled_native_static_v1",
                "name": device.name,
                "base_url": device.base_url,
                "allow_http": device.allow_http,
                "allow_global": device.allow_global,
                "segments": sorted(device.segments),
                "presets": [
                    {"name": preset.name, "id": preset.id, "sha256": preset.sha256}
                    for preset in sorted(device.presets, key=lambda preset: preset.name)
                ],
                "timeout_seconds": settings.timeout_seconds,
                "max_response_bytes": settings.max_response_bytes,
                "max_transition_deciseconds": 100,
                "max_segments": 64,
                "suppress_udp_notification": True,
            }
        )
    )


def load_settings(table: Mapping[str, Any]) -> Settings:
    """Validate settings without network access, name resolution, or secret resolution."""
    problems = validate(dict(table), _SETTINGS_SCHEMA)
    if problems:
        raise ConfigError(f"[connectors.wled]: {'; '.join(problems[:4])}")
    devices: list[Device] = []
    for raw in table["devices"]:
        segments = tuple(raw["segments"])
        if len(set(segments)) != len(segments):
            raise ConfigError("[connectors.wled] segments must not contain duplicates")
        presets = tuple(Preset(**item) for item in raw.get("presets", []))
        if len({item.name for item in presets}) != len(presets):
            raise ConfigError("[connectors.wled] preset names must be unique within a device")
        if len({item.id for item in presets}) != len(presets):
            raise ConfigError("[connectors.wled] preset identifiers must be unique within a device")
        allow_http = raw.get("allow_http", False)
        devices.append(
            Device(
                name=raw["name"],
                base_url=_origin(raw["base_url"], allow_http),
                segments=segments,
                allow_http=allow_http,
                allow_global=raw.get("allow_global", False),
                presets=presets,
            )
        )
    if len({device.name for device in devices}) != len(devices):
        raise ConfigError("[connectors.wled] device names must be unique")
    if len({device.base_url for device in devices}) != len(devices):
        raise ConfigError("[connectors.wled] device endpoints must be unique")
    return Settings(
        devices=tuple(devices),
        timeout_seconds=table.get("timeout_seconds", 5),
        max_response_bytes=table.get("max_response_bytes", 65536),
    )


def _origin(value: str, allow_http: bool) -> str:
    message = "[connectors.wled] base_url must be a local http(s) origin without credentials or a path"
    if not value.isascii() or any(char.isspace() or ord(char) < 33 for char in value):
        raise ConfigError(message)
    if any(char in value for char in "\\%?#@"):
        raise ConfigError(message)
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
    except ValueError as exc:
        raise ConfigError(message) from exc
    if parts.scheme not in {"http", "https"} or not host or parts.path not in {"", "/"}:
        raise ConfigError(message)
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(message)
    if parts.netloc.endswith(":"):
        raise ConfigError(message)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _HOST.fullmatch(host) or not (host == "localhost" or host.endswith((".local", ".home.arpa"))):
            raise ConfigError(message) from None
    else:
        if not any(address.version == network.version and address in network for network in _LOCAL_NETWORKS):
            raise ConfigError(message)
        host = f"[{address}]" if address.version == 6 else str(address)
    if parts.scheme == "http" and not allow_http:
        raise ConfigError("[connectors.wled] every plain-http device requires explicit allow_http = true")
    suffix = f":{port}" if port is not None else ""
    return f"{parts.scheme}://{host}{suffix}"

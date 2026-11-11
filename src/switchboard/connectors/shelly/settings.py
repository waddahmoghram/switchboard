"""Bounded, offline configuration for fixed Shelly devices and nonessential plug loads."""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from switchboard.errors import ConfigError, ToolError
from switchboard.secrets import is_reference
from switchboard.util import canonical_json, sha256_hex

MAX_DEVICES = 16
MAX_SWITCH_ID = 31
MAX_SWITCHES = 16
MAX_CONTROL_WATTS = 100
GENERATIONS = frozenset({2, 3, 4})
CONTROL_MODELS = frozenset({(2, "SNPL-00112EU"), (2, "SNPL-00116US")})

_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_DEVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}")
_MODEL = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,47}")
_CONTROL_ID = re.compile(r"0|[1-9][0-9]?")
_HOST_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
_PRIVATE_V4 = tuple(
    ipaddress.IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_PRIVATE_V6 = ipaddress.IPv6Network("fc00::/7")
_LABEL = "[connectors.shelly]"


@dataclass(frozen=True)
class Control:
    switch_id: int
    max_power_watts: int
    safe_load: str = "nonessential_lighting"


@dataclass(frozen=True)
class Device:
    name: str
    base_url: str
    expected_device_id: str
    expected_model: str
    expected_generation: int
    switch_ids: tuple[int, ...]
    allow_http: bool = False
    password: str | None = field(default=None, repr=False)
    controls: tuple[Control, ...] = ()

    def control(self, switch_id: int) -> Control:
        for control in self.controls:
            if control.switch_id == switch_id:
                return control
        raise ToolError("this Shelly switch is read-only")

    def target(self, control: Control) -> str:
        """Bind an approval to the endpoint, identity, component, authentication and safety policy."""
        return sha256_hex(
            canonical_json(
                {
                    "version": 1,
                    "device": self.name,
                    "base_url": self.base_url,
                    "device_id": self.expected_device_id,
                    "model": self.expected_model,
                    "generation": self.expected_generation,
                    "switch_id": control.switch_id,
                    "allow_http": self.allow_http,
                    "password_reference": self.password,
                    "safe_load": control.safe_load,
                    "max_power_watts": control.max_power_watts,
                }
            )
        )


@dataclass(frozen=True)
class Settings:
    devices: tuple[Device, ...]
    timeout_seconds: int = 5

    def device(self, name: str) -> Device:
        for device in self.devices:
            if device.name == name:
                return device
        raise ToolError("unknown configured Shelly device")


def load_settings(table: Mapping[str, Any]) -> Settings:
    """Validate all settings without resolving secrets, resolving hosts or contacting devices."""
    _keys(table, {"devices", "timeout_seconds"}, _LABEL)
    timeout = _integer(table.get("timeout_seconds", 5), 1, 15, f"{_LABEL} timeout_seconds")
    raw_devices = table.get("devices")
    if not isinstance(raw_devices, Mapping) or not 1 <= len(raw_devices) <= MAX_DEVICES:
        raise ConfigError(f"{_LABEL} devices must contain 1 to {MAX_DEVICES} named device tables")
    devices: list[Device] = []
    endpoints: set[str] = set()
    identities: set[str] = set()
    for name, raw in raw_devices.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ConfigError(f"{_LABEL} device names must be lowercase aliases of at most 32 characters")
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{_LABEL} each device must be a table")
        device = _device(name, raw)
        if device.base_url in endpoints or device.expected_device_id in identities:
            raise ConfigError(f"{_LABEL} duplicate device endpoints or identities are not permitted")
        endpoints.add(device.base_url)
        identities.add(device.expected_device_id)
        devices.append(device)
    return Settings(tuple(sorted(devices, key=lambda item: item.name)), timeout)


def _device(name: str, table: Mapping[str, Any]) -> Device:
    label = f"{_LABEL} devices.{name}"
    _keys(
        table,
        {
            "base_url",
            "expected_device_id",
            "expected_model",
            "expected_generation",
            "switch_ids",
            "allow_http",
            "password",
            "allow_control",
            "controls",
        },
        label,
    )
    allow_http = _boolean(table.get("allow_http", False), f"{label} allow_http")
    allow_control = _boolean(table.get("allow_control", False), f"{label} allow_control")
    device_id = _text(table.get("expected_device_id"), _DEVICE_ID, f"{label} expected_device_id")
    model = _text(table.get("expected_model"), _MODEL, f"{label} expected_model")
    generation = table.get("expected_generation")
    if type(generation) is not int or generation not in GENERATIONS:
        raise ConfigError(f"{label} expected_generation must be 2, 3 or 4; other generations unsupported")
    raw_ids = table.get("switch_ids")
    if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= MAX_SWITCHES:
        raise ConfigError(f"{label} switch_ids must explicitly list 1 to {MAX_SWITCHES} components")
    ids = tuple(_integer(value, 0, MAX_SWITCH_ID, f"{label} switch_ids") for value in raw_ids)
    if len(set(ids)) != len(ids):
        raise ConfigError(f"{label} switch_ids must not contain duplicates")
    password = table.get("password")
    if "password" in table and (
        not is_reference(password)
        or not isinstance(password, str)
        or not 5 <= len(password) <= 512
        or not password.partition(":")[2].strip()
        or not password.isascii()
        or any(ord(char) < 32 or ord(char) == 127 for char in password)
    ):
        raise ConfigError(f"{label} password must be a bounded env:, file: or cmd: secret reference")
    controls = _controls(table.get("controls", {}), allow_control, ids, generation, model, label)
    return Device(
        name,
        _base_url(table.get("base_url"), allow_http, label),
        device_id,
        model,
        generation,
        tuple(sorted(ids)),
        allow_http,
        password,
        controls,
    )


def _controls(
    raw: Any, enabled: bool, ids: tuple[int, ...], generation: int, model: str, label: str
) -> tuple[Control, ...]:
    if not isinstance(raw, Mapping) or len(raw) > MAX_SWITCHES:
        raise ConfigError(f"{label} controls must be a bounded table of component identifiers")
    if bool(raw) != enabled:
        raise ConfigError(f"{label} controls and allow_control = true must be configured together")
    if not enabled:
        return ()
    if (generation, model) not in CONTROL_MODELS:
        raise ConfigError(f"{label} controls support only generation 2 Plus Plug S and Plus Plug US")
    controls: list[Control] = []
    for key, value in raw.items():
        if not isinstance(key, str) or not _CONTROL_ID.fullmatch(key):
            raise ConfigError(f"{label} control identifiers must be canonical decimal strings")
        switch_id = int(key)
        if switch_id != 0 or switch_id not in ids:
            raise ConfigError(f"{label} these supported plugs permit controls only on exposed switch 0")
        if not isinstance(value, Mapping):
            raise ConfigError(f"{label} each control must be a table")
        _keys(value, {"safe_load", "max_power_watts", "safety_acknowledged"}, label)
        if value.get("safe_load") != "nonessential_lighting":
            raise ConfigError(f"{label} only nonessential_lighting loads may be controlled")
        if value.get("safety_acknowledged") is not True:
            raise ConfigError(f"{label} each control requires safety_acknowledged = true")
        watts = _integer(value.get("max_power_watts"), 1, MAX_CONTROL_WATTS, f"{label} max_power_watts")
        controls.append(Control(switch_id, watts))
    return tuple(sorted(controls, key=lambda item: item.switch_id))


def _base_url(value: Any, allow_http: bool, label: str) -> str:
    message = f"{label} base_url must be a fixed http(s) origin without credentials, path, query or fragment"
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 300
        or not value.isascii()
        or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in value)
        or any(char in value for char in "\\%?#")
    ):
        raise ConfigError(message)
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise ConfigError(message) from None
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or "@" in parts.netloc
        or parts.netloc.endswith(":")
        or parts.path not in {"", "/"}
        or port == 0
    ):
        raise ConfigError(message)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if len(host) > 253 or not all(_HOST_LABEL.fullmatch(part) for part in host.split(".")):
            raise ConfigError(message) from None
    if parts.scheme == "http":
        if not allow_http:
            raise ConfigError(f"{label} plain HTTP requires explicit allow_http = true on your own network")
        private = (
            isinstance(address, ipaddress.IPv4Address) and any(address in net for net in _PRIVATE_V4)
        ) or (isinstance(address, ipaddress.IPv6Address) and address in _PRIVATE_V6)
        if not private:
            raise ConfigError(f"{label} plain HTTP requires a private numeric address, not a hostname")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (80 if parts.scheme == "http" else 443):
        netloc += f":{port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, "", "", ""))


def _keys(table: Mapping[str, Any], allowed: set[str], label: str) -> None:
    if any(key not in allowed for key in table):
        raise ConfigError(f"{label} contains unknown settings; check the Shelly connector documentation")


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be true or false")
    return value


def _integer(value: Any, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _text(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ConfigError(f"{label} is missing or malformed")
    return value

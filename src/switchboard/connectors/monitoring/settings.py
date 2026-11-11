"""Explicit, fixed monitoring targets; parsing never inspects a host or resolves a secret."""

from __future__ import annotations

import ipaddress
import math
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar

from switchboard.errors import ConfigError
from switchboard.util import canonical_json, sha256_hex

MAX_CHECKS = 32
MAX_BYTES = 2**63 - 1
NAME_PATTERN = r"[a-z][a-z0-9_]{0,63}"
_NAME = re.compile(NAME_PATTERN)
_HOST_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
_PATH = re.compile(r"/[a-zA-Z0-9_./~-]*")
_FIELD = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,31}(?:\.[a-zA-Z_][a-zA-Z0-9_]{0,31}){0,7}")
_FILENAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,122}\.json")
_ENV = re.compile(r"env:[a-zA-Z_][a-zA-Z0-9_]{0,127}")
_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7")
)


@dataclass(frozen=True)
class EndpointCheck:
    name: str
    url: str
    expected_status: int = 200
    readiness_field: str | None = None
    readiness_equals: bool | str | int | None = None
    bearer_token: str | None = None
    allow_http: bool = False
    timeout_seconds: int = 5
    kind: ClassVar[str] = "endpoint"


@dataclass(frozen=True)
class BackupCheck:
    name: str
    directory: Path
    filename: str
    max_age_seconds: int
    kind: ClassVar[str] = "backup"


@dataclass(frozen=True)
class DiskCheck:
    name: str
    path: Path
    min_free_bytes: int | None = None
    min_free_percent: float | None = None
    kind: ClassVar[str] = "disk"


Check = EndpointCheck | BackupCheck | DiskCheck


@dataclass(frozen=True)
class Settings:
    checks: tuple[Check, ...]
    poll_interval_seconds: int = 300
    snapshot_stale_after_seconds: int = 600


def load_settings(raw: Mapping[str, Any]) -> Settings:
    """Validate all settings lexically, without reading files or resolving addresses."""
    _keys(raw, {"checks", "poll_interval_seconds", "snapshot_stale_after_seconds"})
    poll = _integer(raw.get("poll_interval_seconds", 300), "poll_interval_seconds", 30, 86400)
    stale = _integer(
        raw.get("snapshot_stale_after_seconds", max(600, poll * 2)),
        "snapshot_stale_after_seconds",
        poll,
        604800,
    )
    targets = raw.get("checks")
    if not isinstance(targets, Mapping) or not 1 <= len(targets) <= MAX_CHECKS:
        raise ConfigError(f"monitoring: checks must contain 1-{MAX_CHECKS} named tables")
    if any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in targets):
        raise ConfigError("monitoring: check names must be lowercase letters, digits, and underscores")
    checks = tuple(_check(name, targets[name]) for name in sorted(targets))
    return Settings(checks, poll, stale)


def configuration_key(check: Check) -> str:
    """Hash configuration, including references but never resolved secret values."""
    values = asdict(check)
    values["kind"] = check.kind
    return sha256_hex(canonical_json(values))


def _check(name: str, raw: Any) -> Check:
    if not isinstance(raw, Mapping):
        raise ConfigError("monitoring: each check must be a table")
    kind = raw.get("kind")
    if kind == "endpoint":
        return _endpoint(name, raw)
    if kind == "backup":
        _keys(raw, {"kind", "directory", "filename", "max_age_seconds"})
        filename = raw.get("filename")
        if not isinstance(filename, str) or not _FILENAME.fullmatch(filename):
            raise ConfigError("monitoring: filename must be a simple .json basename, at most 128 characters")
        return BackupCheck(
            name,
            _absolute_path(raw.get("directory"), "directory"),
            filename,
            _integer(raw.get("max_age_seconds"), "max_age_seconds", 30, 31536000),
        )
    if kind == "disk":
        _keys(raw, {"kind", "path", "min_free_bytes", "min_free_percent"})
        minimum_bytes = None
        if "min_free_bytes" in raw:
            minimum_bytes = _integer(raw["min_free_bytes"], "min_free_bytes", 1, MAX_BYTES)
        minimum_percent = None
        if "min_free_percent" in raw:
            value = raw["min_free_percent"]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= 100
                or not math.isfinite(value)
            ):
                raise ConfigError(
                    "monitoring: min_free_percent must be finite, greater than 0, and at most 100"
                )
            minimum_percent = float(value)
        if minimum_bytes is None and minimum_percent is None:
            raise ConfigError("monitoring: a disk check needs at least one free-capacity threshold")
        return DiskCheck(name, _absolute_path(raw.get("path"), "path"), minimum_bytes, minimum_percent)
    raise ConfigError("monitoring: kind must be endpoint, backup, or disk")


def _endpoint(name: str, raw: Mapping[str, Any]) -> EndpointCheck:
    _keys(
        raw,
        {
            "kind",
            "url",
            "expected_status",
            "readiness_field",
            "readiness_equals",
            "bearer_token",
            "allow_http",
            "timeout_seconds",
        },
    )
    allow_http = raw.get("allow_http", False)
    if not isinstance(allow_http, bool):
        raise ConfigError("monitoring: allow_http must be a boolean")
    field = raw.get("readiness_field")
    expected = raw.get("readiness_equals")
    if ("readiness_field" in raw) != ("readiness_equals" in raw):
        raise ConfigError("monitoring: readiness_field and readiness_equals must be supplied together")
    if "readiness_field" in raw:
        if not isinstance(field, str) or len(field) > 255 or not _FIELD.fullmatch(field):
            raise ConfigError("monitoring: readiness_field must be a bounded dotted property path")
        if not (
            isinstance(expected, bool)
            or (isinstance(expected, str) and 1 <= len(expected) <= 128 and _printable(expected))
            or (type(expected) is int and -1_000_000_000 <= expected <= 1_000_000_000)
        ):
            raise ConfigError("monitoring: readiness_equals needs a boolean, bounded integer, or short text")
    token = raw.get("bearer_token")
    if "bearer_token" in raw:
        if not isinstance(token, str) or len(token) > 4096:
            raise ConfigError("monitoring: bearer_token must be an env: or absolute file: reference")
        if not _ENV.fullmatch(token):
            if not token.startswith("file:"):
                raise ConfigError("monitoring: command and literal secret values are forbidden")
            _absolute_path(token[5:], "bearer_token file")
    return EndpointCheck(
        name,
        _url(raw.get("url"), allow_http),
        _integer(raw.get("expected_status", 200), "expected_status", 200, 299),
        field,
        expected,
        token,
        allow_http,
        _integer(raw.get("timeout_seconds", 5), "timeout_seconds", 1, 30),
    )


def _url(value: Any, allow_http: bool) -> str:
    message = "monitoring: url must be a fixed http(s) address without credentials, query, or fragment"
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or not _printable(value)
        or any(char in value for char in "\\@?#%")
        or " " in value
        or not value.startswith(("https://", "http://"))
    ):
        raise ConfigError(message)
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
    except ValueError as exc:
        raise ConfigError(message) from exc
    if not host or parts.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535):
        raise ConfigError(message)
    address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            len(host) > 253
            or re.fullmatch(r"[0-9.]+", host)
            or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
        ):
            raise ConfigError(message) from None
    if address is not None and (address.is_unspecified or address.is_multicast):
        raise ConfigError(message)
    authority = f"[{host}]" if isinstance(address, ipaddress.IPv6Address) else host
    if port is not None:
        authority = f"{authority}:{port}"
    if parts.netloc.lower() != authority.lower():
        raise ConfigError(message)
    if parts.path and (
        not _PATH.fullmatch(parts.path) or any(p in {".", ".."} for p in parts.path.split("/"))
    ):
        raise ConfigError("monitoring: endpoint paths must be simple absolute paths without dot segments")
    if parts.scheme == "http" and (
        not allow_http
        or not (
            host == "localhost" or (address is not None and any(address in net for net in _LOCAL_NETWORKS))
        )
    ):
        raise ConfigError("monitoring: plain http requires allow_http=true and a private or loopback literal")
    return value


def _absolute_path(value: Any, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or not _printable(value)
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ConfigError(f"monitoring: {label} must be an explicit absolute local path without dot segments")
    return Path(value)


def _keys(raw: Mapping[str, Any], allowed: set[str]) -> None:
    if set(raw) - allowed:
        raise ConfigError("monitoring: unknown configuration option")


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"monitoring: {label} must be an integer between {low} and {high}")
    return value


def _printable(value: str) -> bool:
    return value.isascii() and all(32 <= ord(char) <= 126 for char in value)

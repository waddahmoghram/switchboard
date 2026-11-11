"""Validate ``[connectors.calendar]`` without touching the network or resolving secrets."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any

from switchboard import secrets
from switchboard.connectors.calendar.ics import zone_named
from switchboard.errors import ConfigError

NAME = "calendar"
SOURCE_TYPES = ("ics_url", "caldav", "ics_file")
SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
EMAIL = re.compile(r"^[^@\s<>,;:\"()\[\]\\]+@[^@\s<>,;:\"()\[\]\\]+$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_TABLE = "[connectors.calendar]"


@dataclass(frozen=True)
class Source:
    """One ``[connectors.calendar.sources.<name>]`` table; ics_url ``url`` and ``password`` are references."""

    name: str
    type: str
    url: str = ""
    username: str = ""
    password: str = ""
    path: str = ""
    writable: bool = False
    allow_http: bool = False


@dataclass(frozen=True)
class Settings:
    zone: tzinfo
    refresh_seconds: int = 900
    upcoming_minutes: int = 30
    invite_account: str | None = None
    owner_email: str | None = None
    sources: Mapping[str, Source] = field(default_factory=dict)


def _text(table: Mapping[str, Any], key: str, label: str, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} {key} must be a non-empty string")
    return value.strip()


def _optional_text(table: Mapping[str, Any], key: str, label: str) -> str | None:
    return None if table.get(key) is None else _text(table, key, label)


def _integer(table: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{_TABLE} {key} must be a whole number from {low} to {high}")
    return value


def _flag(table: Mapping[str, Any], key: str, label: str) -> bool:
    value = table.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"{label} {key} must be true or false")
    return value


def _reference(table: Mapping[str, Any], key: str, label: str, why: str) -> str:
    value = _text(table, key, label)
    if not secrets.is_reference(value):
        raise ConfigError(f"{label} {key} must be a secret reference (env:, file:, or cmd:) {why}")
    return value


def _check_server_url(url: str, allow_http: bool, label: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.hostname and parts.scheme == "https":
        return
    if parts.hostname and parts.scheme == "http" and (allow_http or parts.hostname in _LOOPBACK):
        return
    raise ConfigError(
        f"{label} url must start with https:// (allow_http = true is for servers on your own network)"
    )


def _source(name: str, table: Any) -> Source:
    label = f"[connectors.calendar.sources.{name}]"
    if not SOURCE_NAME.match(name):
        raise ConfigError(f"{label}: source names may use letters, digits, '.', '_', and '-'")
    if not isinstance(table, Mapping):
        raise ConfigError(f"{label} must be a table")
    kind = table.get("type")
    if kind not in SOURCE_TYPES:
        raise ConfigError(f"{label} type must be one of: {', '.join(SOURCE_TYPES)}")
    allow_http = _flag(table, "allow_http", label)
    if kind == "ics_file":
        return Source(name, kind, path=_text(table, "path", label))
    if kind == "ics_url":
        url = _reference(table, "url", label, "because the feed address alone grants read access")
        return Source(name, kind, url=url, allow_http=allow_http)
    url = _text(table, "url", label)
    _check_server_url(url, allow_http, label)
    return Source(
        name,
        kind,
        url=url,
        username=_text(table, "username", label),
        password=_reference(table, "password", label, "and never the password itself"),
        writable=_flag(table, "writable", label),
        allow_http=allow_http,
    )


def load_settings(raw: Mapping[str, Any]) -> Settings:
    """Parse and check the connector table; raises :class:`ConfigError` with the offending key."""
    zone_name = _text(raw, "timezone", _TABLE, "UTC")
    zone = zone_named(zone_name)
    if zone is None:
        raise ConfigError(f"{_TABLE} timezone {zone_name!r} is not a known IANA time zone")
    owner = _optional_text(raw, "owner_email", _TABLE)
    if owner is not None and not EMAIL.match(owner):
        raise ConfigError(f"{_TABLE} owner_email must be a plain email address")
    tables = raw.get("sources", {})
    if not isinstance(tables, Mapping):
        raise ConfigError("[connectors.calendar.sources] must be a table of named sources")
    return Settings(
        zone=zone,
        refresh_seconds=_integer(raw, "refresh_interval_seconds", 900, 60, 7 * 86400),
        upcoming_minutes=_integer(raw, "upcoming_minutes", 30, 1, 24 * 60),
        invite_account=_optional_text(raw, "invite_account", _TABLE),
        owner_email=owner,
        sources={str(name): _source(str(name), table) for name, table in tables.items()},
    )

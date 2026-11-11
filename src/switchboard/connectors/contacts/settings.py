"""Validate ``[connectors.contacts]`` without touching the network or resolving secrets."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from switchboard import secrets
from switchboard.errors import ConfigError

NAME = "contacts"
SOURCE_TYPES = ("carddav", "vcf_file")
SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class Source:
    """One ``[connectors.contacts.sources.<name>]`` table; ``password`` is a secret reference."""

    name: str
    type: str
    url: str = ""
    username: str = ""
    password: str = ""
    path: str = ""
    allow_http: bool = False


@dataclass(frozen=True)
class Settings:
    refresh_seconds: int = 21600
    sources: Mapping[str, Source] = field(default_factory=dict)


def _text(table: Mapping[str, Any], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} {key} must be a non-empty string")
    return value.strip()


def _flag(table: Mapping[str, Any], key: str, label: str) -> bool:
    value = table.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"{label} {key} must be true or false")
    return value


def _source(name: str, table: Any) -> Source:
    label = f"[connectors.contacts.sources.{name}]"
    if not SOURCE_NAME.match(name):
        raise ConfigError(f"{label}: source names may use letters, digits, '.', '_', and '-'")
    if not isinstance(table, Mapping):
        raise ConfigError(f"{label} must be a table")
    kind = table.get("type")
    if kind not in SOURCE_TYPES:
        raise ConfigError(f"{label} type must be one of: {', '.join(SOURCE_TYPES)}")
    if kind == "vcf_file":
        return Source(name, kind, path=_text(table, "path", label))
    url = _text(table, "url", label)
    allow_http = _flag(table, "allow_http", label)
    parts = urllib.parse.urlsplit(url)
    plain_ok = parts.scheme == "http" and (allow_http or parts.hostname in _LOOPBACK)
    if not parts.hostname or not (parts.scheme == "https" or plain_ok):
        raise ConfigError(f"{label} url must start with https:// (allow_http = true is for your own network)")
    password = _text(table, "password", label)
    if not secrets.is_reference(password):
        raise ConfigError(
            f"{label} password must be a secret reference (env:, file:, or cmd:), never the password"
        )
    return Source(
        name,
        kind,
        url=url,
        username=_text(table, "username", label),
        password=password,
        allow_http=allow_http,
    )


def load_settings(raw: Mapping[str, Any]) -> Settings:
    """Parse and check the connector table; raises :class:`ConfigError` naming the offending key."""
    refresh = raw.get("refresh_interval_seconds", 21600)
    if isinstance(refresh, bool) or not isinstance(refresh, int) or not 300 <= refresh <= 30 * 86400:
        raise ConfigError(
            "[connectors.contacts] refresh_interval_seconds must be a whole number from 300 to 2592000"
        )
    tables = raw.get("sources", {})
    if not isinstance(tables, Mapping):
        raise ConfigError("[connectors.contacts.sources] must be a table of named sources")
    return Settings(
        refresh_seconds=refresh,
        sources={str(name): _source(str(name), table) for name, table in tables.items()},
    )

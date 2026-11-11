"""Offline validation for private exports and optional own-profile publication."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from switchboard import secrets
from switchboard.errors import ConfigError

NAME = "linkedin"
DEFAULT_API_VERSION = "202610"
MAX_POST_CHARS = 3000
VISIBILITIES = ("PUBLIC", "CONNECTIONS")
AUTHOR_PATTERN = r"urn:li:person:[A-Za-z0-9_-]{1,128}"
VERSION_PATTERN = r"20[0-9]{2}(?:0[1-9]|1[0-2])"
_KEYS = frozenset({"import_dir", "publishing_enabled", "access_token", "author_urn", "api_version"})


@dataclass(frozen=True)
class Settings:
    import_dir: Path | None = None
    publishing_enabled: bool = False
    access_token: str = ""
    author_urn: str = ""
    api_version: str = DEFAULT_API_VERSION


def load_settings(raw: Mapping[str, Any]) -> Settings:
    """Validate settings without reading exports, resolving secrets, or contacting LinkedIn."""
    label = "[connectors.linkedin]"
    if set(raw) - _KEYS:
        raise ConfigError(f"{label} contains unknown settings")
    enabled = raw.get("publishing_enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{label} publishing_enabled must be true or false")
    directory = raw.get("import_dir", "")
    if not isinstance(directory, str) or len(directory) > 4096 or "\x00" in directory:
        raise ConfigError(f"{label} import_dir must be a path string")
    author = raw.get("author_urn", "")
    if not isinstance(author, str) or (author and re.fullmatch(AUTHOR_PATTERN, author) is None):
        raise ConfigError(f"{label} author_urn must be your urn:li:person: identifier, not a profile URL")
    reference = raw.get("access_token", "")
    if (
        not isinstance(reference, str)
        or len(reference) > 4096
        or (reference and not secrets.is_reference(reference))
    ):
        raise ConfigError(f"{label} access_token must be an env:, file:, or cmd: secret reference")
    version = raw.get("api_version", DEFAULT_API_VERSION)
    if not isinstance(version, str) or re.fullmatch(VERSION_PATTERN, version) is None:
        raise ConfigError(f"{label} api_version must be an explicit YYYYMM version")
    if enabled and (not author or not reference):
        raise ConfigError(f"{label} publishing requires your author_urn and an access_token secret reference")
    return Settings(
        import_dir=Path(directory).expanduser() if directory else None,
        publishing_enabled=enabled,
        access_token=reference,
        author_urn=author,
        api_version=version,
    )

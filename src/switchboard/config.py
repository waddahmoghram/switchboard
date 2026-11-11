"""Configuration loading.

The configuration is a TOML file (``~/.config/switchboard/config.toml`` by default, or the path in
``SWITCHBOARD_CONFIG``). It never contains secrets directly: credentials are *references* resolved at
use time by :mod:`switchboard.secrets`.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from switchboard.errors import ConfigError

_CORE_KEYS = {
    "switchboard": frozenset({"connectors", "data_dir", "event_retention_days"}),
    "policy": frozenset({"tools", "approval_ttl_minutes"}),
    "mcp": frozenset({"expose", "instructions"}),
}


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def default_config_path() -> Path:
    override = os.environ.get("SWITCHBOARD_CONFIG")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "switchboard" / "config.toml"


def default_data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "switchboard"


@dataclass(frozen=True)
class Config:
    """Parsed configuration plus the resolved private data directory."""

    data: dict[str, Any]
    data_dir: Path
    path: Path | None = None

    def __post_init__(self) -> None:
        unknown = set(self.data) - (_CORE_KEYS.keys() | {"connectors", "rules"})
        if unknown:
            raise ConfigError(f"unknown configuration section(s): {', '.join(sorted(unknown))}")
        self.section("connectors")
        for section, allowed in _CORE_KEYS.items():
            unknown = set(self.section(section)) - allowed
            if unknown:
                raise ConfigError(f"[{section}]: unknown setting(s): {', '.join(sorted(unknown))}")
        self.mcp_exposure()
        for section, key in (
            ("switchboard", "event_retention_days"),
            ("policy", "approval_ttl_minutes"),
        ):
            settings = self.section(section)
            if key in settings:
                value = settings[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ConfigError(f"[{section}] {key} must be a positive whole number")

    def section(self, *keys: str) -> dict[str, Any]:
        """Return a nested table; missing tables are empty, malformed tables are errors."""
        node: Any = self.data
        location: list[str] = []
        for key in keys:
            location.append(key)
            node = node.get(key, {})
            if not isinstance(node, dict):
                raise ConfigError(f"[{'.'.join(location)}] must be a table")
        return node

    def mcp_exposure(self) -> list[str] | None:
        """An absent exposure list means all tools; an explicitly empty one means none."""
        value = self.section("mcp").get("expose")
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(item, str) for item in value)
        ):
            raise ConfigError("[mcp] expose must be a list of tool-name patterns")
        return value

    def enabled_connectors(self) -> list[str]:
        value = self.section("switchboard").get("connectors", [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError("[switchboard] connectors must be a list of connector names")
        return list(value)

    def connector(self, name: str) -> dict[str, Any]:
        return self.section("connectors", name)


def config_from_dict(data: dict[str, Any], data_dir: Path) -> Config:
    """Build a configuration in memory (used by tests and ``init``)."""
    return Config(data=data, data_dir=data_dir, path=None)


def load_config(path: Path | None = None) -> Config:
    target = (path or default_config_path()).expanduser()
    if not target.exists():
        raise ConfigError(f"configuration not found at {target}; run 'switchboard init' first")
    try:
        with target.open("rb") as handle:
            data = tomllib.load(handle)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{target}: configuration must be UTF-8 text") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{target}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {target}: {exc.strerror}") from exc
    initial = Config(data=data, data_dir=default_data_dir(), path=target)
    raw_dir = initial.section("switchboard").get("data_dir")
    if raw_dir is not None and not isinstance(raw_dir, str):
        raise ConfigError("[switchboard] data_dir must be a path string")
    data_dir = Path(os.path.expanduser(raw_dir)) if isinstance(raw_dir, str) else default_data_dir()
    return Config(data=data, data_dir=data_dir, path=target)


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` with owner-only permissions, tightening an existing directory if needed."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigError(f"{path} must be a real directory, not a link or file")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def ensure_config_dir(path: Path) -> Path:
    """Create a private config folder without changing an existing parent folder's permissions."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise ConfigError(f"cannot prepare configuration directory {path}: {exc.strerror}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError(f"{path} must be a real directory, not a link or file")
    if info.st_mode & 0o022:
        raise ConfigError(f"{path} must not be writable by other users")
    return path

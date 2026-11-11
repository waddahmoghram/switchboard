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

    def section(self, *keys: str) -> dict[str, Any]:
        """Return a nested table, or an empty dict when any level is missing."""
        node: Any = self.data
        for key in keys:
            if not isinstance(node, dict):
                return {}
            node = node.get(key, {})
        return node if isinstance(node, dict) else {}

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
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{target}: invalid TOML: {exc}") from exc
    raw_dir = data.get("switchboard", {}).get("data_dir")
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

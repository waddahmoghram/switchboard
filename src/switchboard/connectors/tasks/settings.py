"""Strict, account-free settings for the local task store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.errors import ConfigError


@dataclass(frozen=True)
class Settings:
    poll_interval_seconds: int = 60
    max_reminders_per_tick: int = 100


def load_settings(raw: Mapping[str, Any]) -> Settings:
    unknown = set(raw) - {"poll_interval_seconds", "max_reminders_per_tick"}
    if unknown:
        raise ConfigError(f"tasks: unknown settings: {', '.join(sorted(unknown))}")
    interval = raw.get("poll_interval_seconds", 60)
    maximum = raw.get("max_reminders_per_tick", 100)
    if type(interval) is not int or not 30 <= interval <= 3600:
        raise ConfigError("tasks.poll_interval_seconds must be an integer from 30 to 3600")
    if type(maximum) is not int or not 1 <= maximum <= 500:
        raise ConfigError("tasks.max_reminders_per_tick must be an integer from 1 to 500")
    return Settings(interval, maximum)

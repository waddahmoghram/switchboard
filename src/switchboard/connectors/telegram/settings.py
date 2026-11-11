"""Telegram transport settings; credentials are resolved only when a tool or job runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.errors import ConfigError
from switchboard.secrets import is_reference

MAX_ID = 4_503_599_627_370_495


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    allow_groups: bool = False
    poll_interval_seconds: int = 30
    long_poll_seconds: int = 10
    retention_days: int = 14

    def allows(self, user_id: int, chat_id: int, chat_type: str) -> bool:
        return (
            user_id in self.allowed_user_ids
            and chat_id in self.allowed_chat_ids
            and (chat_type == "private" or (self.allow_groups and chat_type in {"group", "supergroup"}))
        )


def load_settings(raw: Mapping[str, Any]) -> Settings:
    keys = {
        "bot_token",
        "allowed_user_ids",
        "allowed_chat_ids",
        "allow_groups",
        "poll_interval_seconds",
        "long_poll_seconds",
        "retention_days",
    }
    if set(raw) - keys:
        raise ConfigError("telegram: unknown settings: " + ", ".join(sorted(set(raw) - keys)))
    token = raw.get("bot_token")
    if not is_reference(token):
        raise ConfigError("telegram: bot_token must be a secret reference")
    groups = raw.get("allow_groups", False)
    if not isinstance(groups, bool):
        raise ConfigError("telegram: allow_groups must be a boolean")
    return Settings(
        str(token),
        _ids(raw.get("allowed_user_ids"), "allowed_user_ids", positive=True),
        _ids(raw.get("allowed_chat_ids"), "allowed_chat_ids", positive=False),
        groups,
        _integer(raw, "poll_interval_seconds", 30, 30, 86400),
        _integer(raw, "long_poll_seconds", 10, 0, 20),
        _integer(raw, "retention_days", 14, 1, 3650),
    )


def _ids(value: Any, label: str, *, positive: bool) -> frozenset[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ConfigError(f"telegram: {label} must list 1-100 numeric identifiers")
    if any(
        isinstance(item, bool)
        or not isinstance(item, int)
        or item == 0
        or abs(item) > MAX_ID
        or (positive and item < 0)
        for item in value
    ):
        raise ConfigError(f"telegram: {label} contains an invalid identifier")
    return frozenset(value)


def _integer(raw: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"telegram: {key} must be an integer between {low} and {high}")
    return value

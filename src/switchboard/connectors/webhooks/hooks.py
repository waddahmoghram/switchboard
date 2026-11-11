"""Webhook settings: ``[connectors.webhooks]`` and one ``[connectors.webhooks.hooks.<name>]`` per sender."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.errors import ConfigError
from switchboard.secrets import is_reference

VERIFY_KINDS = ("hmac-sha256", "github", "token-header", "msgraph", "google-calendar")
DEFAULT_MAX_BODY_BYTES = 256 * 1024
MIN_BODY_BYTES = 256
MAX_BODY_BYTES = 4 * 1024 * 1024
DEFAULT_TOLERANCE_SECONDS = 300

_HOOK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")


@dataclass(frozen=True)
class Hook:
    """One endpoint, ``POST /hooks/<name>``, and how to verify what arrives there."""

    name: str
    verify: str
    secret: str  # a secret reference; resolved by the receiver, never stored here
    header: str = ""  # token-header only: the lowercased header that carries the secret
    tolerance: int = DEFAULT_TOLERANCE_SECONDS  # hmac-sha256 only: accepted clock difference in seconds


@dataclass(frozen=True)
class WebhookSettings:
    hooks: Mapping[str, Hook]
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES


def load_settings(table: Mapping[str, Any]) -> WebhookSettings:
    """Parse the connector table. Raises ConfigError with the offending table named; no secrets resolved."""
    max_body = table.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)
    if not _int_between(max_body, MIN_BODY_BYTES, MAX_BODY_BYTES):
        raise ConfigError(
            "[connectors.webhooks] max_body_bytes must be an integer "
            f"from {MIN_BODY_BYTES} to {MAX_BODY_BYTES}"
        )
    raw = table.get("hooks", {})
    if not isinstance(raw, Mapping):
        raise ConfigError("[connectors.webhooks.hooks] must be a table of hooks")
    hooks = {str(name): _parse_hook(name, value) for name, value in raw.items()}
    return WebhookSettings(hooks=hooks, max_body_bytes=int(max_body))


def _parse_hook(name: Any, table: Any) -> Hook:
    label = f"[connectors.webhooks.hooks.{name}]"
    if not isinstance(name, str) or not _HOOK_NAME.fullmatch(name):
        raise ConfigError(f"{label}: hook names use lowercase letters, digits, '-' and '_' (at most 64)")
    if not isinstance(table, Mapping):
        raise ConfigError(f"{label} must be a table")
    verify = table.get("verify")
    if verify not in VERIFY_KINDS:
        raise ConfigError(f"{label}: verify must be one of {', '.join(VERIFY_KINDS)}")
    reference = table.get("secret")
    if not is_reference(reference):
        raise ConfigError(
            f"{label}: secret must be a secret reference such as env:NAME, file:/path, or cmd:program"
        )
    header = table.get("header", "")
    if verify == "token-header" and not (isinstance(header, str) and _HEADER_NAME.fullmatch(header)):
        raise ConfigError(f'{label}: token-header hooks need header = "X-Your-Header-Name"')
    tolerance = table.get("tolerance_seconds", DEFAULT_TOLERANCE_SECONDS)
    if not _int_between(tolerance, 30, 3600):
        raise ConfigError(f"{label}: tolerance_seconds must be an integer from 30 to 3600")
    return Hook(
        name=name,
        verify=str(verify),
        secret=str(reference),
        header=str(header).lower() if verify == "token-header" else "",
        tolerance=int(tolerance),
    )


def _int_between(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high

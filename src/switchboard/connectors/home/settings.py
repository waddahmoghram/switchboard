"""Home Assistant settings: ``[connectors.home]``.

``expose`` lists the entity globs assistants may see; ``deny_domains`` always wins over ``expose``.
The default deny list blocks locks, alarm panels, covers (which include garage doors), and cameras.
Setting ``deny_domains`` replaces the default, so keep every domain you still want blocked in the list.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.errors import ConfigError
from switchboard.secrets import is_reference
from switchboard.util import glob_any

DEFAULT_DENY_DOMAINS = ("lock", "alarm_control_panel", "cover", "camera")
DOMAIN = re.compile(r"[a-z0-9_]{1,64}")
ENTITY_ID = re.compile(r"[a-z0-9_]{1,64}\.[a-z0-9_]{1,190}")
_LABEL = "[connectors.home]"


@dataclass(frozen=True)
class HomeSettings:
    base_url: str
    token: str  # secret reference to a long-lived access token
    expose: tuple[str, ...] = ()
    deny_domains: frozenset[str] = frozenset(DEFAULT_DENY_DOMAINS)
    allow_http: bool = False

    def visible(self, entity_id: str) -> bool:
        """Exposed by a glob and not in a denied domain."""
        domain = entity_id.partition(".")[0].lower()
        return domain not in self.deny_domains and glob_any(entity_id, self.expose)


def load_settings(table: Mapping[str, Any]) -> HomeSettings:
    """Parse and check the table without resolving the token or contacting Home Assistant."""
    allow_http = table.get("allow_http", False)
    if not isinstance(allow_http, bool):
        raise ConfigError(f"{_LABEL} allow_http must be true or false")
    token = table.get("token")
    if not is_reference(token):
        raise ConfigError(
            f"{_LABEL} token must be a secret reference such as env:NAME, file:/path, or cmd:program"
        )
    return HomeSettings(
        base_url=_base_url(table.get("base_url"), allow_http),
        token=str(token),
        expose=_expose(table.get("expose", [])),
        deny_domains=_deny_domains(table.get("deny_domains", list(DEFAULT_DENY_DOMAINS))),
        allow_http=allow_http,
    )


def _base_url(value: Any, allow_http: bool) -> str:
    message = f"{_LABEL} base_url must be an https:// address without credentials, query, or fragment"
    if not isinstance(value, str):
        raise ConfigError(message)
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ConfigError(message) from exc
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ConfigError(message)
    if parts.query or parts.fragment or "@" in parts.netloc:
        raise ConfigError(message)
    if parts.scheme == "http" and not allow_http:
        raise ConfigError(
            f"{_LABEL} base_url uses http, which sends the token in clear text; use https, or set "
            "allow_http = true only for a server on your own network"
        )
    return value.rstrip("/")


def _expose(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 100
        or not all(isinstance(item, str) and 0 < len(item) <= 255 for item in value)
    ):
        raise ConfigError(f'{_LABEL} expose must list entity globs such as "light.*" or "sensor.outdoor_*"')
    return tuple(value)


def _deny_domains(value: Any) -> frozenset[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and DOMAIN.fullmatch(item) for item in value
    ):
        raise ConfigError(f'{_LABEL} deny_domains must list domains such as "lock" or "cover"')
    return frozenset(value)

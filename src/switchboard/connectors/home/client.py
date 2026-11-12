"""A small client for Home Assistant's Representational State Transfer interface.

Endpoints used: ``GET /api/states``, ``GET /api/states/<entity_id>``, and
``POST /api/services/<domain>/<service>``, each with ``Authorization: Bearer <long-lived token>``.
Callers validate entity ids, domains, and service names before they reach a URL path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from switchboard.connectors.home.settings import ENTITY_ID, HomeSettings
from switchboard.context import AppContext
from switchboard.errors import HttpError, SecretError, ToolError, UncertainOutcomeError
from switchboard.httpclient import HttpResponse

_HEADER_SAFE = re.compile(r"[!-~]+")
_STATUS_MESSAGES = {
    400: "Home Assistant rejected the request (HTTP 400); check the action name and its data",
    401: "Home Assistant refused the access token (HTTP 401)",
    403: "Home Assistant refused the access token (HTTP 403)",
    404: "Home Assistant does not know that entity or action (HTTP 404)",
    500: "the action failed inside Home Assistant (HTTP 500)",
    503: "Home Assistant is starting or shutting down (HTTP 503)",
}


@dataclass(frozen=True)
class ActionReceipt:
    """Transport acceptance is separate from observed state or physical completion."""

    accepted: bool | None
    changed: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class HomeAssistant:
    def __init__(self, ctx: AppContext, settings: HomeSettings) -> None:
        self.ctx = ctx
        self.settings = settings

    def states(self, *, strict: bool = False) -> list[dict[str, Any]]:
        """Every state the token may read, as Home Assistant returns them (filter before showing)."""
        data = _json(self._request("GET", "/api/states"))
        if not isinstance(data, list):
            raise ToolError("Home Assistant returned an unexpected states list")
        if strict and any(
            not isinstance(item, dict)
            or not isinstance(item.get("entity_id"), str)
            or not ENTITY_ID.fullmatch(item["entity_id"])
            or not isinstance(item.get("state"), str)
            or not isinstance(item.get("attributes"), dict)
            for item in data
        ):
            raise ToolError("Home Assistant returned a malformed states list")
        return [item for item in data if isinstance(item, dict) and isinstance(item.get("entity_id"), str)]

    def state(self, entity_id: str) -> dict[str, Any]:
        data = _json(self._request("GET", f"/api/states/{entity_id}"))
        if not isinstance(data, dict) or data.get("entity_id") != entity_id:
            raise ToolError("Home Assistant returned an unexpected state")
        return data

    def call_service(self, domain: str, service: str, data: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Run one action and return the states it changed (empty when the reply lists none)."""
        body = json.dumps(dict(data)).encode("utf-8")
        response = self._request("POST", f"/api/services/{domain}/{service}", body)
        try:
            changed = response.json()
        except ValueError:
            return []  # the action already ran; an unreadable list of changes is not a failure
        if not isinstance(changed, list):
            return []
        return [item for item in changed if isinstance(item, dict) and isinstance(item.get("entity_id"), str)]

    def submit(self, domain: str, service: str, data: Mapping[str, Any]) -> ActionReceipt:
        """Send an effect once, without redirects or retries, preserving ambiguous outcomes."""
        body = json.dumps(dict(data), allow_nan=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        try:
            response = self.ctx.http.request(
                "POST",
                f"{self.settings.base_url}/api/services/{domain}/{service}",
                headers=headers,
                body=body,
                timeout=15,
                max_bytes=1024 * 1024,
                allow_http=self.settings.allow_http,
                follow_redirects=False,
            )
        except HttpError:
            return ActionReceipt(
                None,
                error="request outcome is unknown; inspect state before any new request; nothing was resent",
            )
        if not response.ok:
            ambiguous = response.status >= 500 or response.status == 408
            message = _STATUS_MESSAGES.get(response.status, f"Home Assistant answered HTTP {response.status}")
            if ambiguous:
                message += "; effects may be partial; nothing was resent"
            return ActionReceipt(None if ambiguous else False, error=message)
        try:
            changed = response.json()
        except ValueError:
            changed = []
        if not isinstance(changed, list):
            changed = []
        return ActionReceipt(
            True,
            [item for item in changed if isinstance(item, dict) and isinstance(item.get("entity_id"), str)],
        )

    def _request(self, method: str, path: str, body: bytes | None = None) -> HttpResponse:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.ctx.http.request(
                method,
                self.settings.base_url + path,
                headers=headers,
                body=body,
                timeout=15,
                max_bytes=8 * 1024 * 1024,
                allow_http=self.settings.allow_http,
                follow_redirects=False,
            )
        except HttpError:
            message = "cannot reach Home Assistant"
            if method == "POST":
                message += "; action outcome may be unknown; nothing was resent"
                raise UncertainOutcomeError(message) from None
            raise ToolError(message) from None
        if not response.ok:
            message = _STATUS_MESSAGES.get(response.status, f"Home Assistant answered HTTP {response.status}")
            if method == "POST" and (response.status >= 500 or response.status == 408):
                raise UncertainOutcomeError(message)
            raise ToolError(message)
        return response

    def _token(self) -> str:
        try:
            token = self.ctx.secret(self.settings.token)
        except SecretError as exc:
            raise ToolError(f"the Home Assistant token reference does not resolve: {exc}") from exc
        if not _HEADER_SAFE.fullmatch(token):
            raise ToolError("the Home Assistant token contains spaces or control characters")
        return token


def _json(response: HttpResponse) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ToolError("Home Assistant answered with something other than JSON") from exc

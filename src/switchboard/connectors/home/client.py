"""A minimal client for the Home Assistant REST API.

Endpoints used: ``GET /api/states``, ``GET /api/states/<entity_id>``, and
``POST /api/services/<domain>/<service>``, each with ``Authorization: Bearer <long-lived token>``.
Callers validate entity ids, domains, and service names before they reach a URL path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from switchboard.connectors.home.settings import HomeSettings
from switchboard.context import AppContext
from switchboard.errors import HttpError, SecretError, ToolError
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


class HomeAssistant:
    def __init__(self, ctx: AppContext, settings: HomeSettings) -> None:
        self.ctx = ctx
        self.settings = settings

    def states(self) -> list[dict[str, Any]]:
        """Every state the token may read, as Home Assistant returns them (filter before showing)."""
        data = _json(self._request("GET", "/api/states"))
        if not isinstance(data, list):
            raise ToolError("Home Assistant returned an unexpected states list")
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
                allow_http=self.settings.allow_http,
            )
        except HttpError as exc:
            raise ToolError(f"cannot reach Home Assistant: {exc}") from exc
        if not response.ok:
            raise ToolError(
                _STATUS_MESSAGES.get(response.status, f"Home Assistant answered HTTP {response.status}")
            )
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

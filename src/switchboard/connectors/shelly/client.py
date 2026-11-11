"""Fixed web remote procedure calls with bounded Digest authentication."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.request import parse_http_list

from switchboard.context import AppContext
from switchboard.errors import HttpError, SecretError, ToolError
from switchboard.httpclient import HttpResponse
from switchboard.util import canonical_json, sha256_hex

from .settings import Device

MAX_RESPONSE_BYTES = 65_536
_METHODS = frozenset({"Shelly.GetDeviceInfo", "Switch.GetStatus", "Switch.Set"})
_TOKEN = re.compile(r"[A-Za-z0-9+/_=.-]{1,256}")
_PARAMETER = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_DIRECTIVE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~:/=-]+")
_MESSAGES = {
    "transport_error": "Shelly transport failed; no automatic resend",
    "redirect_refused": "Shelly redirects are refused",
    "http_error": "Shelly returned an unsuccessful HTTP status",
    "authentication_required": "Shelly requires a configured password reference",
    "authentication_failed": "Shelly authentication was refused",
    "authentication_not_enforced": "Shelly is not enforcing the configured authentication",
    "unsupported_authentication": "Shelly requires an unsupported or malformed authentication challenge",
    "secret_unavailable": "the configured Shelly password could not be resolved safely",
    "protocol_error": "Shelly returned an invalid or mismatched response",
    "identity_mismatch": "Shelly identity, model or generation does not match the configured device",
    "rpc_rejected": "Shelly rejected the fixed operation",
}


class ShellyError(ToolError):
    """A fixed safe error, with no provider messages, addresses or credentials."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code
        self.uncertain = uncertain


@dataclass(frozen=True)
class Challenge:
    realm: str
    nonce: str
    opaque: str | None = None


def parse_challenge(header: str, expected_realm: str) -> Challenge:
    """Accept the documented Digest profile, including old and new nonce formats."""
    if (
        not 1 <= len(header) <= 2048
        or not header.isascii()
        or "\\" in header
        or any(ord(char) < 32 or ord(char) == 127 for char in header)
    ):
        raise ShellyError("unsupported_authentication")
    scheme, separator, raw = header.partition(" ")
    if not separator or scheme.lower() != "digest":
        raise ShellyError("unsupported_authentication")
    parameters: dict[str, str] = {}
    for directive in parse_http_list(raw):
        key, equals, value = directive.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not equals or not _PARAMETER.fullmatch(key) or key in parameters:
            raise ShellyError("unsupported_authentication")
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
            if '"' in value or "\\" in value:
                raise ShellyError("unsupported_authentication")
        elif not _DIRECTIVE.fullmatch(value):
            raise ShellyError("unsupported_authentication")
        parameters[key] = value
    allowed = {"realm", "nonce", "qop", "algorithm", "opaque", "stale", "charset", "userhash"}
    if (
        set(parameters) - allowed
        or parameters.get("realm") != expected_realm
        or parameters.get("algorithm", "").upper() != "SHA-256"
        or "auth" not in {part.strip() for part in parameters.get("qop", "").split(",")}
        or parameters.get("charset", "UTF-8").upper() != "UTF-8"
        or parameters.get("userhash", "false").lower() != "false"
        or parameters.get("stale", "false").lower() not in {"true", "false"}
        or not _TOKEN.fullmatch(parameters.get("nonce", ""))
        or ("opaque" in parameters and not _TOKEN.fullmatch(parameters["opaque"]))
    ):
        raise ShellyError("unsupported_authentication")
    return Challenge(expected_realm, parameters["nonce"], parameters.get("opaque"))


class ShellyClient:
    """One tool invocation's session; nonce state is never shared across invocations or persisted."""

    def __init__(self, ctx: AppContext, device: Device, timeout_seconds: int) -> None:
        self.ctx = ctx
        self.device = device
        self.timeout_seconds = timeout_seconds
        self._challenge: Challenge | None = None
        self._nonce_count = 0
        self._password: str | None = None
        self._identified = False

    def identify(self) -> None:
        self._identified = False
        result = self._rpc("Shelly.GetDeviceInfo", {"ident": False})
        if (
            result.get("id") != self.device.expected_device_id
            or result.get("model") != self.device.expected_model
            or type(result.get("gen")) is not int
            or result["gen"] != self.device.expected_generation
        ):
            raise ShellyError("identity_mismatch")
        auth = result.get("auth_en")
        if auth is not None and not isinstance(auth, bool):
            raise ShellyError("protocol_error")
        if auth is True and self.device.password is None:
            raise ShellyError("authentication_required")
        if auth is False and self.device.password is not None:
            raise ShellyError("authentication_not_enforced")
        self._identified = True

    def status(self, switch_id: int) -> dict[str, Any]:
        self._require_switch(switch_id)
        return self._rpc("Switch.GetStatus", {"id": switch_id})

    def set_output(self, switch_id: int, on: bool) -> dict[str, Any]:
        self._require_switch(switch_id)
        self.device.control(switch_id)
        if not isinstance(on, bool):
            raise ToolError("Shelly output must be an explicit boolean")
        if self.device.password is not None and self._challenge is None:
            raise ShellyError("authentication_required")
        return self._rpc("Switch.Set", {"id": switch_id, "on": on})

    def _require_switch(self, switch_id: int) -> None:
        if not self._identified:
            raise ShellyError("identity_mismatch")
        if type(switch_id) is not int or switch_id not in self.device.switch_ids:
            raise ToolError("this Shelly switch component is not exposed")

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in _METHODS:
            raise ToolError("unsupported Shelly operation")
        request_id = secrets.token_hex(8)
        payload = canonical_json(
            {"id": request_id, "src": "switchboard", "method": method, "params": params}
        ).encode("ascii")
        public = method == "Shelly.GetDeviceInfo"
        authorization = self._authorization() if self._challenge is not None and not public else None
        response = self._send(payload, authorization)
        if response.status == 401:
            if self.device.password is None:
                raise ShellyError("authentication_required")
            challenge = parse_challenge(
                response.headers.get("www-authenticate", ""), self.device.expected_device_id
            )
            if (
                authorization is not None
                and self._challenge is not None
                and challenge.nonce == self._challenge.nonce
            ):
                raise ShellyError("authentication_failed")
            self._challenge = challenge
            self._nonce_count = 0
            response = self._send(payload, self._authorization())
            if response.status == 401:
                raise ShellyError("authentication_failed")
        elif not public and self.device.password is not None and authorization is None and response.ok:
            raise ShellyError("authentication_not_enforced")
        if response.status == 403:
            raise ShellyError("authentication_failed")
        if not response.ok:
            raise ShellyError("http_error", uncertain=True)
        try:
            frame = response.json()
        except (ValueError, RecursionError):
            raise ShellyError("protocol_error", uncertain=True) from None
        if (
            not isinstance(frame, dict)
            or frame.get("id") != request_id
            or frame.get("src") != self.device.expected_device_id
            or frame.get("dst", "switchboard") != "switchboard"
            or ("result" in frame) == ("error" in frame)
        ):
            raise ShellyError("protocol_error", uncertain=True)
        if "error" in frame:
            error = frame["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int:
                raise ShellyError("protocol_error", uncertain=True)
            raise ShellyError("rpc_rejected", uncertain=True)
        result = frame["result"]
        if not isinstance(result, dict):
            raise ShellyError("protocol_error", uncertain=True)
        return result

    def _send(self, payload: bytes, authorization: str | None) -> HttpResponse:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if authorization is not None:
            headers["Authorization"] = authorization
        url = self.device.base_url + "/rpc"
        try:
            response = self.ctx.http.request(
                "POST",
                url,
                headers=headers,
                body=payload,
                timeout=self.timeout_seconds,
                max_bytes=MAX_RESPONSE_BYTES,
                allow_http=self.device.allow_http,
                follow_redirects=False,
            )
        except HttpError:
            raise ShellyError("transport_error", uncertain=True) from None
        if response.url != url or 300 <= response.status < 400:
            raise ShellyError("redirect_refused", uncertain=True)
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise ShellyError("protocol_error", uncertain=True)
        return response

    def _authorization(self) -> str:
        challenge = self._challenge
        if challenge is None or self.device.password is None:
            raise ShellyError("authentication_required")
        if self._password is None:
            try:
                password = self.ctx.secret(self.device.password)
                valid = (
                    isinstance(password, str)
                    and 1 <= len(password.encode("utf-8")) <= 1024
                    and not any(char in password for char in "\r\n\x00")
                )
            except (SecretError, UnicodeError):
                raise ShellyError("secret_unavailable") from None
            if not valid:
                raise ShellyError("secret_unavailable")
            self._password = password
        self._nonce_count += 1
        nc = f"{self._nonce_count:08x}"
        cnonce = secrets.token_hex(16)
        ha1 = sha256_hex(f"admin:{challenge.realm}:{self._password}")
        ha2 = sha256_hex("POST:/rpc")
        digest = sha256_hex(f"{ha1}:{challenge.nonce}:{nc}:{cnonce}:auth:{ha2}")
        header = (
            f'Digest username="admin", realm="{challenge.realm}", nonce="{challenge.nonce}", '
            f'uri="/rpc", algorithm=SHA-256, response="{digest}", qop=auth, nc={nc}, cnonce="{cnonce}"'
        )
        if challenge.opaque is not None:
            header += f', opaque="{challenge.opaque}"'
        return header

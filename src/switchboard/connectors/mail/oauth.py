"""OAuth 2.0 for IMAP and SMTP: sign-in flows, token refresh, token files, and SASL XOAUTH2.

Endpoints and scopes were checked against the providers' documentation on 2026-11-11:

* Microsoft personal accounts use the device authorization grant (RFC 8628) against the owner's own
  app registration, tenant ``consumers`` by default, with the Outlook IMAP and SMTP scopes plus
  ``offline_access`` for a refresh token. Both scopes belong to one resource, so a single access token
  serves IMAP and SMTP.
  https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code
  https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth
* Google uses the installed-app flow: a loopback redirect to ``http://127.0.0.1:<port>`` and PKCE with
  S256 (RFC 7636), scope ``https://mail.google.com/``.
  https://developers.google.com/identity/protocols/oauth2/native-app

Every HTTP request goes through the core client passed in as ``http`` (``ctx.http``). Tokens are kept
per account in ``<data_dir>/mail/oauth/<account>.json``: the directory is mode 700, files are written
with mode 600 through ``os.open`` (symbolic links refused) and replaced atomically.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from switchboard.config import ensure_private_dir
from switchboard.errors import SwitchboardError
from switchboard.httpclient import HttpTransport

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_MARGIN_SECONDS = 120
_MAX_TOKEN_FILE_BYTES = 64 * 1024


class OAuthError(SwitchboardError):
    """Sign-in or token refresh failed. Messages never contain tokens or codes."""


@dataclass(frozen=True)
class OAuthEndpoints:
    authorize_url: str | None
    device_url: str | None
    token_url: str
    scopes: tuple[str, ...]
    scope_on_refresh: bool


_GOOGLE_OAUTH_HOST = "https://oauth2.googleapis.com"

GOOGLE = OAuthEndpoints(
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    device_url=None,
    token_url=f"{_GOOGLE_OAUTH_HOST}/token",
    scopes=("https://mail.google.com/",),
    scope_on_refresh=False,
)


def microsoft(tenant: str) -> OAuthEndpoints:
    """Microsoft identity platform v2 endpoints; ``tenant`` is ``consumers`` for personal accounts."""
    base = f"https://login.microsoftonline.com/{urllib.parse.quote(tenant, safe='')}/oauth2/v2.0"
    return OAuthEndpoints(
        authorize_url=None,
        device_url=f"{base}/devicecode",
        token_url=f"{base}/token",
        scopes=(
            "https://outlook.office.com/IMAP.AccessAsUser.All",
            "https://outlook.office.com/SMTP.Send",
            "offline_access",
        ),
        scope_on_refresh=True,
    )


@dataclass(frozen=True)
class TokenSet:
    """Tokens for one account; ``expires_at`` is in seconds since the epoch. ``repr`` hides tokens."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: float
    scope: str = ""

    def needs_refresh(self, now: float) -> bool:
        return now >= self.expires_at - REFRESH_MARGIN_SECONDS

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_json(cls, data: Any) -> TokenSet:
        if not isinstance(data, dict):
            raise ValueError("token file must hold a JSON object")
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        expires_at = data.get("expires_at")
        scope = data.get("scope", "")
        if not isinstance(access, str) or not access:
            raise ValueError("access_token is missing")
        if refresh is not None and not isinstance(refresh, str):
            raise ValueError("refresh_token must be a string")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            raise ValueError("expires_at must be a number")
        return cls(access, refresh or None, float(expires_at), scope if isinstance(scope, str) else "")


def tokens_from_response(
    data: Mapping[str, Any], *, now: float, previous: TokenSet | None = None
) -> TokenSet:
    """Build a token set from a token endpoint response (RFC 6749 section 5.1).

    A refresh response may omit ``refresh_token``; the previous one is kept. When one is present it
    replaces the old token, which Microsoft's documentation asks clients to discard.
    """
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthError("the token response did not include an access token")
    try:
        lifetime = float(data.get("expires_in", 3600))
    except (TypeError, ValueError):
        lifetime = 3600.0
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = previous.refresh_token if previous else None
    scope = data.get("scope")
    if not isinstance(scope, str):
        scope = previous.scope if previous else ""
    return TokenSet(access, refresh, now + lifetime, scope)


def _post_form(http: HttpTransport, url: str, form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    response = http.request(
        "POST",
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body=urllib.parse.urlencode(form).encode("ascii"),
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        host = urllib.parse.urlsplit(url).hostname
        raise OAuthError(f"{host} answered HTTP {response.status} without a JSON body")
    return response.status, payload


def _refused(status: int, payload: Mapping[str, Any]) -> OAuthError:
    code = payload.get("error")
    detail = payload.get("error_description")
    text = code if isinstance(code, str) and code else f"HTTP {status}"
    if isinstance(detail, str) and detail.strip():
        text += ": " + detail.strip().splitlines()[0][:160]
    return OAuthError(f"the sign-in server refused the request ({text})")


def _ok(status: int) -> bool:
    return 200 <= status < 300


# ---- Microsoft: device authorization grant (RFC 8628) ----


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str


def request_device_code(
    http: HttpTransport, endpoints: OAuthEndpoints, client_id: str
) -> DeviceAuthorization:
    """Start the device flow (RFC 8628 section 3.1-3.2): get the code the user types into the browser."""
    if endpoints.device_url is None:
        raise OAuthError("this provider does not offer the device flow")
    status, payload = _post_form(
        http, endpoints.device_url, {"client_id": client_id, "scope": " ".join(endpoints.scopes)}
    )
    if not _ok(status):
        raise _refused(status, payload)
    device_code = payload.get("device_code")
    user_code = payload.get("user_code")
    uri = payload.get("verification_uri")
    if not (isinstance(device_code, str) and isinstance(user_code, str) and isinstance(uri, str)):
        raise OAuthError("the device authorization response was incomplete")
    message = payload.get("message")
    if not isinstance(message, str) or not message:
        message = f"To sign in, open {uri} and enter the code {user_code}."
    return DeviceAuthorization(
        device_code=device_code,
        user_code=user_code,
        verification_uri=uri,
        expires_in=_positive_int(payload.get("expires_in"), 900),
        interval=_positive_int(payload.get("interval"), 5),
        message=message,
    )


def poll_device_token(
    http: HttpTransport,
    endpoints: OAuthEndpoints,
    client_id: str,
    device: DeviceAuthorization,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> TokenSet:
    """Poll the token endpoint until the user finishes (RFC 8628 sections 3.4 and 3.5).

    Waits ``interval`` seconds before every request, adds 5 seconds after each ``slow_down``, keeps
    going on ``authorization_pending``, and stops on any other error or when the code expires.
    ``sleep`` and ``clock`` are injected so tests run instantly.
    """
    interval = float(device.interval)
    deadline = clock() + device.expires_in
    form = {"grant_type": DEVICE_GRANT, "client_id": client_id, "device_code": device.device_code}
    while True:
        sleep(interval)
        if clock() >= deadline:
            raise OAuthError("the sign-in code expired before sign-in finished; run oauth-login again")
        status, payload = _post_form(http, endpoints.token_url, form)
        if _ok(status):
            return tokens_from_response(payload, now=clock())
        error = payload.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error in ("authorization_declined", "access_denied"):
            raise OAuthError("sign-in was declined in the browser")
        if error in ("expired_token", "code_expired"):
            raise OAuthError("the sign-in code expired; run oauth-login again")
        raise _refused(status, payload)


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


# ---- Google: installed-app flow with a loopback redirect and PKCE (RFC 7636) ----


def make_code_verifier(random_bytes: bytes) -> str:
    """RFC 7636 section 4.1: base64url of 32 random octets gives a 43-character verifier."""
    return _base64url(random_bytes)


def code_challenge(verifier: str) -> str:
    """RFC 7636 section 4.2, method S256: ``BASE64URL(SHA256(ASCII(code_verifier)))``."""
    return _base64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def authorization_url(
    endpoints: OAuthEndpoints,
    client_id: str,
    redirect_uri: str,
    *,
    challenge: str,
    state: str,
    login_hint: str | None = None,
) -> str:
    if endpoints.authorize_url is None:
        raise OAuthError("this provider does not offer a browser sign-in")
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(endpoints.scopes),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if login_hint:
        query["login_hint"] = login_hint
    return f"{endpoints.authorize_url}?{urllib.parse.urlencode(query)}"


def parse_redirect(path: str, expected_state: str) -> str:
    """Return the authorization code from the loopback request after checking ``state``."""
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    error = _first(params, "error")
    if error:
        raise OAuthError(f"sign-in failed: {error[:80]}")
    state = _first(params, "state") or ""
    if not hmac.compare_digest(state.encode("utf-8"), expected_state.encode("utf-8")):
        raise OAuthError("the sign-in response does not belong to this request (state mismatch)")
    code = _first(params, "code")
    if not code:
        raise OAuthError("the sign-in response had no authorization code")
    return code


def _first(params: Mapping[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    return values[0] if values else None


def exchange_code(
    http: HttpTransport,
    endpoints: OAuthEndpoints,
    client_id: str,
    client_secret: str | None,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    now: float,
) -> TokenSet:
    form = {
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        form["client_secret"] = client_secret
    status, payload = _post_form(http, endpoints.token_url, form)
    if not _ok(status):
        raise _refused(status, payload)
    return tokens_from_response(payload, now=now)


class _LoopbackServer(HTTPServer):
    """Receives the browser's redirect on 127.0.0.1; ``result`` is the request path."""

    result: str | None = None


class _LoopbackHandler(BaseHTTPRequestHandler):
    server: _LoopbackServer

    def do_GET(self) -> None:
        query = urllib.parse.urlsplit(self.path).query
        if "code=" not in query and "error=" not in query:
            self.send_error(404)
            return
        self.server.result = self.path
        body = b"<!doctype html><title>Switchboard</title><p>Sign-in finished. You can close this tab.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Stay quiet: the request line carries the authorization code."""


def google_login(
    http: HttpTransport,
    client_id: str,
    client_secret: str | None,
    *,
    login_hint: str | None,
    open_browser: Callable[[str], object],
    announce: Callable[[str], None],
    timeout: float = 300.0,
    clock: Callable[[], float] = time.monotonic,
) -> TokenSet:
    """Run Google's installed-app flow and return fresh tokens.

    A one-shot HTTP server listens on 127.0.0.1 with a free port, the browser is sent to Google with a
    PKCE challenge and a random ``state``, and the code from the redirect is exchanged for tokens.
    """
    verifier = make_code_verifier(secrets.token_bytes(32))
    state = secrets.token_urlsafe(24)
    server = _LoopbackServer(("127.0.0.1", 0), _LoopbackHandler)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_port}"
        url = authorization_url(
            GOOGLE,
            client_id,
            redirect_uri,
            challenge=code_challenge(verifier),
            state=state,
            login_hint=login_hint,
        )
        announce(f"Sign in with your browser. If it did not open, visit:\n{url}")
        open_browser(url)
        path = _wait_for_redirect(server, timeout=timeout, clock=clock)
    finally:
        server.server_close()
    code = parse_redirect(path, state)
    return exchange_code(
        http,
        GOOGLE,
        client_id,
        client_secret,
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri,
        now=time.time(),
    )


def _wait_for_redirect(server: _LoopbackServer, *, timeout: float, clock: Callable[[], float]) -> str:
    server.timeout = 1.0
    deadline = clock() + timeout
    while server.result is None:
        if clock() >= deadline:
            raise OAuthError("timed out waiting for the browser sign-in")
        server.handle_request()
    return server.result


# ---- Refresh, storage, and SASL ----


def refresh_tokens(
    http: HttpTransport,
    endpoints: OAuthEndpoints,
    client_id: str,
    client_secret: str | None,
    tokens: TokenSet,
    *,
    now: float,
) -> TokenSet:
    """Exchange the refresh token for a new access token (RFC 6749 section 6)."""
    if not tokens.refresh_token:
        raise OAuthError("the access token expired and there is no refresh token; run oauth-login again")
    form = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": tokens.refresh_token}
    if endpoints.scope_on_refresh:
        form["scope"] = " ".join(endpoints.scopes)
    if client_secret:
        form["client_secret"] = client_secret
    status, payload = _post_form(http, endpoints.token_url, form)
    if _ok(status):
        return tokens_from_response(payload, now=now, previous=tokens)
    if payload.get("error") == "invalid_grant":
        raise OAuthError(
            "the refresh token was rejected (expired or revoked); run 'switchboard mail oauth-login' again"
        )
    raise _refused(status, payload)


class TokenStore:
    """Token files in ``<data_dir>/mail/oauth``; see the module docstring for the safety rules."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.directory = data_dir / "mail" / "oauth"

    def path(self, account: str) -> Path:
        return self.directory / f"{account}.json"

    def load(self, account: str) -> TokenSet | None:
        """The stored tokens, or None when the account has not signed in yet."""
        path = self.path(account)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise OAuthError(f"cannot open {path} (symbolic links are refused): {exc.strerror}") from exc
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise OAuthError(f"{path} must be a regular file with mode 600; run: chmod 600 {path}")
            raw = handle.read(_MAX_TOKEN_FILE_BYTES + 1)
        try:
            if len(raw) > _MAX_TOKEN_FILE_BYTES:
                raise ValueError("token file is too large")
            return TokenSet.from_json(json.loads(raw.decode("utf-8")))
        except ValueError as exc:
            raise OAuthError(f"{path} is not a valid token file; run oauth-login again") from exc

    def save(self, account: str, tokens: TokenSet) -> Path:
        """Write the tokens with mode 600 and atomically replace the previous file."""
        ensure_private_dir(self.data_dir / "mail")
        ensure_private_dir(self.directory)
        path = self.path(account)
        # A unique, exclusively created name per save, so two processes refreshing at once never share it.
        try:
            fd, name = tempfile.mkstemp(dir=self.directory, prefix=f".{path.name}.", suffix=".tmp")
        except OSError as exc:
            raise OAuthError(f"cannot write a token file in {self.directory}: {exc.strerror}") from exc
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(json.dumps(tokens.to_json(), sort_keys=True).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        return path


def xoauth2_string(user: str, access_token: str) -> str:
    """The SASL XOAUTH2 initial response before base64: ``user=<user>^Aauth=Bearer <token>^A^A``."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


class XOAuth2:
    """A SASL XOAUTH2 responder for ``imaplib.IMAP4.authenticate`` and ``smtplib.SMTP.auth``.

    The first challenge is answered with the credentials. When the server rejects a token it sends a
    second challenge carrying a JSON error, which must get an empty response (Google's XOAUTH2
    documentation); repeating the credentials instead would loop.
    """

    def __init__(self, user: str, access_token: str) -> None:
        self._payload = xoauth2_string(user, access_token)
        self._answered = False

    def __repr__(self) -> str:
        return "XOAuth2(<hidden>)"

    def _next(self) -> str:
        if self._answered:
            return ""
        self._answered = True
        return self._payload

    def imap(self, challenge: bytes) -> bytes:
        """imaplib callback: receives the decoded challenge and returns bytes for it to base64-encode."""
        return self._next().encode("utf-8")

    def smtp(self, challenge: bytes | None = None) -> str:
        """smtplib callback: first called without a challenge to produce the initial response."""
        return self._next()

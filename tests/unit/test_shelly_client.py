"""Synthetic HTTP and Digest protocol cases; no real device traffic."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.request import parse_http_list, parse_keqv_list

import pytest

from switchboard.connectors.shelly.client import (
    MAX_RESPONSE_BYTES,
    ShellyClient,
    ShellyError,
    parse_challenge,
)
from switchboard.connectors.shelly.settings import load_settings
from switchboard.context import AppContext
from switchboard.errors import SecretError, ToolError
from switchboard.httpclient import HttpResponse
from tests.conftest import AppFactory
from tests.fakes import FakeHttp, RecordedRequest
from tests.fixtures.shelly import (
    BASE,
    DEVICE_ID,
    PASSWORD,
    accepted,
    challenge,
    config,
    identity,
    methods,
    rpc_frame,
    status,
)


def client(make_app: AppFactory, *, password: bool = False, controls: bool = False) -> ShellyClient:
    app = make_app({"switchboard": {"connectors": []}})
    settings = load_settings(config(password=password, controls=controls)["connectors"]["shelly"])
    return ShellyClient(app.ctx, settings.devices[0], settings.timeout_seconds)


def auth_fields(request: RecordedRequest) -> dict[str, str]:
    header = request.headers["authorization"]
    assert header.startswith("Digest ")
    return parse_keqv_list(parse_http_list(header.removeprefix("Digest ")))


def test_unauthenticated_status_uses_only_fixed_full_frame_posts(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    device = client(make_app)
    identity(fake_http)
    status(fake_http)
    device.identify()
    assert device.status(0)["apower"] == 6.5
    assert methods(fake_http) == ["Shelly.GetDeviceInfo", "Switch.GetStatus"]
    for request in fake_http.requests:
        assert request.url == BASE + "/rpc"
        assert request.method == "POST"
        assert request.follow_redirects is False
        assert request.max_bytes == MAX_RESPONSE_BYTES
        assert request.timeout == 5
        assert "authorization" not in request.headers


@pytest.mark.parametrize("nonce", ["60dc59c6", "AAAAAABnSyntheticToken=="])
def test_old_and_new_nonce_formats_use_http_sha256_digest_not_websocket_dummy_values(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch, nonce: str
) -> None:
    monkeypatch.setenv("SB_TEST_SHELLY_PASSWORD", PASSWORD)
    device = client(make_app, password=True)
    identity(fake_http, authenticated=True)
    challenge(fake_http, nonce=nonce)
    status(fake_http)
    device.identify()
    device.status(0)
    assert "authorization" not in fake_http.requests[1].headers
    fields = auth_fields(fake_http.requests[2])
    assert fields["username"] == "admin"
    assert fields["realm"] == DEVICE_ID
    assert fields["nonce"] == nonce
    assert fields["uri"] == "/rpc"
    assert fields["algorithm"] == "SHA-256"
    assert fields["qop"] == "auth"
    assert fields["nc"] == "00000001"
    ha1 = hashlib.sha256(f"admin:{DEVICE_ID}:{PASSWORD}".encode()).hexdigest()
    ha2 = hashlib.sha256(b"POST:/rpc").hexdigest()
    expected = hashlib.sha256(f"{ha1}:{nonce}:00000001:{fields['cnonce']}:auth:{ha2}".encode()).hexdigest()
    assert fields["response"] == expected
    assert PASSWORD not in repr(fake_http.requests)
    assert all("auth" not in json.loads(request.body or b"{}") for request in fake_http.requests)
    assert fake_http.requests[1].body == fake_http.requests[2].body


def test_digest_nonce_count_increments_within_one_invocation(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_SHELLY_PASSWORD", PASSWORD)
    device = client(make_app, password=True)
    identity(fake_http, authenticated=True)
    challenge(fake_http)
    status(fake_http)
    status(fake_http)
    device.identify()
    device.status(0)
    device.status(0)
    signed = [request for request in fake_http.requests if "authorization" in request.headers]
    assert [auth_fields(request)["nc"] for request in signed] == ["00000001", "00000002"]
    assert auth_fields(signed[0])["cnonce"] != auth_fields(signed[1])["cnonce"]


def test_explicit_new_nonce_401_can_negotiate_once_without_an_unsigned_control(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_SHELLY_PASSWORD", PASSWORD)
    device = client(make_app, password=True, controls=True)
    identity(fake_http, authenticated=True)
    challenge(fake_http, nonce="old-nonce")
    status(fake_http)
    challenge(fake_http, nonce="new-nonce", stale="true")
    accepted(fake_http)
    device.identify()
    device.status(0)
    assert device.set_output(0, True) == {"was_on": False}
    writes = [
        request
        for request in fake_http.requests
        if json.loads(request.body or b"{}")["method"] == "Switch.Set"
    ]
    assert len(writes) == 2
    assert writes[0].body == writes[1].body
    assert [auth_fields(request)["nonce"] for request in writes] == ["old-nonce", "new-nonce"]
    assert [auth_fields(request)["nc"] for request in writes] == ["00000002", "00000001"]


def test_a_repeated_challenge_after_authentication_is_not_retried(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_SHELLY_PASSWORD", PASSWORD)
    device = client(make_app, password=True)
    identity(fake_http, authenticated=True)
    challenge(fake_http)
    status(fake_http)
    challenge(fake_http)
    device.identify()
    device.status(0)
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "authentication_failed"
    assert len(fake_http.requests) == 4


def test_wrong_password_is_refused_after_one_authenticated_attempt(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_SHELLY_PASSWORD", PASSWORD)
    device = client(make_app, password=True)
    identity(fake_http, authenticated=True)
    challenge(fake_http)
    challenge(fake_http, nonce="another-nonce")
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "authentication_failed"
    assert len(fake_http.requests) == 3


def test_password_resolution_is_lazy_and_sanitizes_secret_resolver_errors(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fail(_ctx: AppContext, reference: str) -> str:
        calls.append(reference)
        raise SecretError("synthetic-secret-provider-field-must-not-be-returned")

    monkeypatch.setattr(AppContext, "secret", fail)
    device = client(make_app, password=True)
    assert calls == []
    identity(fake_http, authenticated=True)
    challenge(fake_http)
    device.identify()
    assert calls == []
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "secret_unavailable"
    assert "synthetic-secret" not in str(raised.value)
    assert len(calls) == 1
    assert len(fake_http.requests) == 2


@pytest.mark.parametrize("password", ["", "a" * 1025, "invalid\npassword", "invalid\x00password", "\ud800"])
def test_resolved_passwords_are_bounded_and_invalid_values_never_reach_a_header(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch, password: str
) -> None:
    monkeypatch.setattr(AppContext, "secret", lambda _ctx, _ref: password)
    device = client(make_app, password=True)
    identity(fake_http, authenticated=True)
    challenge(fake_http)
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "secret_unavailable"
    assert not any("authorization" in request.headers for request in fake_http.requests)


def test_configured_authentication_does_not_silently_downgrade_to_an_unprotected_device(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    device = client(make_app, password=True, controls=True)
    identity(fake_http)
    with pytest.raises(ShellyError) as raised:
        device.identify()
    assert raised.value.code == "authentication_not_enforced"
    assert methods(fake_http) == ["Shelly.GetDeviceInfo"]


def test_missing_identity_auth_flag_does_not_bypass_required_status_challenge(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    device = client(make_app, password=True)
    identity(fake_http, auth_en=None)
    status(fake_http)
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "authentication_not_enforced"


def test_enabled_authentication_without_a_reference_stops_before_protected_requests(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    device = client(make_app)
    identity(fake_http, authenticated=True)
    with pytest.raises(ShellyError) as raised:
        device.identify()
    assert raised.value.code == "authentication_required"
    assert len(fake_http.requests) == 1


@pytest.mark.parametrize(
    "header",
    [
        'Basic realm="example"',
        f'Digest realm="{DEVICE_ID}", nonce="x", qop="auth", algorithm=MD5',
        f'Digest realm="{DEVICE_ID}", nonce="x", qop="auth-int", algorithm=SHA-256',
        'Digest realm="other", nonce="x", qop="auth", algorithm=SHA-256',
        f'Digest realm="{DEVICE_ID}", nonce="", qop="auth", algorithm=SHA-256',
        f'Digest realm="{DEVICE_ID}", nonce="back\\\\slash", qop="auth", algorithm=SHA-256',
        f'Digest realm="{DEVICE_ID}", nonce="x", qop="auth"',
        f'Digest realm="{DEVICE_ID}", nonce="x", nonce="y", qop="auth", algorithm=SHA-256',
        f'Digest realm="{DEVICE_ID}", nonce="x", qop="auth", algorithm=SHA-256\r\nX: bad',
        f'Digest realm="{DEVICE_ID}", nonce="x", qop="auth", algorithm=SHA-256, userhash=true',
        f'Digest realm="{DEVICE_ID}", nonce="x", qop="auth", algorithm=SHA-256, charset=LATIN1',
        f'Digest realm="{DEVICE_ID}", nonce="x", qop="auth", algorithm=SHA-256, stale=maybe',
        f'Digest realm="{DEVICE_ID}", nonce="x", qop="auth", algorithm=SHA-256, mystery=value',
        "Digest " + "x" * 2048,
        "",
    ],
)
def test_malformed_unsupported_or_realm_swapped_challenges_are_rejected(header: str) -> None:
    with pytest.raises(ShellyError) as raised:
        parse_challenge(header, DEVICE_ID)
    assert raised.value.code == "unsupported_authentication"


def test_digest_supports_auth_among_qop_choices_and_opaque_tokens() -> None:
    header = (
        f'Digest realm="{DEVICE_ID}", nonce="60dc59c6", qop="auth,auth-int", '
        'algorithm=SHA-256, opaque="abc123", charset=UTF-8'
    )
    parsed = parse_challenge(header, DEVICE_ID)
    assert parsed.nonce == "60dc59c6"
    assert parsed.opaque == "abc123"


@pytest.mark.parametrize("http_status", [301, 302, 307, 308])
def test_redirects_never_forward_credentials(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch, http_status: int
) -> None:
    monkeypatch.setenv("SB_TEST_SHELLY_PASSWORD", PASSWORD)
    device = client(make_app, password=True)
    identity(fake_http, authenticated=True)
    challenge(fake_http)
    fake_http.add(
        "POST", BASE + "/rpc", status=http_status, headers={"Location": "https://elsewhere.example.test/rpc"}
    )
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "redirect_refused"
    assert len(fake_http.requests) == 3
    assert all(not request.follow_redirects for request in fake_http.requests)
    assert all(request.url == BASE + "/rpc" for request in fake_http.requests)


@pytest.mark.parametrize("http_status", [400, 403, 404, 429, 500, 503])
def test_http_errors_do_not_surface_raw_provider_messages_or_retry(
    make_app: AppFactory, fake_http: FakeHttp, http_status: int
) -> None:
    device = client(make_app)
    identity(fake_http)
    fake_http.add("POST", BASE + "/rpc", status=http_status, body="synthetic-secret-message")
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert "synthetic-secret" not in str(raised.value)
    assert len(fake_http.requests) == 2


def test_transport_errors_are_sanitized_and_not_retried(make_app: AppFactory, fake_http: FakeHttp) -> None:
    device = client(make_app)
    identity(fake_http)
    fake_http.add_error("POST", BASE + "/rpc", "synthetic-secret-message")
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "transport_error"
    assert raised.value.uncertain is True
    assert "synthetic-secret" not in str(raised.value)
    assert len(fake_http.requests) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "wrong"},
        {"src": "unconfigured-device"},
        {"dst": "other-client"},
        {"result": []},
        {"error": {"code": -105, "message": "synthetic-secret-message"}},
    ],
)
def test_mismatched_or_ambiguous_frames_are_rejected(
    make_app: AppFactory, fake_http: FakeHttp, overrides: dict[str, Any]
) -> None:
    device = client(make_app)
    identity(fake_http)

    def reply(request: RecordedRequest) -> HttpResponse:
        return rpc_frame(request, {"id": 0, "output": False}, **overrides)

    fake_http.add_handler("POST", BASE + "/rpc", reply, repeat=False)
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "protocol_error"
    assert "synthetic-secret" not in str(raised.value)


@pytest.mark.parametrize(
    "body", [b"not-json", b'{"value":NaN}', b"[" * 1500, b"x" * (MAX_RESPONSE_BYTES + 1)]
)
def test_unreadable_nonfinite_deep_or_oversized_responses_are_rejected(
    make_app: AppFactory, fake_http: FakeHttp, body: bytes
) -> None:
    device = client(make_app)
    identity(fake_http)
    fake_http.add("POST", BASE + "/rpc", body=body)
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "protocol_error"


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "different-device"},
        {"model": "SNSW-001P16EU"},
        {"gen": 1},
        {"gen": 3},
        {"gen": True},
        {"gen": 2.0},
    ],
)
def test_device_identity_must_match_before_a_switch_can_be_used(
    make_app: AppFactory, fake_http: FakeHttp, overrides: dict[str, Any]
) -> None:
    device = client(make_app)
    identity(fake_http, **overrides)
    with pytest.raises(ShellyError) as raised:
        device.identify()
    assert raised.value.code == "identity_mismatch"
    assert len(fake_http.requests) == 1


def test_client_cannot_issue_controls_before_identification_or_authentication(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    device = client(make_app, password=True, controls=True)
    with pytest.raises(ShellyError):
        device.set_output(0, True)
    assert fake_http.requests == []
    identity(fake_http, authenticated=True)
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.set_output(0, True)
    assert raised.value.code == "authentication_required"
    assert len(fake_http.requests) == 1


def test_client_refuses_hidden_components_and_arbitrary_operations(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    device = client(make_app)
    identity(fake_http)
    device.identify()
    with pytest.raises(ToolError):
        device.status(1)
    with pytest.raises(ToolError):
        device._rpc("Shelly.FactoryReset", {})
    with pytest.raises(ToolError):
        device.set_output(0, True)
    assert methods(fake_http) == ["Shelly.GetDeviceInfo"]


def test_transport_response_url_changes_are_rejected_even_without_a_redirect_status(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    device = client(make_app)

    def moved(request: RecordedRequest) -> HttpResponse:
        response = rpc_frame(request, {})
        return HttpResponse(response.status, "https://different.example.test/rpc", body=response.body)

    fake_http.add_handler("POST", BASE + "/rpc", moved, repeat=False)
    with pytest.raises(ShellyError) as raised:
        device.identify()
    assert raised.value.code == "redirect_refused"
    assert len(fake_http.requests) == 1


def test_a_challenge_for_another_realm_never_resolves_or_sends_credentials(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_ctx: AppContext, _reference: str) -> str:
        raise AssertionError("untrusted challenge must be rejected before secret resolution")

    monkeypatch.setattr(AppContext, "secret", forbidden)
    device = client(make_app, password=True)
    identity(fake_http, authenticated=True)
    challenge(fake_http, realm='"different-device"')
    device.identify()
    with pytest.raises(ShellyError) as raised:
        device.status(0)
    assert raised.value.code == "unsupported_authentication"
    assert not any("authorization" in request.headers for request in fake_http.requests)

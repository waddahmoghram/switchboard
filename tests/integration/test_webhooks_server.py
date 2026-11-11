"""The webhook receiver over real HTTP on 127.0.0.1: only verified requests become events."""

from __future__ import annotations

import socket
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

from switchboard.app import App
from switchboard.connectors.webhooks.hooks import load_settings
from switchboard.connectors.webhooks.receiver import WebhookReceiver
from switchboard.connectors.webhooks.server import WebhookHandler, WebhookServer
from switchboard.connectors.webhooks.verify import sign

pytestmark = pytest.mark.integration

AppFactory = Callable[..., App]
SECRET = "test-secret"
# Never route test traffic through a proxy from the environment.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class Running:
    app: App
    base: str


@pytest.fixture
def running(make_app: AppFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Running]:
    monkeypatch.setenv("SB_TEST_HOOK_SECRET", SECRET)
    monkeypatch.setenv("SB_TEST_GRAPH_STATE", "test-client-state")
    hooks = {
        "ci": {"verify": "hmac-sha256", "secret": "env:SB_TEST_HOOK_SECRET"},
        "outlook": {"verify": "msgraph", "secret": "env:SB_TEST_GRAPH_STATE"},
    }
    app = make_app(
        {
            "switchboard": {"connectors": ["webhooks"]},
            "connectors": {"webhooks": {"max_body_bytes": 2048, "hooks": hooks}},
        }
    )
    receiver = WebhookReceiver(
        app.ctx, load_settings(app.ctx.connector_config("webhooks")), source="webhooks"
    )
    assert receiver.load_secrets() == []
    server = WebhookServer(("127.0.0.1", 0), receiver)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield Running(app, f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def post(url: str, body: bytes, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers or {})
    try:
        with OPENER.open(request, timeout=10) as response:
            return response.status, _lower(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, _lower(error.headers.items()), error.read()


def _lower(items: list[tuple[str, str]]) -> dict[str, str]:
    return {name.lower(): value for name, value in items}


def test_only_verified_requests_become_events(running: Running) -> None:
    hook = f"{running.base}/hooks/ci"
    body = b'{"job":"nightly","status":"passed"}'
    headers = {
        "Content-Type": "application/json",
        "X-Switchboard-Signature": sign(SECRET, int(time.time()), body),
    }
    oversized = b"x" * 4096

    signed_status, signed_headers, signed_body = post(hook, body, headers)
    replay_status = post(hook, body, headers)[0]
    unsigned_status = post(hook, body, {"Content-Type": "application/json"})[0]
    big_headers = {"X-Switchboard-Signature": sign(SECRET, int(time.time()), oversized)}
    oversized_status = post(hook, oversized, big_headers)[0]
    unknown_status = post(f"{running.base}/hooks/nope", body, headers)[0]

    assert (signed_status, signed_body) == (202, b"accepted\n")
    assert signed_headers["content-type"] == "text/plain; charset=utf-8"
    assert signed_headers["x-content-type-options"] == "nosniff"
    assert replay_status == 401
    assert unsigned_status == 401
    assert oversized_status == 413
    assert unknown_status == 404

    events = [event for event in running.app.ctx.events.recent(20) if event.type == "webhook.received"]
    assert len(events) == 1
    assert events[0].payload["json"] == {"job": "nightly", "status": "passed"}
    assert "x-switchboard-signature" not in events[0].payload["headers"]
    reasons = [
        record["reason"] for record in running.app.ctx.audit.tail(50) if record["kind"] == "webhook.rejected"
    ]
    assert reasons == ["replayed delivery", "missing X-Switchboard-Signature"]


def test_graph_validation_handshake_over_http(running: Running) -> None:
    token = "Validation: Testing client application reachability for subscription Request-Id: sb-test-0001"
    url = f"{running.base}/hooks/outlook?validationToken={urllib.parse.quote(token)}"

    status, headers, body = post(url, b"", {"Content-Type": "text/plain; charset=utf-8"})

    assert status == 200
    assert headers["content-type"].startswith("text/plain")
    assert body.decode("utf-8") == token
    assert [event for event in running.app.ctx.events.recent(20) if event.type == "webhook.received"] == []


def test_methods_other_than_post_are_refused(running: Running) -> None:
    request = urllib.request.Request(f"{running.base}/hooks/ci", method="GET")

    with pytest.raises(urllib.error.HTTPError) as caught:
        OPENER.open(request, timeout=10)

    with caught.value as error:
        assert error.code == 405
        assert error.headers["Allow"] == "POST"


def test_a_client_that_never_finishes_is_cut_off_at_the_connection_deadline(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(WebhookHandler, "connection_deadline", 0.5)
    port = int(running.base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(b"POST /hooks/ci HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # headers never end
        started = time.monotonic()
        assert client.recv(1024) == b""
        assert time.monotonic() - started < 5


def test_extra_connections_are_refused_and_each_slot_is_freed_once(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    handled: list[socket.socket] = []
    monkeypatch.setattr(
        socketserver.ThreadingMixIn,
        "process_request",
        lambda _self, request, _address: handled.append(request),
    )
    receiver = WebhookReceiver(
        running.app.ctx, load_settings(running.app.ctx.connector_config("webhooks")), source="webhooks"
    )
    server = WebhookServer(("127.0.0.1", 0), receiver, max_connections=1)
    pairs = [socket.socketpair() for _ in range(3)]
    try:
        first, second, third = (pair[0] for pair in pairs)
        server.process_request(first, ("127.0.0.1", 1))
        server.process_request(second, ("127.0.0.1", 2))
        assert handled == [first]
        assert second.fileno() == -1  # no free slot: closed at once

        server.shutdown_request(first)
        server.shutdown_request(first)  # a second close must not release the slot again
        server.process_request(third, ("127.0.0.1", 3))
        assert handled == [first, third]
        server.shutdown_request(third)
    finally:
        server.server_close()
        for pair in pairs:
            for end in pair:
                end.close()

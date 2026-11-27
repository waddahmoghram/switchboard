"""``switchboard webhooks serve`` with the real HTTP server on an ephemeral 127.0.0.1 port."""

from __future__ import annotations

import socket
import threading
import urllib.request

import pytest

from switchboard.cli import build_parser
from switchboard.connectors import webhooks
from switchboard.connectors.webhooks.server import WebhookServer
from tests.conftest import AppFactory

pytestmark = pytest.mark.integration

SECRET = "test-secret"
# Never route test traffic through a proxy from the environment.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def test_serve_turns_a_verified_delivery_into_an_event_and_closes_its_socket_when_stopped(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # HTTPServer resolves its display name before serving; this loopback test needs no external resolver.
    monkeypatch.setattr(socket, "getfqdn", lambda host: host)
    monkeypatch.setenv("SB_TEST_HOOK_SECRET", SECRET)
    hook = {"verify": "token-header", "header": "X-Backup-Token", "secret": "env:SB_TEST_HOOK_SECRET"}
    app = make_app(
        {"switchboard": {"connectors": ["webhooks"]}, "connectors": {"webhooks": {"hooks": {"backup": hook}}}}
    )
    started = threading.Event()
    built: list[WebhookServer] = []

    class ObservedServer(WebhookServer):
        """The real server, made reachable from the test and quick to stop."""

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            built.append(self)
            started.set()
            super().serve_forever(poll_interval=0.05)

    monkeypatch.setattr(webhooks, "WebhookServer", ObservedServer)
    args = build_parser([webhooks]).parse_args(["webhooks", "serve", "--port", "0"])
    results: list[int] = []

    def run_command() -> None:
        results.append(args.app_handler(args, app))

    thread = threading.Thread(target=run_command, daemon=True)
    thread.start()
    assert started.wait(timeout=10), "the command never started serving"
    [server] = built
    port = server.server_port
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/hooks/backup",
            data=b"backup finished",
            method="POST",
            headers={"X-Backup-Token": SECRET, "Content-Type": "text/plain"},
        )
        with OPENER.open(request, timeout=10) as response:
            reply = (response.status, response.read())
    finally:
        server.shutdown()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert results == [0]
    assert reply == (202, b"accepted\n")
    assert server.socket.fileno() == -1, "the listening socket was left open"
    [event] = [event for event in app.ctx.events.recent(10) if event.type == "webhook.received"]
    assert (event.source, event.payload["hook"]) == ("webhooks", "backup")
    assert f"listening on http://127.0.0.1:{port}/hooks/<name>" in capsys.readouterr().err.splitlines()

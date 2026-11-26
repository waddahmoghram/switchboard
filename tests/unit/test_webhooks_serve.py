"""``switchboard webhooks serve`` and the webhooks doctor, with the HTTP server replaced by a recorder.

No socket is opened here; ``tests/integration/test_webhooks_serve_command.py`` runs the command with the
real server on an ephemeral loopback port.
"""

from __future__ import annotations

import errno
import socket
from typing import Any

import pytest

from switchboard.app import App
from switchboard.cli import build_parser
from switchboard.connectors import webhooks
from switchboard.connectors.webhooks.receiver import WebhookReceiver
from switchboard.errors import ConfigError
from tests.conftest import AppFactory

SECRET_ENV = "SB_TEST_HOOK_SECRET"
UNSET_ENV = "SB_TEST_UNSET_HOOK_SECRET"
HOOKS = {
    "ci": {"verify": "hmac-sha256", "secret": f"env:{SECRET_ENV}"},
    "gh": {"verify": "github", "secret": f"env:{SECRET_ENV}"},
}
BANNER = [
    "listening on http://127.0.0.1:8787/hooks/<name>",
    "hooks: ci, gh; run 'switchboard run' to process events",
]
CHOSEN_PORT = 49152
EVERY_INTERFACE = "0.0.0.0"  # noqa: S104 - the exposed bind is what the warning test checks for


def webhooks_config(hooks: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "switchboard": {"connectors": ["webhooks"]},
        "connectors": {"webhooks": {"hooks": hooks, **extra}},
    }


class FakeServer:
    def __init__(self, address: tuple[str, int], receiver: WebhookReceiver, stop: BaseException) -> None:
        self.address = address
        self.receiver = receiver
        self.server_port = address[1] or CHOSEN_PORT  # port 0 asks the system to choose one
        self.stop = stop
        self.served = False
        self.closed = False

    def serve_forever(self) -> None:
        self.served = True
        raise self.stop

    def server_close(self) -> None:
        self.closed = True


class Servers:
    """Stands in for ``WebhookServer``: records every server built, or fails to bind like a busy port."""

    def __init__(self) -> None:
        self.built: list[FakeServer] = []
        self.bind_error: OSError | None = None
        self.stop: BaseException = KeyboardInterrupt()  # what Ctrl+C raises inside serve_forever

    def __call__(self, address: tuple[str, int], receiver: WebhookReceiver) -> FakeServer:
        if self.bind_error is not None:
            raise self.bind_error
        server = FakeServer(address, receiver, self.stop)
        self.built.append(server)
        return server


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> Servers:
    fake = Servers()
    monkeypatch.setattr(webhooks, "WebhookServer", fake)
    return fake


@pytest.fixture(autouse=True)
def hook_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "test-secret")
    monkeypatch.delenv(UNSET_ENV, raising=False)


def serve(app: App, *argv: str) -> int:
    args = build_parser([webhooks]).parse_args(["webhooks", "serve", *argv])
    return int(args.app_handler(args, app))


def test_serve_listens_on_loopback_until_ctrl_c_then_closes_the_server(
    make_app: AppFactory, servers: Servers, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app(webhooks_config(HOOKS))

    assert serve(app) == 0

    [server] = servers.built
    assert server.address == ("127.0.0.1", 8787)
    assert list(server.receiver.settings.hooks) == ["ci", "gh"]
    assert (server.served, server.closed) == (True, True)
    assert capsys.readouterr().err.splitlines() == BANNER


def test_the_banner_shows_the_port_the_system_chose(
    make_app: AppFactory, servers: Servers, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app(webhooks_config(HOOKS))

    assert serve(app, "--port", "0") == 0

    assert servers.built[0].address == ("127.0.0.1", 0)
    assert f"listening on http://127.0.0.1:{CHOSEN_PORT}/hooks/<name>" in capsys.readouterr().err.splitlines()


def test_the_server_is_closed_even_when_serving_fails(make_app: AppFactory, servers: Servers) -> None:
    servers.stop = RuntimeError("selector failed")
    app = make_app(webhooks_config(HOOKS))

    with pytest.raises(RuntimeError, match="selector failed"):
        serve(app)

    assert servers.built[0].closed


def test_serve_without_hooks_stops_before_binding(
    make_app: AppFactory, servers: Servers, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app(webhooks_config({}))

    assert serve(app) == 1

    assert servers.built == []
    assert capsys.readouterr().err == "no hooks configured under [connectors.webhooks.hooks]\n"


def test_every_unresolvable_secret_is_listed_and_nothing_listens(
    make_app: AppFactory, servers: Servers, capsys: pytest.CaptureFixture[str]
) -> None:
    hooks = {
        **HOOKS,
        "backup": {"verify": "token-header", "header": "X-Backup-Token", "secret": f"env:{UNSET_ENV}"},
        "outlook": {"verify": "msgraph", "secret": f"env:{UNSET_ENV}"},
    }
    app = make_app(webhooks_config(hooks))

    assert serve(app) == 1

    assert servers.built == []
    assert capsys.readouterr().err.splitlines() == [
        f"problem: hook backup: environment variable '{UNSET_ENV}' is not set",
        f"problem: hook outlook: environment variable '{UNSET_ENV}' is not set",
    ]


@pytest.mark.parametrize(
    ("argv", "error", "message"),
    [
        (
            [],
            OSError(errno.EADDRINUSE, "Address already in use"),
            "cannot listen on 127.0.0.1:8787: Address already in use",
        ),
        (
            ["--host", "no-such-host.invalid", "--port", "9000"],
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
            "cannot listen on no-such-host.invalid:9000: Name or service not known",
        ),
        ([], OSError("no usable address"), "cannot listen on 127.0.0.1:8787: no usable address"),
    ],
)
def test_an_address_that_cannot_be_bound_is_reported_without_a_traceback(
    make_app: AppFactory,
    servers: Servers,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    error: OSError,
    message: str,
) -> None:
    servers.bind_error = error
    app = make_app(webhooks_config(HOOKS))

    assert serve(app, *argv) == 1

    assert capsys.readouterr().err == f"{message}\n"


@pytest.mark.parametrize(
    ("host", "warned"), [(EVERY_INTERFACE, True), ("192.0.2.10", True), ("localhost", False)]
)
def test_hosts_beyond_loopback_are_served_with_a_warning(
    make_app: AppFactory, servers: Servers, capsys: pytest.CaptureFixture[str], host: str, warned: bool
) -> None:
    app = make_app(webhooks_config(HOOKS))

    assert serve(app, "--host", host) == 0

    assert servers.built[0].address == (host, 8787)
    lines = capsys.readouterr().err.splitlines()
    warning = (
        f"warning: listening on {host} exposes the receiver beyond this machine; "
        "prefer 127.0.0.1 behind a tunnel you control"
    )
    assert (warning in lines) is warned
    assert f"listening on http://{host}:8787/hooks/<name>" in lines


@pytest.mark.parametrize(
    "table",
    [
        {"hooks": {"Bad Name": {"verify": "github", "secret": "env:X"}}},
        {"hooks": ["ci"]},
        {"hooks": {"ci": "github"}},
        {"max_body_bytes": 10},
    ],
)
def test_doctor_reports_an_invalid_table_as_start_up_would(
    make_app: AppFactory, table: dict[str, Any]
) -> None:
    problems = webhooks.doctor(make_app({"connectors": {"webhooks": table}}).ctx)

    with pytest.raises(ConfigError) as raised:
        make_app({"switchboard": {"connectors": ["webhooks"]}, "connectors": {"webhooks": table}})
    assert problems == [str(raised.value)]

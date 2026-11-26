"""Command modules for the broker: parsers, configuration-free operator commands, and the operator client.

Operator commands run against an in-process operator interface instead of a socket. Authored, not run on the
authoring host.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from switchboard import broker, cli_broker
from switchboard.actions import ActionJournal
from switchboard.app import App
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool
from tests.conftest import AppFactory

PEER = broker.PeerCredentials(uid=1000, gid=1000, pid=4242)
ACTION_ID = "0123456789abcdef0123456789abcdef"


class LoopbackChannel:
    """Routes request lines to an in-process operator interface, as the operator socket would."""

    def __init__(self, api: broker.OperatorApi) -> None:
        self.api = api
        self.replies: list[bytes] = []
        self.closed = False

    def send(self, data: bytes) -> None:
        for frame in data.splitlines():
            if frame.strip():
                self.replies.append(broker.encode_frame(self.api.handle(json.loads(frame), PEER)))

    def end_input(self) -> None:
        return None

    def readline(self, size: int, /) -> bytes:
        return self.replies.pop(0) if self.replies else b""

    def close(self) -> None:
        self.closed = True


class ScriptedChannel:
    """Answers every request with the next scripted line, whatever was asked."""

    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines
        self.closed = False

    def send(self, data: bytes) -> None:
        return None

    def end_input(self) -> None:
        return None

    def readline(self, size: int, /) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    def close(self) -> None:
        self.closed = True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchboard")
    parser.add_argument("--config", type=Path)
    cli_broker.add_commands(parser.add_subparsers(dest="command", metavar="<command>"))
    return parser


@pytest.fixture
def sent() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def app(make_app: AppFactory, sent: list[dict[str, Any]]) -> App:
    built = make_app()

    def send(args: dict[str, Any]) -> dict[str, Any]:
        sent.append(dict(args))
        return {"sent_to": args["to"]}

    schema = {
        "type": "object",
        "properties": {"to": {"type": "string", "maxLength": 80}},
        "required": ["to"],
        "additionalProperties": False,
    }
    built.registry.add(Tool("demo_send", "Send a synthetic message.", Tier.ACT, schema, send))
    return built


@pytest.fixture
def connected(app: App, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Point every operator command at an in-process broker; returns the socket paths commands chose."""
    chosen: list[Path] = []
    api = broker.OperatorApi(broker.Gateway(app))

    def open_client(path: Path) -> broker.OperatorClient:
        chosen.append(path)
        return broker.OperatorClient(LoopbackChannel(api))

    monkeypatch.setattr(broker, "open_operator_client", open_client)
    return chosen


def _run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    assert not hasattr(args, "app_handler"), "operator commands must not build the application"
    return int(args.handler(args))


def test_parsers_register_broker_serve_and_the_operator_commands() -> None:
    parser = _parser()
    serve = parser.parse_args(
        ["broker", "serve", "--client-group", "1234", "--operator-group", "5678", "--allow-shared-account"]
    )
    assert serve.app_handler is cli_broker.cmd_broker_serve
    assert (serve.client_group, serve.operator_group, serve.allow_shared_account) == (1234, 5678, True)

    approve = parser.parse_args(
        ["operator", "--socket", "/run/x/o.sock", "approvals", "approve", "1a2b3c4d", "--yes"]
    )
    assert approve.handler is cli_broker.cmd_operator_approvals_approve
    assert approve.socket == Path("/run/x/o.sock")
    assert approve.yes is True

    resolve = parser.parse_args(
        ["operator", "actions", "resolve", ACTION_ID, "--not-applied", "--note", "checked", "--yes"]
    )
    assert resolve.handler is cli_broker.cmd_operator_actions_resolve
    assert (resolve.not_applied, resolve.applied, resolve.yes) == (True, False, True)


@pytest.mark.parametrize(
    "argv",
    [
        ["operator", "actions", "resolve", ACTION_ID, "--note", "missing a resolution"],
        ["operator", "actions", "resolve", ACTION_ID, "--applied", "--not-applied", "--note", "both"],
        ["operator", "kill-switch", "maybe"],
        ["operator", "actions", "list", "--state", "done"],
        ["operator", "approvals", "list", "--all"],
        ["broker", "serve", "--client-group", "no-such-group-for-switchboard-tests"],
    ],
)
def test_parsers_refuse_ambiguous_operator_input(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["operator", "status"],
        ["operator", "approvals", "list"],
        ["operator", "approvals", "show", "1a2b3c4d"],
        ["operator", "approvals", "deny", "1a2b3c4d"],
        ["operator", "actions", "list", "--state", "uncertain", "-n", "5"],
        ["operator", "actions", "show", ACTION_ID],
        ["operator", "kill-switch", "status"],
    ],
)
def test_operator_commands_never_load_the_application(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    assert hasattr(args, "handler")
    assert not hasattr(args, "app_handler")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["operator", "status"], True),
        (["--config", "/etc/switchboard/config.toml", "operator", "kill-switch", "on"], True),
        (["serve-mcp", "--broker", "/run/switchboard/client.sock"], True),
        (["serve-mcp", "--broker=/run/switchboard/client.sock"], True),
        (["serve-mcp"], False),
        (["broker", "serve"], False),
        (["backup", "create", "/tmp/backup"], False),
        ([], False),
        (["--config"], False),
    ],
)
def test_configuration_free_invocations(argv: list[str], expected: bool) -> None:
    assert cli_broker.configuration_free(argv) is expected


def test_serve_mcp_via_broker_only_runs_the_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def shim(path: Path, **_kwargs: Any) -> int:
        seen.append(path)
        return 0

    monkeypatch.setattr(broker, "run_shim", shim)
    assert cli_broker.serve_mcp_via_broker(Path("/run/switchboard/client.sock")) == 0
    assert seen == [Path("/run/switchboard/client.sock")]


def test_the_operator_socket_comes_from_the_flag_the_environment_or_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(broker.OPERATOR_SOCKET_ENV, raising=False)
    assert cli_broker.operator_socket(argparse.Namespace(socket=None)) == broker.DEFAULT_OPERATOR_SOCKET
    monkeypatch.setenv(broker.OPERATOR_SOCKET_ENV, "/srv/o.sock")
    assert cli_broker.operator_socket(argparse.Namespace(socket=None)) == Path("/srv/o.sock")
    assert cli_broker.operator_socket(argparse.Namespace(socket=Path("/x.sock"))) == Path("/x.sock")


def test_operator_status_and_kill_switch(
    app: App, connected: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(["operator", "--socket", "/run/t/o.sock", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert (status["tool"], status["data"]["kill_switch"]) == ("switchboard_status", False)
    assert connected == [Path("/run/t/o.sock")]
    assert _run(["operator", "kill-switch", "on"]) == 0
    assert "ON" in capsys.readouterr().out
    assert app.policy.kill_switch.exists()
    assert _run(["operator", "kill-switch", "status"]) == 0
    assert "ON" in capsys.readouterr().out
    assert _run(["operator", "kill-switch", "off"]) == 0
    assert "off" in capsys.readouterr().out
    assert not app.policy.kill_switch.exists()


def test_operator_approval_needs_a_terminal_or_an_explicit_yes(
    app: App,
    sent: list[dict[str, Any]],
    connected: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pending = app.dispatcher.call("demo_send", {"to": "a@example.invalid"}, Origin.ASSISTANT)
    assert pending.approval_id is not None
    assert _run(["operator", "approvals", "list"]) == 0
    assert pending.approval_id in capsys.readouterr().out

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert _run(["operator", "approvals", "approve", pending.approval_id]) == 1
    assert "without a terminal" in capsys.readouterr().err
    assert sent == []

    assert _run(["operator", "approvals", "approve", pending.approval_id, "--yes"]) == 0
    output = capsys.readouterr().out
    assert "a@example.invalid" in output
    assert sent == [{"to": "a@example.invalid"}]


def test_operator_deny_and_refusals_surface_as_errors(
    app: App, connected: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    pending = app.dispatcher.call("demo_send", {"to": "b@example.invalid"}, Origin.ASSISTANT)
    assert pending.approval_id is not None
    assert _run(["operator", "approvals", "deny", pending.approval_id]) == 0
    assert f"denied {pending.approval_id}" in capsys.readouterr().out
    assert _run(["operator", "approvals", "deny", pending.approval_id]) == 1
    assert "not denied" in capsys.readouterr().err
    assert _run(["operator", "approvals", "approve", pending.approval_id, "--yes"]) == 1
    assert "not pending" in capsys.readouterr().err
    assert _run(["operator", "actions", "list", "-n", "101"]) == 1
    assert "limit must be from 1 to 100" in capsys.readouterr().err
    with pytest.raises(broker.OperatorError) as refused:
        _run(["operator", "approvals", "show", "ffffffff"])
    assert refused.value.code == "invalid"


def test_operator_reconciles_an_uncertain_action_only_when_confirmed(
    app: App, connected: list[Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = ActionJournal(app.ctx.db)
    attempt = journal.begin(
        tool="demo_send",
        args={"to": "c@example.invalid"},
        origin="assistant",
        approval_id=None,
        timeout_seconds=30,
    )
    journal.finish(attempt.id, "uncertain", error="timed out")
    resolve = ["operator", "actions", "resolve", attempt.id, "--not-applied", "--note", "not in sent mail"]

    assert _run(["operator", "actions", "list", "--state", "uncertain"]) == 0
    assert attempt.id in capsys.readouterr().out
    assert _run(["operator", "actions", "show", attempt.id]) == 0
    assert json.loads(capsys.readouterr().out)["args"] == {"to": "c@example.invalid"}

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert _run(resolve) == 1
    assert "without a terminal" in capsys.readouterr().err
    record = journal.get(attempt.id)
    assert record is not None and record.state == "uncertain"

    assert _run([*resolve, "--yes"]) == 0
    output = capsys.readouterr().out
    assert "did NOT happen" in output and "reconciled_not_applied" in output
    record = journal.get(attempt.id)
    assert record is not None and record.resolution_note == "not in sent mail"

    assert _run([*resolve, "--yes"]) == 1
    assert "only uncertain actions are reconciled" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        ([], "closed"),
        ([b"not json\n"], "protocol"),
        ([b"[1, 2]\n"], "protocol"),
        ([b'{"request_id": 99, "ok": true, "result": {}}\n'], "protocol"),
        ([b'{"op": "approve", "request_id": 1, "ok": true, "result": {}}\n'], "protocol"),
        ([b'{"request_id": 1, "ok": false, "error": {"code": "forbidden", "message": "no"}}\n'], "forbidden"),
    ],
)
def test_the_operator_client_rejects_unexpected_replies(lines: list[bytes], code: str) -> None:
    client = broker.OperatorClient(ScriptedChannel(list(lines)))
    with pytest.raises(broker.OperatorError) as refused:
        client.call("status")
    assert refused.value.code == code


def test_the_operator_client_sends_flat_fields_and_returns_the_result() -> None:
    class Recording(ScriptedChannel):
        def __init__(self) -> None:
            super().__init__(
                [b'{"ok": true, "op": "approval", "request_id": 1, "result": {"id": "1a2b3c4d"}}\n']
            )
            self.sent: list[bytes] = []

        def send(self, data: bytes) -> None:
            self.sent.append(data)

    channel = Recording()
    with broker.OperatorClient(channel) as client:
        assert client.call("approval", id="1a2b3c4d") == {"id": "1a2b3c4d"}
    assert [json.loads(line) for line in channel.sent] == [
        {"op": "approval", "request_id": 1, "id": "1a2b3c4d"}
    ]
    assert channel.closed


def test_broker_serve_refuses_shared_accounts_unless_overridden(
    make_app: AppFactory,
    socket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    table = {
        "client_socket": str(socket_dir / "c.sock"),
        "operator_socket": str(socket_dir / "o.sock"),
        "client_uids": [123456],
        "operator_uids": [123456],
    }
    shared = make_app({"broker": table})
    served: list[broker.BrokerSettings] = []

    def serve(_app: App, settings: broker.BrokerSettings) -> int:
        served.append(settings)
        return 0

    monkeypatch.setattr(broker, "serve", serve)
    refuse = argparse.Namespace(client_group=None, operator_group=None, allow_shared_account=False)
    assert cli_broker.cmd_broker_serve(refuse, shared) == 1
    assert "refusing" in capsys.readouterr().err
    assert served == []

    allow = argparse.Namespace(client_group=None, operator_group=None, allow_shared_account=True)
    assert cli_broker.cmd_broker_serve(allow, shared) == 0
    assert "warning" in capsys.readouterr().err
    assert served[0].client_uids == frozenset({123456})

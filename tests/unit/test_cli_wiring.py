"""``switchboard.cli`` wiring: lifecycle and broker commands, and ``serve-mcp`` with and without a broker.

Operator commands and the broker shim must never build the application or read the configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from switchboard import cli, cli_broker, cli_data, mcp_server


def test_the_main_parser_registers_lifecycle_broker_and_operator_commands() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["backup", "verify", "/srv/backup"]).handler is cli_data.cmd_backup_verify
    assert parser.parse_args(["data", "purge", "--dry-run"]).app_handler is cli_data.cmd_data_purge
    assert parser.parse_args(["broker", "serve"]).app_handler is cli_broker.cmd_broker_serve
    status = parser.parse_args(["operator", "status"])
    assert status.handler is cli_broker.cmd_operator_status
    assert not hasattr(status, "app_handler")
    show = parser.parse_args(["operator", "approvals", "show", "0123abcd"])
    assert show.handler is cli_broker.cmd_operator_approvals_show
    assert not hasattr(show, "app_handler")


def test_serve_mcp_never_needs_the_application_handler() -> None:
    parser = cli.build_parser()
    direct = parser.parse_args(["serve-mcp"])
    relayed = parser.parse_args(["serve-mcp", "--broker", "/run/switchboard/client.sock"])
    assert direct.handler is cli.cmd_serve_mcp and direct.broker is None
    assert relayed.handler is cli.cmd_serve_mcp
    assert relayed.broker == Path("/run/switchboard/client.sock")
    assert not hasattr(direct, "app_handler") and not hasattr(relayed, "app_handler")


def test_serve_mcp_with_a_broker_relays_without_loading_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relayed: list[Path] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the broker shim must not read configuration or build the application")

    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(cli, "build_app", forbidden)
    monkeypatch.setattr(cli, "_connector_cli_modules", forbidden)

    def relay(socket: Path) -> int:
        relayed.append(socket)
        return 0

    monkeypatch.setattr(cli_broker, "serve_mcp_via_broker", relay)

    assert cli.main(["serve-mcp", "--broker", "/run/switchboard/client.sock"]) == 0
    assert relayed == [Path("/run/switchboard/client.sock")]


def test_operator_commands_skip_connector_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("operator commands must not read the configuration")

    monkeypatch.setattr(cli, "_connector_cli_modules", forbidden)
    monkeypatch.setattr(cli, "build_app", forbidden)
    monkeypatch.setattr(cli_broker, "cmd_operator_status", lambda _args: 0)

    assert cli.main(["operator", "status"]) == 0


def test_serve_mcp_without_a_broker_builds_the_application(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[object] = []
    app = object()

    def build(_args: Any) -> object:
        built.append(app)
        return app

    monkeypatch.setattr(cli, "_app", build)
    monkeypatch.setattr(mcp_server, "run", lambda given: 0 if given is app else 1)

    assert cli.cmd_serve_mcp(cli.build_parser().parse_args(["serve-mcp"])) == 0
    assert built == [app]

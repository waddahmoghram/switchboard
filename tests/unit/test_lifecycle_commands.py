"""Command modules for the data lifecycle: parsers and handlers, dispatched the way ``switchboard.cli.main``
dispatches them. Authored, not run on the authoring host.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from switchboard import cli_broker, cli_data, lifecycle
from switchboard.app import build_app
from switchboard.config import load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchboard")
    parser.add_argument("--config", type=Path)
    cli_data.add_commands(parser.add_subparsers(dest="command", metavar="<command>"))
    return parser


def _run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if hasattr(args, "app_handler"):
        return int(args.app_handler(args, build_app(load_config(args.config))))
    return int(args.handler(args))


def _config(tmp_path: Path, name: str, body: str = "") -> Path:
    path = tmp_path / f"{name}.toml"
    data_dir = (tmp_path / f"{name}-data").as_posix()
    path.write_text(f'[switchboard]\nconnectors = []\ndata_dir = "{data_dir}"\n{body}', encoding="utf-8")
    path.chmod(0o600)
    return path


def test_parsers_register_the_lifecycle_commands_with_the_right_handler_kinds() -> None:
    parser = _parser()
    create = parser.parse_args(["backup", "create", "/srv/backups/today"])
    assert create.app_handler is cli_data.cmd_backup_create
    verify = parser.parse_args(["backup", "verify", "/srv/backups/today", "--expect-audit-head", "a" * 64])
    assert verify.handler is cli_data.cmd_backup_verify
    assert not hasattr(verify, "app_handler")
    restore = parser.parse_args(["backup", "restore", "/srv/backups/today", "--target", "/srv/new"])
    assert restore.handler is cli_data.cmd_backup_restore
    assert not hasattr(restore, "app_handler")
    assert parser.parse_args(["data", "export", "/srv/export.json"]).app_handler is cli_data.cmd_data_export
    purge = parser.parse_args(["data", "purge", "--dry-run"])
    assert purge.app_handler is cli_data.cmd_data_purge and purge.dry_run is True
    assert parser.parse_args(["audit-archive", "rotate"]).app_handler is cli_data.cmd_audit_archive_rotate
    assert parser.parse_args(["audit-archive", "verify"]).app_handler is cli_data.cmd_audit_archive_verify


@pytest.mark.parametrize("argv", [["backup"], ["data"], ["audit-archive"], ["backup", "create"]])
def test_incomplete_commands_are_usage_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(argv)


def test_new_top_level_names_leave_the_existing_audit_group_alone() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("audit")
    cli_data.add_commands(sub)
    cli_broker.add_commands(sub)
    assert set(sub.choices) == {"audit", "backup", "data", "audit-archive", "broker", "operator"}


def test_backup_verify_and_restore_through_the_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _config(tmp_path, "source")
    app = build_app(load_config(source))
    app.ctx.audit.append("test.event", value=1)
    backup = tmp_path / "backup"

    assert _run(["--config", str(source), "backup", "create", str(backup)]) == 0
    created = capsys.readouterr().out
    assert "audit head" in created and "manifest sha256" in created

    assert _run(["backup", "verify", str(backup)]) == 0
    assert "ok:" in capsys.readouterr().out

    target = _config(tmp_path, "target")
    assert _run(["--config", str(target), "backup", "restore", str(backup)]) == 0
    assert "kill switch is ON" in capsys.readouterr().out
    restored = build_app(load_config(target))
    assert restored.policy.kill_switch.exists()
    assert restored.ctx.audit.verify().ok

    with pytest.raises(lifecycle.LifecycleError, match="not empty"):
        _run(["--config", str(source), "backup", "restore", str(backup)])


def test_backup_verify_fails_loudly_for_a_damaged_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _config(tmp_path, "source")
    backup = tmp_path / "backup"
    assert _run(["--config", str(source), "backup", "create", str(backup)]) == 0
    (backup / "manifest.json").write_text("{}", encoding="utf-8")
    capsys.readouterr()

    assert _run(["backup", "verify", str(backup)]) == 1
    assert "BROKEN" in capsys.readouterr().err


def test_export_purge_and_audit_archive_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path, "live", "\n[retention]\ntool_calls_days = 7\n")
    build_app(load_config(config)).ctx.audit.append("test.event", value=1)
    export = tmp_path / "export.json"

    assert _run(["--config", str(config), "data", "export", str(export)]) == 0
    assert export.stat().st_mode & 0o777 == 0o600
    assert json.loads(export.read_text(encoding="ascii"))["format"] == "switchboard-export"

    assert _run(["--config", str(config), "data", "purge", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "dry run: nothing was removed" in output
    assert "tool_calls: would remove 0" in output

    assert _run(["--config", str(config), "audit-archive", "rotate"]) == 0
    assert "archived" in capsys.readouterr().out
    assert _run(["--config", str(config), "audit-archive", "verify"]) == 0
    assert "2 segment(s)" in capsys.readouterr().out

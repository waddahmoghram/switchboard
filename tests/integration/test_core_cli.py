"""End-to-end: the command-line interface against a temporary configuration (in process)."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from switchboard.app import build_app
from switchboard.cli import main
from switchboard.config import load_config
from tests.conftest import AppFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    path = tmp_path / "config" / "config.toml"
    assert main(["--config", str(path), "init"]) == 0
    return path


def test_init_writes_a_private_configuration_and_refuses_to_overwrite(config: Path) -> None:
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o077 == 0
    assert main(["--config", str(config), "init"]) == 1


def test_doctor_tools_call_and_audit(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(config), "doctor"]) == 0
    assert main(["--config", str(config), "tools"]) == 0
    listing = capsys.readouterr().out
    assert "switchboard_status" in listing
    assert "approvals_status" in listing

    assert main(["--config", str(config), "call", "switchboard_status"]) == 0
    assert main(["--config", str(config), "audit", "verify"]) == 0
    assert "ok:" in capsys.readouterr().out


def test_approvals_kill_switch_and_runner(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(config), "approvals", "list"]) == 0
    assert "nothing is waiting" in capsys.readouterr().out
    assert main(["--config", str(config), "kill-switch", "on"]) == 0
    assert "ON" in capsys.readouterr().out
    assert main(["--config", str(config), "kill-switch", "off"]) == 0
    assert "off" in capsys.readouterr().out
    assert main(["--config", str(config), "run", "--once"]) == 0
    assert "processed" in capsys.readouterr().out


def test_call_rejects_arguments_that_are_not_a_json_object(config: Path) -> None:
    assert main(["--config", str(config), "call", "switchboard_status", "--args", "[1]"]) == 2
    assert main(["--config", str(config), "call", "switchboard_status", "--args", "{bad"]) == 2


def test_only_one_runner_processes_events_at_a_time(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import fcntl
    import os

    data_dir = tmp_path / "xdg-data" / "switchboard"
    fd = os.open(data_dir / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main(["--config", str(config), "run", "--once"]) == 1
        assert "already processing" in capsys.readouterr().err
    finally:
        os.close(fd)
    assert main(["--config", str(config), "run", "--once"]) == 0


def test_failed_events_are_inspectable_without_showing_their_bodies(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = build_app(load_config(config))
    event_id = app.ctx.events.emit("test", "demo.failure", {"private": "do not show this"})
    assert event_id is not None
    app.ctx.events.mark(event_id, "subscriber: RuntimeError")
    capsys.readouterr()

    assert main(["--config", str(config), "events", "--failed", "-n", "5"]) == 0

    output = capsys.readouterr().out
    assert json.loads(output)["error"] == "subscriber: RuntimeError"
    assert "do not show this" not in output


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_call_does_not_accept_non_finite_json(config: Path, literal: str) -> None:
    assert (
        main(["--config", str(config), "call", "switchboard_status", "--args", f'{{"value":{literal}}}']) == 2
    )


def test_init_does_not_restrict_an_existing_parent_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "existing-workspace"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = parent / "switchboard.toml"

    assert main(["--config", str(path), "init"]) == 0

    assert parent.stat().st_mode & 0o777 == 0o755
    assert path.stat().st_mode & 0o777 == 0o600


def test_force_init_replaces_the_file_with_private_permissions(config: Path) -> None:
    config.chmod(0o644)
    assert main(["--config", str(config), "init", "--force"]) == 0
    assert config.stat().st_mode & 0o777 == 0o600
    assert not list(config.parent.glob(".config.toml.*.tmp"))


def test_force_init_can_replace_a_configuration_with_invalid_encoding(config: Path) -> None:
    config.write_bytes(b"\xff\xfe not valid configuration text")
    assert main(["--config", str(config), "init", "--force"]) == 0
    assert load_config(config).enabled_connectors() == []
    assert config.stat().st_mode & 0o777 == 0o600


def test_failed_force_init_keeps_the_existing_configuration(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config.read_bytes()

    def fail(_source: Path, _destination: Path) -> None:
        raise OSError(errno.ENOSPC, "simulated full filesystem")

    monkeypatch.setattr(os, "replace", fail)

    assert main(["--config", str(config), "init", "--force"]) == 1
    assert config.read_bytes() == original
    assert not list(config.parent.glob(".config.toml.*.tmp"))


def test_init_refuses_symbolic_links_without_touching_the_target(config: Path, tmp_path: Path) -> None:
    link = tmp_path / "linked.toml"
    link.symlink_to(config)
    original = config.read_bytes()
    assert main(["--config", str(link), "init", "--force"]) == 1
    assert config.read_bytes() == original
    assert link.is_symlink()


def test_init_rejects_an_unsafe_parent_without_changing_its_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    path = parent / "config.toml"
    assert main(["--config", str(path), "init"]) == 1
    assert parent.stat().st_mode & 0o777 == 0o777
    assert not path.exists()


def test_a_concurrent_init_cannot_be_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config" / "config.toml"

    def competing_init(_source: Path, destination: Path) -> None:
        destination.write_text("# another initializer won\n", encoding="utf-8")
        raise FileExistsError(errno.EEXIST, "simulated concurrent init")

    monkeypatch.setattr(os, "link", competing_init)

    assert main(["--config", str(path), "init"]) == 1
    assert path.read_text(encoding="utf-8") == "# another initializer won\n"
    assert not list(path.parent.glob(".config.toml.*.tmp"))


def test_run_once_reports_failed_jobs_instead_of_success(
    make_app: AppFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from switchboard.cli import cmd_run
    from switchboard.errors import ToolError

    app = make_app()

    def fail() -> None:
        raise ToolError("synthetic polling failure")

    app.ctx.scheduler.add("example.poll", 30, fail)
    assert cmd_run(argparse.Namespace(once=True), app) == 1
    assert "failed jobs: example.poll" in capsys.readouterr().err

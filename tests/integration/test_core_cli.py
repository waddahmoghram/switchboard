"""End-to-end: the command-line interface against a temporary configuration (in process)."""

from __future__ import annotations

from pathlib import Path

import pytest

from switchboard.cli import main

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

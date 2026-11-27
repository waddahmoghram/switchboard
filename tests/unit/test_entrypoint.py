"""``python -m switchboard`` dispatches to the command line, run in-process so coverage records the module."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from switchboard import __version__


@pytest.fixture
def missing_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point configuration discovery at an empty temporary home, so no real configuration is ever read."""
    path = tmp_path / "config" / "switchboard.toml"
    monkeypatch.setenv("SWITCHBOARD_CONFIG", str(path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return path


def run_module(monkeypatch: pytest.MonkeyPatch, *args: str) -> object:
    """Run ``python -m switchboard *args`` in this process and return its exit code."""
    monkeypatch.setattr("sys.argv", ["switchboard", *args])
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("switchboard", run_name="__main__")
    return exited.value.code


def test_version_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing_config: Path
) -> None:
    assert run_module(monkeypatch, "--version") == 0
    assert capsys.readouterr().out == f"switchboard {__version__}\n"


def test_no_command_prints_usage_and_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing_config: Path
) -> None:
    assert run_module(monkeypatch) == 2
    assert capsys.readouterr().out.startswith("usage: switchboard ")


def test_a_configuration_error_becomes_exit_one_with_a_single_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing_config: Path
) -> None:
    assert run_module(monkeypatch, "tools") == 1
    captured = capsys.readouterr()
    expected = f"error: configuration not found at {missing_config}; run 'switchboard init' first\n"
    assert captured.out == ""
    assert captured.err == expected
    assert not missing_config.exists()

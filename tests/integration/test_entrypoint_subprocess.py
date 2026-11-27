"""``python -m switchboard`` as a real process: the module entry point and its exit codes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parents[2] / "src"


def run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the module with configuration discovery confined to ``tmp_path``."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")])),
        "SWITCHBOARD_CONFIG": str(tmp_path / "config" / "switchboard.toml"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    return subprocess.run(
        [sys.executable, "-m", "switchboard", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )


def test_help_lists_the_commands_and_exits_zero(tmp_path: Path) -> None:
    completed = run(tmp_path, "--help")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("usage: switchboard ")
    assert {"init", "doctor", "tools", "serve-mcp"} <= set(completed.stdout.split())
    assert completed.stderr == ""


def test_missing_configuration_exits_one_without_a_traceback(tmp_path: Path) -> None:
    completed = run(tmp_path, "tools")

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.startswith("error: configuration not found at ")
    assert "Traceback" not in completed.stderr
    assert not (tmp_path / "data").exists()

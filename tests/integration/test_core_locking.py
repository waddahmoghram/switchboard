"""Without ``fcntl``, Switchboard refuses to load instead of writing the audit log without a lock."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parents[2] / "src"
REFUSAL = "Switchboard needs POSIX file locking (the fcntl module)"


def _python_without_fcntl(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")])),
    }
    env.pop("SWITCHBOARD_CONFIG", None)
    return subprocess.run(
        [sys.executable, "-c", f"import sys\nsys.modules['fcntl'] = None\n{code}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    "module", ["switchboard.audit", "switchboard.lifecycle", "switchboard.cli", "switchboard.broker"]
)
def test_modules_that_write_state_refuse_to_load_without_fcntl(tmp_path: Path, module: str) -> None:
    completed = _python_without_fcntl(f"import {module}", tmp_path)

    assert completed.returncode != 0
    assert REFUSAL in completed.stderr


def test_a_command_fails_before_it_writes_anything(tmp_path: Path) -> None:
    data = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(f'[switchboard]\nconnectors = []\ndata_dir = "{data.as_posix()}"\n', encoding="utf-8")
    config.chmod(0o600)

    command = ["--config", str(config), "kill-switch", "on"]
    completed = _python_without_fcntl(
        f"from switchboard.cli import main\nraise SystemExit(main({command!r}))", tmp_path
    )

    assert completed.returncode != 0
    assert REFUSAL in completed.stderr
    assert not data.exists()

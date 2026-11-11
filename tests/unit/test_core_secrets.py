"""Secret references: env:, file:, and cmd:, with their safety checks."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from switchboard import secrets
from switchboard.errors import SecretError


def test_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCHBOARD_TEST_SECRET", "value\n")
    assert secrets.resolve("env:SWITCHBOARD_TEST_SECRET") == "value"
    monkeypatch.delenv("SWITCHBOARD_TEST_SECRET")
    with pytest.raises(SecretError):
        secrets.resolve("env:SWITCHBOARD_TEST_SECRET")


def test_empty_values_are_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCHBOARD_EMPTY", "")
    with pytest.raises(SecretError, match="empty"):
        secrets.resolve("env:SWITCHBOARD_EMPTY")


def test_private_files(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text("s3cret\n", encoding="utf-8")
    path.chmod(0o600)
    assert secrets.resolve(f"file:{path}") == "s3cret"


def test_files_readable_by_others_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(SecretError, match="chmod 600"):
        secrets.resolve(f"file:{path}")


def test_symbolic_links_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SecretError, match="symbolic links"):
        secrets.resolve(f"file:{link}")


def test_commands_run_without_a_shell() -> None:
    python = shlex.quote(sys.executable)
    assert secrets.resolve(f"cmd:{python} -c \"print('from-cmd')\"") == "from-cmd"
    with pytest.raises(SecretError, match="exited"):
        secrets.resolve(f'cmd:{python} -c "import sys; sys.exit(3)"')
    with pytest.raises(SecretError, match="not found"):
        secrets.resolve("cmd:/nonexistent/switchboard-secret-helper")


@pytest.mark.parametrize("reference", ["plain-text", "vault:x", "", "cmd:"])
def test_malformed_references(reference: str) -> None:
    with pytest.raises(SecretError):
        secrets.resolve(reference)


def test_is_reference() -> None:
    assert secrets.is_reference("env:X")
    assert secrets.is_reference("cmd:pass show x")
    assert not secrets.is_reference("hunter2")
    assert not secrets.is_reference(42)


def test_commands_get_no_standard_input() -> None:
    python = shlex.quote(sys.executable)
    assert secrets.resolve(f'cmd:{python} -c "import sys; print(repr(sys.stdin.read()))"') == "''"

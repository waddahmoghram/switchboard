"""Secret references: env:, file:, and cmd:, with their safety checks."""

from __future__ import annotations

import json
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
    assert secrets.is_reference("cmd:/usr/bin/pass show x")
    assert not secrets.is_reference("hunter2")
    assert not secrets.is_reference(42)


def test_commands_get_no_standard_input() -> None:
    python = shlex.quote(sys.executable)
    assert secrets.resolve(f'cmd:{python} -c "import sys; print(repr(sys.stdin.read()))"') == "''"


def _helper(folder: Path, marker: Path) -> Path:
    """An executable that records that it ran and prints a value."""
    script = folder / "switchboard-test-helper"
    script.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\necho from-helper\n", encoding="utf-8")
    script.chmod(0o700)
    return script


@pytest.mark.parametrize("program", ["switchboard-test-helper", "./switchboard-test-helper"])
def test_commands_must_name_their_program_by_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str
) -> None:
    marker = tmp_path / "ran"
    _helper(tmp_path, marker)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SecretError, match="absolute path") as caught:
        secrets.resolve(f"cmd:{program} --entry mail/app")
    assert "mail/app" not in str(caught.value)
    assert not marker.exists()


def test_a_bare_program_name_error_says_how_to_find_its_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(SecretError, match="command -v pass"):
        secrets.resolve("cmd:pass show mail/app")


def test_a_program_path_may_start_from_the_home_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran"
    _helper(tmp_path, marker)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert secrets.resolve("cmd:~/switchboard-test-helper") == "from-helper"
    assert marker.exists()


def test_commands_get_only_allowlisted_and_named_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCHBOARD_TEST_TOKEN", "must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("GNUPGHOME", "/keys")
    monkeypatch.setenv("PASSWORD_STORE_DIR", "/store")
    monkeypatch.setenv("OP_SESSION_personal", "session")
    monkeypatch.setenv("LC_TIME", "C")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("BW_SESSION", "named")
    monkeypatch.setenv("VAULT_ADDR", "named")
    monkeypatch.setenv("SWITCHBOARD_SECRET_ENV", "BW_SESSION, VAULT_ADDR")
    python = shlex.quote(sys.executable)
    names = json.loads(
        secrets.resolve(f'cmd:{python} -c "import json, os; print(json.dumps(sorted(os.environ)))"')
    )
    assert {"SWITCHBOARD_TEST_TOKEN", "AWS_SECRET_ACCESS_KEY", "PYTEST_CURRENT_TEST"}.isdisjoint(names)
    expected = {"GNUPGHOME", "PASSWORD_STORE_DIR", "OP_SESSION_personal", "LC_TIME", "XDG_RUNTIME_DIR"}
    assert expected | {"BW_SESSION", "VAULT_ADDR"} <= set(names)


@pytest.mark.parametrize("listing", ["BW_SESSION=hunter2", "BW-SESSION", "1PASSWORD"])
def test_extra_variable_names_must_be_names(monkeypatch: pytest.MonkeyPatch, listing: str) -> None:
    monkeypatch.setenv("SWITCHBOARD_SECRET_ENV", listing)
    python = shlex.quote(sys.executable)
    with pytest.raises(SecretError, match="SWITCHBOARD_SECRET_ENV") as caught:
        secrets.resolve(f"cmd:{python} -c \"print('x')\"")
    assert listing not in str(caught.value)

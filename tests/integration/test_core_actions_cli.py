"""The operator's ``switchboard actions`` commands against a temporary configuration (in process)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import cast

import pytest

from switchboard.actions import ActionJournal, Finish
from switchboard.app import build_app
from switchboard.cli import main
from switchboard.config import load_config

pytestmark = pytest.mark.integration


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    path = tmp_path / "config" / "config.toml"
    assert main(["--config", str(path), "init"]) == 0
    return path


def record(config: Path, state: str, *, text: str = "hello") -> str:
    """Journal one attempt as another process would; no tool or network is involved."""
    journal = ActionJournal(build_app(load_config(config)).ctx.db, owner="ended-process")
    action = journal.begin(
        tool="demo_send",
        args={"to": "pat@example.com", "text": text},
        origin="rule",
        approval_id=None,
        timeout_seconds=60,
    )
    if state != "in_flight":
        error = "connection reset after sending" if state == "uncertain" else None
        journal.finish(action.id, cast(Finish, state), error=error)
    return action.id


def state_of(config: Path, action_id: str) -> str:
    found = build_app(load_config(config)).journal.get(action_id)
    assert found is not None
    return found.state


def test_list_and_show_report_recorded_actions(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = ["--config", str(config), "actions"]
    assert main([*base, "list"]) == 0
    assert "no recorded actions" in capsys.readouterr().out
    uncertain = record(config, "uncertain")
    done = record(config, "succeeded", text="later")

    assert main([*base, "list"]) == 0
    listing = capsys.readouterr().out
    assert uncertain in listing and done in listing and "demo_send" in listing
    assert main([*base, "list", "--state", "uncertain", "-n", "5"]) == 0
    filtered = capsys.readouterr().out
    assert uncertain in filtered and done not in filtered
    assert "connection reset after sending" in filtered
    assert main([*base, "list", "-n", "0"]) == 2
    capsys.readouterr()

    assert main([*base, "show", uncertain]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["args"] == {"to": "pat@example.com", "text": "hello"}
    assert (shown["state"], shown["origin"]) == ("uncertain", "rule")
    assert main([*base, "show", "0" * 32]) == 1
    assert "no action" in capsys.readouterr().err


def test_resolve_needs_a_terminal_or_an_explicit_yes(
    config: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    action_id = record(config, "uncertain")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    args = [
        "--config",
        str(config),
        "actions",
        "resolve",
        action_id,
        "--applied",
        "--note",
        "Saw it in the chat",
    ]

    assert main(args) == 1
    assert "refusing to reconcile without a terminal" in capsys.readouterr().err
    assert state_of(config, action_id) == "uncertain"

    assert main([*args, "--yes"]) == 0
    assert "reconciled_applied" in capsys.readouterr().out
    assert state_of(config, action_id) == "reconciled_applied"
    audit = build_app(load_config(config)).ctx.audit
    [entry] = [item for item in audit.records() if item["kind"] == "action.reconciled"]
    assert (entry["action"], entry["state"], entry["note"]) == (
        action_id,
        "reconciled_applied",
        "Saw it in the chat",
    )
    assert audit.verify().ok


def test_resolve_asks_for_confirmation_at_a_terminal(
    config: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    action_id = record(config, "uncertain")
    monkeypatch.setattr(sys, "stdin", Terminal())
    answers = iter(["n", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    args = [
        "--config",
        str(config),
        "actions",
        "resolve",
        action_id,
        "--not-applied",
        "--note",
        "Outbox empty",
    ]

    assert main(args) == 1
    assert "nothing recorded" in capsys.readouterr().out
    assert state_of(config, action_id) == "uncertain"

    assert main(args) == 0
    shown = capsys.readouterr().out
    assert "did NOT happen" in shown and '"text": "hello"' in shown
    assert "connection reset after sending" in shown
    assert state_of(config, action_id) == "reconciled_not_applied"


@pytest.mark.parametrize("prior", ["in_flight", "succeeded", "failed"])
def test_resolve_refuses_actions_that_are_not_uncertain(
    config: Path, capsys: pytest.CaptureFixture[str], prior: str
) -> None:
    action_id = record(config, prior)
    args = [
        "--config",
        str(config),
        "actions",
        "resolve",
        action_id,
        "--applied",
        "--note",
        "checked",
        "--yes",
    ]
    assert main(args) == 1
    assert f"is {prior}" in capsys.readouterr().err
    assert state_of(config, action_id) == prior


def test_resolve_validates_the_note_the_verdict_and_the_identifier(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    action_id = record(config, "uncertain")
    base = ["--config", str(config), "actions", "resolve"]
    assert main([*base, action_id, "--applied", "--note", "   ", "--yes"]) == 2
    assert "1 to 500 printable" in capsys.readouterr().err
    for invalid in (
        [action_id, "--applied", "--not-applied", "--note", "checked", "--yes"],
        [action_id, "--note", "checked", "--yes"],
        [action_id, "--applied", "--yes"],
    ):
        with pytest.raises(SystemExit) as stopped:
            main([*base, *invalid])
        assert stopped.value.code == 2
    assert main([*base, "0" * 32, "--applied", "--note", "checked", "--yes"]) == 1
    assert state_of(config, action_id) == "uncertain"


def test_doctor_counts_open_actions_without_failing(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record(config, "uncertain")
    record(config, "in_flight", text="still going")
    capsys.readouterr()
    assert main(["--config", str(config), "doctor"]) == 0
    output = capsys.readouterr().out
    assert "actions: 1 uncertain, 1 in flight" in output
    assert "switchboard actions list --state uncertain" in output

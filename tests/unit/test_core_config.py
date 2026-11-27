"""Configuration and storage fail closed before any connector runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from switchboard.config import config_from_dict, load_config
from switchboard.db import Database
from switchboard.errors import ConfigError


@pytest.mark.parametrize("section", ["switchboard", "connectors", "policy", "mcp"])
@pytest.mark.parametrize("value", ["not a table", [], False])
def test_core_sections_must_be_tables(tmp_path: Path, section: str, value: Any) -> None:
    with pytest.raises(ConfigError, match="must be a table"):
        config_from_dict({section: value}, tmp_path)


@pytest.mark.parametrize("value", ["mail_*", {"mail_*": True}, [1], False])
def test_invalid_exposure_lists_cannot_expose_all_tools(tmp_path: Path, value: Any) -> None:
    with pytest.raises(ConfigError, match="expose must be a list"):
        config_from_dict({"mcp": {"expose": value}}, tmp_path)


def test_absent_and_empty_exposure_lists_have_distinct_meanings(tmp_path: Path) -> None:
    assert config_from_dict({}, tmp_path).mcp_exposure() is None
    assert config_from_dict({"mcp": {"expose": []}}, tmp_path).mcp_exposure() == []
    assert config_from_dict({"mcp": {"expose": ["mail_*"]}}, tmp_path).mcp_exposure() == ["mail_*"]


def test_malformed_nested_connector_settings_are_not_silently_empty(tmp_path: Path) -> None:
    config = config_from_dict({"connectors": {"mail": "invalid"}}, tmp_path)
    with pytest.raises(ConfigError, match=r"\[connectors.mail\]"):
        config.connector("mail")


def test_invalid_file_configuration_reports_a_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('switchboard = "invalid"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[switchboard\]"):
        load_config(path)


def test_database_permissions_are_tightened_before_connecting(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    path.touch(mode=0o644)
    path.chmod(0o644)
    with Database(path).session() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("switchboard", "event_retention_days"),
        ("switchboard", "tool_timeout_seconds"),
        ("switchboard", "job_timeout_seconds"),
        ("switchboard", "job_workers"),
        ("policy", "approval_ttl_minutes"),
        ("retention", "tool_calls_days"),
        ("retention", "approvals_days"),
        ("retention", "actions_days"),
    ],
)
@pytest.mark.parametrize("value", [False, 0, -1, "14", 10**9])
def test_durations_cannot_silently_change_type(tmp_path: Path, section: str, key: str, value: Any) -> None:
    with pytest.raises(ConfigError, match="must be a whole number from"):
        config_from_dict({section: {key: value}}, tmp_path)


def test_bounded_settings_report_documented_defaults(tmp_path: Path) -> None:
    config = config_from_dict({}, tmp_path)
    assert config.bounded("switchboard", "tool_timeout_seconds") == 120
    assert config.bounded("switchboard", "job_timeout_seconds") == 300
    assert config.bounded("switchboard", "job_workers") == 4
    assert config.bounded("retention", "actions_days") == 180
    assert config.retention_connector_days() == {}
    assert config.broker_settings() == {}


@pytest.mark.parametrize(
    "broker",
    [
        {"client_socket": ""},
        {"operator_socket": 7},
        {"client_uids": []},
        {"client_uids": [True]},
        {"operator_uids": [-1]},
        {"operator_uids": ["1000"]},
        {"unknown": 1},
    ],
)
def test_broker_settings_refuse_ambiguous_principals(tmp_path: Path, broker: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        config_from_dict({"broker": broker}, tmp_path)


@pytest.mark.parametrize("value", [0, True, "30", 3651])
def test_connector_retention_overrides_are_bounded_days(tmp_path: Path, value: Any) -> None:
    with pytest.raises(ConfigError, match=r"\[retention.connectors\] telegram"):
        config_from_dict({"retention": {"connectors": {"telegram": value}}}, tmp_path)


@pytest.mark.parametrize("value", ["body", [""], [" body"], [1], ["field"] * 65, {"body": True}])
def test_audit_hash_only_must_list_field_names(tmp_path: Path, value: Any) -> None:
    with pytest.raises(ConfigError, match=r"\[audit\] hash_only must list up to 64 field names"):
        config_from_dict({"audit": {"hash_only": value}}, tmp_path)


def test_audit_hash_only_names_ignore_letter_case(tmp_path: Path) -> None:
    assert config_from_dict({}, tmp_path).audit_hash_only() == frozenset()
    config = config_from_dict({"audit": {"hash_only": ["Body", "summary"]}}, tmp_path)
    assert config.audit_hash_only() == {"body", "summary"}


def test_a_malformed_data_path_does_not_fall_back_to_another_database(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[switchboard]\ndata_dir = 42\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="data_dir must be a path string"):
        load_config(path)


def test_database_symlinks_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "other.sqlite3"
    target.touch()
    link = tmp_path / "state.sqlite3"
    link.symlink_to(target)
    with pytest.raises(ConfigError, match="cannot open the database"):
        Database(link).connect()
    assert target.stat().st_size == 0


@pytest.mark.parametrize(
    "data",
    [
        {"polciy": {"tools": {"mail_send": {"decision": "deny"}}}},
        {"mcp": {"expos": []}},
        {"policy": {"tool": {"mail_send": {"decision": "deny"}}}},
        {"switchboard": {"event_retention_day": 14}},
    ],
)
def test_misspelled_core_controls_cannot_fall_back_to_defaults(tmp_path: Path, data: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match="unknown"):
        config_from_dict(data, tmp_path)

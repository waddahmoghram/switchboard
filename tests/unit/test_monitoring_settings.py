"""Configuration tests; all targets and paths are synthetic."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from switchboard.connectors.monitoring.settings import (
    BackupCheck,
    DiskCheck,
    EndpointCheck,
    configuration_key,
    load_settings,
)
from switchboard.errors import ConfigError
from tests.fixtures.monitoring import backup, config, disk, endpoint


def test_settings_are_explicit_sorted_offline_and_do_not_mutate_input() -> None:
    raw = config()["connectors"]["monitoring"]
    original = copy.deepcopy(raw)
    settings = load_settings(raw)
    assert raw == original
    assert [check.name for check in settings.checks] == ["backup", "disk", "server"]
    assert isinstance(settings.checks[0], BackupCheck)
    assert isinstance(settings.checks[1], DiskCheck)
    assert isinstance(settings.checks[2], EndpointCheck)
    assert (settings.poll_interval_seconds, settings.snapshot_stale_after_seconds) == (300, 600)
    assert configuration_key(settings.checks[0]) == configuration_key(load_settings(original).checks[0])


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"checks": {}},
        {"checks": []},
        {"checks": {"bad-name": endpoint()}},
        {"checks": {"A": endpoint()}},
        {"checks": {"x" * 65: endpoint()}},
        {"checks": {str(n): endpoint() for n in range(33)}},
        {"checks": {"server": []}},
        {"checks": {"server": {"kind": "ssh", "command": "ignored"}}},
        {"checks": {"server": endpoint()}, "unknown": True},
        {"checks": {"server": endpoint()}, "poll_interval_seconds": 29},
        {"checks": {"server": endpoint()}, "poll_interval_seconds": True},
        {"checks": {"server": endpoint()}, "poll_interval_seconds": 30.0},
        {"checks": {"server": endpoint()}, "poll_interval_seconds": 86401},
        {"checks": {"server": endpoint()}, "snapshot_stale_after_seconds": 299},
        {"checks": {"server": endpoint()}, "snapshot_stale_after_seconds": 604801},
    ],
)
def test_invalid_global_settings_are_refused(values: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_settings(values)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/ready",
        "ftp://server.example.test/ready",
        "//server.example.test/ready",
        "https://",
        "https://user:password@server.example.test/ready",
        "https://server.example.test/ready?token=SYNTHETIC_SECRET",
        "https://server.example.test/ready?",
        "https://server.example.test/ready#fragment",
        "https://server.example.test/ready#",
        "https://server.example.test:0/ready",
        "https://server.example.test:65536/ready",
        "https://server.example.test:bad/ready",
        "https://server.example.test:/ready",
        "https://[::1]trailing/ready",
        "https://server.example.test:080/ready",
        "https://bad_host.example.test/ready",
        "https://127.1/ready",
        "https://2130706433/ready",
        "https://[fe80::1%25eth0]/ready",
        "https://0.0.0.0/ready",
        "https://224.0.0.1/ready",
        "https://server.example.test/../ready",
        "https://server.example.test/%2e%2e/ready",
        "https://server.example.test\\other/ready",
        "https://server.example.test/\nready",
        " https://server.example.test/ready",
        "https://server.example.test/ready path",
        "https://server.example.test/" + "x" * 2048,
    ],
)
def test_endpoint_addresses_are_strict_and_errors_do_not_echo_them(url: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({"checks": {"server": endpoint(url=url)}})
    assert "SYNTHETIC_SECRET" not in str(caught.value)
    assert url not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://server.example.test/ready",
        "http://203.0.113.8/ready",
        "http://169.254.169.254/ready",
        "http://0.0.0.0/ready",
        "http://[::]/ready",
    ],
)
def test_http_consent_does_not_allow_public_unknown_or_metadata_targets(url: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({"checks": {"server": endpoint(url=url, allow_http=True)}})


@pytest.mark.parametrize(
    "url",
    [
        "https://server.example.test:8443/health/ready",
        "https://[::1]:8443/ready",
        "http://localhost:8000/ready",
        "http://127.0.0.1:8000/ready",
        "http://192.168.10.5:8000/ready",
        "http://10.1.2.3/ready",
        "http://172.16.0.1/ready",
        "http://[::1]:8000/ready",
        "http://[fd00::1]:8000/ready",
    ],
)
def test_fixed_encrypted_or_explicit_local_http_targets_are_accepted(url: str) -> None:
    [check] = load_settings({"checks": {"server": endpoint(url=url, allow_http=True)}}).checks
    assert isinstance(check, EndpointCheck)
    assert check.url == url


@pytest.mark.parametrize(
    "updates",
    [
        {"readiness_field": None},
        {"readiness_field": "a."},
        {"readiness_field": ".".join(["x"] * 9)},
        {"readiness_equals": None},
        {"readiness_equals": []},
        {"readiness_equals": 1.5},
        {"readiness_equals": 1_000_000_001},
        {"readiness_equals": "x" * 129},
        {"readiness_equals": "\r\n"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
        {"timeout_seconds": True},
        {"expected_status": 199},
        {"expected_status": 300},
        {"expected_status": True},
        {"allow_http": "yes"},
        {"bearer_token": "literal-value"},
        {"bearer_token": "cmd:echo forbidden"},
        {"bearer_token": "env:"},
        {"bearer_token": "env:INVALID-NAME"},
        {"bearer_token": "file:relative-token"},
        {"bearer_token": "file:/private/../token"},
        {"extra": True},
    ],
)
def test_endpoint_options_are_typed_bounded_and_forbid_commands(updates: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_settings({"checks": {"server": endpoint(**updates)}})


@pytest.mark.parametrize("missing", ["readiness_field", "readiness_equals"])
def test_readiness_criterion_requires_both_parts(missing: str) -> None:
    raw = endpoint()
    del raw[missing]
    with pytest.raises(ConfigError):
        load_settings({"checks": {"server": raw}})


@pytest.mark.parametrize("reference", ["env:SYNTHETIC_TOKEN", "file:/synthetic-private/token"])
def test_secret_references_are_lazy_and_affect_configuration_identity(reference: str) -> None:
    [check] = load_settings({"checks": {"server": endpoint(bearer_token=reference)}}).checks
    [without] = load_settings({"checks": {"server": endpoint()}}).checks
    assert isinstance(check, EndpointCheck)
    assert check.bearer_token == reference
    assert configuration_key(check) != configuration_key(without)


@pytest.mark.parametrize(
    "updates",
    [
        {"directory": "relative"},
        {"directory": "~/status"},
        {"directory": "//remote/status"},
        {"directory": "/private/../status"},
        {"directory": "/private/\x00status"},
        {"filename": "../status.json"},
        {"filename": "/status.json"},
        {"filename": "status.txt"},
        {"filename": r"other\status.json"},
        {"filename": "x" * 124 + ".json"},
        {"max_age_seconds": 29},
        {"max_age_seconds": 31536001},
        {"max_age_seconds": True},
        {"unknown": True},
    ],
)
def test_backup_locations_and_freshness_are_bounded(updates: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_settings({"checks": {"backup": backup(**updates)}})


@pytest.mark.parametrize(
    "updates",
    [
        {"path": "relative"},
        {"path": "/disk/../other"},
        {"min_free_bytes": -1},
        {"min_free_bytes": 0},
        {"min_free_bytes": 2**63},
        {"min_free_bytes": True},
        {"min_free_bytes": 1.0},
        {"min_free_percent": 0},
        {"min_free_percent": 101},
        {"min_free_percent": float("nan")},
        {"min_free_percent": float("inf")},
        {"min_free_percent": 10**500},
        {"min_free_percent": True},
        {"command": "df"},
    ],
)
def test_disk_thresholds_are_explicit_finite_and_have_units(updates: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_settings({"checks": {"disk": disk(**updates)}})


def test_disk_requires_a_threshold_and_polling_supports_the_minimum_interval() -> None:
    with pytest.raises(ConfigError):
        load_settings({"checks": {"disk": {"kind": "disk", "path": "/synthetic-disk"}}})
    settings = load_settings(
        {"checks": {"disk": disk()}, "poll_interval_seconds": 30, "snapshot_stale_after_seconds": 30}
    )
    assert settings.poll_interval_seconds == settings.snapshot_stale_after_seconds == 30

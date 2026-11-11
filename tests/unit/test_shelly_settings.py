"""Bounded Shelly settings and safety opt-ins. All cases are authored and UNRUN."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from switchboard.connectors.shelly.settings import MAX_DEVICES, load_settings
from switchboard.errors import ConfigError
from tests.fixtures.shelly import BASE, DEVICE_ID, MODEL, REFERENCE, config


def table(**kwargs: Any) -> dict[str, Any]:
    return config(**kwargs)["connectors"]["shelly"]


def test_read_only_defaults_are_offline_and_do_not_create_controls() -> None:
    settings = load_settings(table(password=True))
    [device] = settings.devices
    assert settings.timeout_seconds == 5
    assert device.name == "desk_lamp" and device.base_url == BASE
    assert device.expected_device_id == DEVICE_ID and device.expected_model == MODEL
    assert device.switch_ids == (0,) and device.controls == ()
    assert device.password == REFERENCE
    assert REFERENCE not in repr(device)


def test_controls_require_both_device_and_component_opt_ins() -> None:
    [device] = load_settings(table(controls=True)).devices
    [control] = device.controls
    assert control.switch_id == 0 and control.max_power_watts == 60
    assert control.safe_load == "nonessential_lighting"


@pytest.mark.parametrize("generation", [2, 3, 4])
def test_protocol_generations_can_be_pinned_for_read_only_status(generation: int) -> None:
    [device] = load_settings(table(expected_generation=generation)).devices
    assert device.expected_generation == generation


@pytest.mark.parametrize("generation", [None, 1, 5, True, 2.0, "2"])
def test_unsupported_or_mistyped_generation_is_refused(generation: Any) -> None:
    with pytest.raises(ConfigError):
        load_settings(table(expected_generation=generation))


@pytest.mark.parametrize(
    "overrides",
    [
        {"allow_control": True},
        {"controls": {"0": {}}},
        {"allow_control": "true"},
        {"allow_http": 1},
        {"expected_device_id": DEVICE_ID + "\n"},
        {"expected_model": MODEL + "\n"},
        {"expected_device_id": "not a device"},
        {"expected_model": "x" * 49},
        {"switch_ids": []},
        {"switch_ids": [True]},
        {"switch_ids": [0, 0]},
        {"switch_ids": [32]},
        {"switch_ids": list(range(17))},
        {"switch_ids": "0"},
        {"arbitrary_rpc": "Switch.Toggle"},
    ],
)
def test_device_settings_fail_closed_on_invalid_or_unknown_values(overrides: dict[str, Any]) -> None:
    raw = table()
    raw["devices"]["desk_lamp"].update(overrides)
    with pytest.raises(ConfigError):
        load_settings(raw)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.10.40",
        "http://[fd12::40]:8080/",
        "https://shelly.example.test/",
        "https://192.168.10.40:8443",
    ],
)
def test_fixed_origins_are_supported_without_network_resolution(url: str) -> None:
    [device] = load_settings(table(base_url=url)).devices
    assert device.base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://shelly.local",
        "http://127.0.0.1",
        "http://169.254.169.254",
        "http://8.8.8.8",
        "http://[::1]",
        "http://[fe80::1]",
        "http://[ff02::1]",
        "http://[::ffff:192.168.10.40]",
        "http://192.168.10.40:0",
        "http://192.168.10.40:",
        "https://shelly.example.test:65536",
        "https://admin:secret@shelly.example.test",
        "https://shelly.example.test/rpc",
        "https://shelly.example.test/?token=private",
        "https://shelly.example.test/#fragment",
        "https://shelly.example.test?",
        "https://shelly.example.test#",
        "https://shelly.example.test\\@different.test",
        "https://shelly.example.test/%2f",
        "https://shelly.example.test\n",
        "ftp://192.168.10.40",
        "//shelly.example.test",
        "https://bad_host",
        "https://[fd12::40%25eth0]",
    ],
)
def test_unsafe_or_non_origin_addresses_are_refused(url: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(table(base_url=url))


def test_plain_http_requires_consent_even_for_private_numeric_targets() -> None:
    with pytest.raises(ConfigError, match="explicit allow_http"):
        load_settings(table(allow_http=False))


def test_origin_normalization_prevents_duplicate_aliases_for_the_same_endpoint() -> None:
    raw = table()
    duplicate = deepcopy(raw["devices"]["desk_lamp"])
    duplicate["base_url"] = BASE + ":80/"
    duplicate["expected_device_id"] = "shellyplusplugs-000000000002"
    raw["devices"]["second"] = duplicate
    with pytest.raises(ConfigError, match="duplicate"):
        load_settings(raw)


@pytest.mark.parametrize("value", ["literal-password", "env:", "file:", "env:A\n", None, "env:" + "A" * 513])
def test_passwords_must_be_bounded_nonempty_secret_references(value: Any) -> None:
    raw = table()
    raw["devices"]["desk_lamp"]["password"] = value
    with pytest.raises(ConfigError):
        load_settings(raw)


@pytest.mark.parametrize("value", [0, 16, True, 5.0, "5"])
def test_timeouts_are_strictly_bounded_integers(value: Any) -> None:
    raw = table()
    raw["timeout_seconds"] = value
    with pytest.raises(ConfigError):
        load_settings(raw)


@pytest.mark.parametrize("alias", ["Desk", "../plug", "a" * 33, "lamp\n"])
def test_device_aliases_are_bounded_and_not_addresses(alias: str) -> None:
    raw = table()
    raw["devices"] = {alias: raw["devices"]["desk_lamp"]}
    with pytest.raises(ConfigError):
        load_settings(raw)


def test_configuration_requires_a_bounded_nonempty_device_table() -> None:
    invalid_devices: list[Any] = [{}, [], None, {f"d{index}": {} for index in range(MAX_DEVICES + 1)}]
    for devices in invalid_devices:
        with pytest.raises(ConfigError):
            load_settings({"devices": devices})


def test_unknown_top_level_settings_are_not_silently_ignored() -> None:
    raw = table()
    raw["poll_seconds"] = 30
    with pytest.raises(ConfigError, match="unknown settings"):
        load_settings(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"safe_load": "heater"},
        {"safe_load": "security_camera"},
        {"safe_load": "medical_equipment"},
        {"safety_acknowledged": False},
        {"max_power_watts": 101},
        {"max_power_watts": 0},
        {"max_power_watts": True},
        {"max_power_watts": 60.0},
        {"toggle_after": 1},
    ],
)
def test_unsafe_or_unacknowledged_component_controls_are_refused(changes: dict[str, Any]) -> None:
    raw = table(controls=True)
    raw["devices"]["desk_lamp"]["controls"]["0"].update(changes)
    with pytest.raises(ConfigError):
        load_settings(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_generation": 3},
        {"expected_model": "SNSW-001P16EU"},
        {"switch_ids": [1]},
        {"controls": {"00": {}}},
        {"controls": {"1": {}}},
    ],
)
def test_only_documented_generation_2_plug_switch_zero_can_be_controlled(changes: dict[str, Any]) -> None:
    raw = table(controls=True)
    raw["devices"]["desk_lamp"].update(changes)
    with pytest.raises(ConfigError):
        load_settings(raw)


def test_documented_plus_plug_us_model_is_also_control_eligible() -> None:
    settings = load_settings(table(controls=True, expected_model="SNPL-00116US"))
    assert settings.devices[0].controls[0].switch_id == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://192.168.10.41"},
        {"expected_device_id": "shellyplusplugs-000000000002"},
        {"expected_model": "SNPL-00116US"},
        {"password": "env:SB_TEST_DIFFERENT_PASSWORD"},
    ],
)
def test_approval_fingerprint_changes_with_target_or_authentication_configuration(
    changes: dict[str, Any],
) -> None:
    [before] = load_settings(table(controls=True)).devices
    raw = table(controls=True)
    raw["devices"]["desk_lamp"].update(changes)
    [after] = load_settings(raw).devices
    assert before.target(before.controls[0]) != after.target(after.controls[0])


def test_approval_fingerprint_changes_with_the_safe_power_limit() -> None:
    [before] = load_settings(table(controls=True)).devices
    raw = table(controls=True)
    raw["devices"]["desk_lamp"]["controls"]["0"]["max_power_watts"] = 20
    [after] = load_settings(raw).devices
    assert before.target(before.controls[0]) != after.target(after.controls[0])

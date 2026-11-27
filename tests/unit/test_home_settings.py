"""UNRUN configuration and capability-binding contracts; synthetic inputs only."""

from __future__ import annotations

import copy
from typing import Any, NoReturn
from zoneinfo import ZoneInfoNotFoundError

import pytest

from switchboard.connectors.home import settings as home_settings
from switchboard.connectors.home.settings import load_settings
from switchboard.errors import ConfigError
from tests.unit.test_home_support import configuration


def table(**kwargs: Any) -> dict[str, Any]:
    home: dict[str, Any] = configuration(**kwargs)["connectors"]["home"]
    return home


def test_extension_defaults_are_off_and_legacy_settings_remain_valid() -> None:
    settings = load_settings({"base_url": "https://home.example.test", "token": "env:UNRESOLVED"})
    assert not settings.poll.enabled
    assert not settings.speech.enabled
    assert settings.lights == ()
    assert settings.climate == {}
    assert not settings.visible("light.lounge")
    assert settings.deny_domains == {"lock", "cover", "alarm_control_panel", "camera"}


def test_complete_configuration_parses_without_resolving_a_secret() -> None:
    settings = load_settings(table(polling=True, speaking=True))
    assert settings.poll.entities == ("light.lounge", "sensor.temperature", "binary_sensor.leak")
    assert settings.climate["climate.lounge"].unit == "C"
    assert settings.media_players["media_player.lounge"].maximum == 0.5
    assert settings.speech.engine == "tts.local_voice"
    assert len(settings.binding) == 64


def test_legacy_configuration_does_not_require_a_system_time_zone_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_key: str) -> NoReturn:
        raise ZoneInfoNotFoundError("synthetic missing system zones")

    monkeypatch.setattr(home_settings, "ZoneInfo", unavailable)
    settings = load_settings({"base_url": "https://home.example.test", "token": "env:UNRESOLVED"})
    assert settings.quiet_hours.enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"polll": {}},
        {"lights": ["light.*"]},
        {"lights": ["light.lounge", "light.lounge"]},
        {"lights": ["switch.heater"]},
        {"lights": ["light.lounge\n"]},
        {"lights": ["lock.front"]},
        {"rooms": {"unsafe": ["cover.garage"]}},
        {"rooms": {"unsafe": ["alarm_control_panel.house"]}},
        {"rooms": {"unsafe": ["switch.oven"]}},
        {"rooms": {"unsafe": ["scene.relax"]}},
        {"rooms": {"bad alias": ["light.lounge"]}},
        {"rooms": {"empty": []}},
        {"poll": {"enabled": "true"}},
        {"poll": {"enabled": True}},
        {"poll": {"enabled": True, "entities": ["sensor.*"]}},
        {"poll": {"entities": ["sensor.temperature"], "interval_seconds": 29}},
        {"poll": {"entities": ["sensor.temperature"], "interval_seconds": True}},
        {"poll": {"interval_seconds": 60, "stale_after_seconds": 59}},
        {"poll": {"enabled": True, "entities": ["lock.front"], "interval_seconds": 60}},
        {"media_players": {"media_player.lounge": {"minimum": 0.4, "maximum": 0.2}}},
        {"media_players": {"media_player.lounge": {}}},
        {"fans": {"fan.lounge": {"maximum": 101}}},
        {"fans": {"fan.lounge": {"max_change": 0}}},
        {"fans": {"fan.lounge": {"minimum": True}}},
        {"quiet_hours": {"enabled": True, "timezone": "Not/A_System_Zone"}},
        {"quiet_hours": {"enabled": True, "start": "24:00", "end": "07:00"}},
        {"quiet_hours": {"start": "07:00", "end": "07:00"}},
        {"quiet_hours": {"enabled": "yes"}},
        {"quiet_hours": {"enabled": True, "statr": "22:00"}},
        {"base_url": "https://home.example.test:invalid"},
    ],
)
def test_invalid_extension_settings_fail_explicitly(overrides: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_settings(table(**overrides))


@pytest.mark.parametrize(
    "changes",
    [
        {"unit": "K"},
        {"unit": []},
        {"minimum": 30, "maximum": 20},
        {"minimum": float("nan")},
        {"maximum": float("inf")},
        {"minimum": True},
        {"modes": ["auto"]},
        {"modes": ["heat", "heat"]},
        {"modes": []},
        {"max_change": 0},
        {"minimum": -51},
        {"maximum": 101},
        {"max_change": 21},
        {"unexpected": True},
    ],
)
def test_climate_configuration_rejects_unsafe_or_ambiguous_limits(changes: dict[str, Any]) -> None:
    values = table()
    values["climate"]["climate.lounge"].update(changes)
    with pytest.raises(ConfigError):
        load_settings(values)


@pytest.mark.parametrize(
    "action",
    [
        {"entity_id": "script.movie", "state": "on"},
        {"entity_id": "scene.movie", "state": "on"},
        {"entity_id": "switch.heater", "state": "on"},
        {"entity_id": "light.lounge", "state": "on", "service": "toggle"},
        {"entity_id": "light.lounge", "state": "on", "area_id": "whole_home"},
        {"entity_id": "light.lounge", "state": "on", "brightness_pct": 101},
        {"entity_id": "light.lounge", "state": "on", "brightness_pct": 0},
        {"entity_id": "light.lounge", "state": "off", "rgb_color": [1, 2, 3]},
        {"entity_id": "light.lounge", "state": "on", "rgb_color": [1, 2]},
        {"entity_id": "light.lounge", "state": "on", "rgb_color": [1, True, 3]},
        {"entity_id": "light.lounge", "state": "on", "rgb_color": [1, 2, 3], "color_temp_kelvin": 3000},
        {"entity_id": "light.lounge", "state": "on", "transition": float("nan")},
        {"entity_id": "light.lounge", "state": []},
    ],
)
def test_presets_are_typed_lighting_content_only(action: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_settings(table(lighting_presets={"unsafe": {"actions": [action]}}))


def test_presets_must_use_the_separate_lighting_allowlist() -> None:
    values = table(lights=["light.desk"])
    with pytest.raises(ConfigError, match="also be in lights"):
        load_settings(values)


def test_duplicate_preset_targets_are_rejected() -> None:
    action = {"entity_id": "light.lounge", "state": "on"}
    with pytest.raises(ConfigError, match="repeat a target"):
        load_settings(table(lighting_presets={"duplicate": {"actions": [action, action]}}))


def test_exposure_and_denial_win_over_all_new_allowlists() -> None:
    with pytest.raises(ConfigError, match="must be exposed"):
        load_settings(table(deny_domains=["light"]))
    with pytest.raises(ConfigError, match="must be exposed"):
        load_settings(table(expose=["light.desk"]))


@pytest.mark.parametrize(
    "changes",
    [
        {"external_processing": True},
        {"external_processing": True, "allow_external": True, "allow_sensitive": True},
        {"engine": "tts.not_exposed"},
        {"destinations": ["media_player.not_exposed"]},
        {"destinations": []},
        {"unknown": True},
    ],
)
def test_speech_configuration_fails_closed(changes: dict[str, Any]) -> None:
    values = table(speaking=True)
    values["speech"].update(changes)
    with pytest.raises(ConfigError):
        load_settings(values)


def test_speech_needs_a_privacy_declaration_and_quiet_hours() -> None:
    values = table(speaking=True)
    del values["speech"]["external_processing"]
    with pytest.raises(ConfigError, match="explicit external_processing"):
        load_settings(values)
    values = table(speaking=True, quiet_hours={})
    with pytest.raises(ConfigError, match="enabled quiet_hours"):
        load_settings(values)


@pytest.mark.parametrize(
    "section",
    [
        "base_url",
        "allow_http",
        "expose",
        "deny_domains",
        "lights",
        "rooms",
        "climate",
        "fans",
        "media_players",
        "lighting_presets",
        "quiet_hours",
        "speech",
    ],
)
def test_action_binding_covers_endpoint_targets_presets_engine_and_safety(section: str) -> None:
    original = table(speaking=True)
    altered = copy.deepcopy(original)
    if section == "base_url":
        altered[section] = "https://other.example.test"
    elif section == "allow_http":
        altered[section] = True
    elif section == "expose":
        altered[section].append("sensor.additional")
    elif section == "deny_domains":
        altered[section] = ["lock", "cover", "alarm_control_panel", "camera", "button"]
    elif section == "lights":
        altered[section].reverse()
    elif section == "rooms":
        altered[section]["lounge"].remove("light.desk")
    elif section == "climate":
        altered[section]["climate.lounge"]["maximum"] = 25
    elif section == "fans":
        altered[section]["fan.lounge"]["maximum"] = 70
    elif section == "media_players":
        altered[section]["media_player.lounge"]["maximum"] = 0.4
    elif section == "lighting_presets":
        altered[section]["reading"]["actions"][0]["brightness_pct"] = 30
    elif section == "quiet_hours":
        altered[section]["start"] = "21:00"
    else:
        altered["expose"].append("tts.other_voice")
        altered[section]["engine"] = "tts.other_voice"
    assert load_settings(original).binding != load_settings(altered).binding


def test_binding_excludes_secrets_and_is_stable_across_mapping_order() -> None:
    original = table(speaking=True)
    reordered = dict(reversed(list(original.items())))
    reordered["token"] = "file:~/a-different-secret-reference"
    first, second = load_settings(original), load_settings(reordered)
    assert first.binding == second.binding
    assert first.source == second.source
    assert "secret" not in first.binding

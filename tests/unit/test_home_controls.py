"""UNRUN reviewed-target, profile-bypass, unit, acceptance and readback regressions."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from switchboard.connectors.home import controls
from switchboard.errors import ToolError
from switchboard.policy import Origin, Tier
from tests.fakes import FakeHttp
from tests.unit.test_home_support import (
    HA,
    MOMENT,
    AppFactory,
    action_args,
    add_post,
    add_state,
    configuration,
    post_bodies,
    sample,
)

pytest_plugins = ["tests.unit.test_home_support"]


def test_new_tool_tiers_and_required_current_configuration_binding(home_app: AppFactory) -> None:
    app = home_app(configuration(speaking=True))
    for name in ("home_targets", "home_sensor_summary", "home_poll_status", "home_poll"):
        tool = app.registry.get(name)
        assert tool is not None
        assert tool.tier is Tier.READ
    binding = app.call("home_targets", {}, Origin.ASSISTANT).data["binding"]
    for name in (
        "home_lights",
        "home_climate",
        "home_fan",
        "home_media",
        "home_lighting_preset",
        "home_announce",
    ):
        tool = app.registry.get(name)
        assert tool is not None
        assert tool.tier is Tier.ACT
        assert "binding" in tool.input_schema["required"]
        assert tool.input_schema["properties"]["binding"]["enum"] == [binding]


def test_targets_are_offline_and_do_not_return_credentials(home_app: AppFactory, fake_http: FakeHttp) -> None:
    app = home_app(configuration())
    outcome = app.call("home_targets", {}, Origin.ASSISTANT)
    assert outcome.status == "ok"
    assert outcome.data["rooms"]["lounge"]
    assert "token" not in json.dumps(outcome.data)
    assert "synthetic-home-token" not in json.dumps(outcome.data)
    assert fake_http.requests == []


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("home_lights", {"entity_ids": ["light.lounge"], "state": "on"}),
        ("home_climate", {"entity_id": "climate.lounge", "unit": "C", "temperature": 22}),
        ("home_fan", {"entity_id": "fan.lounge", "percentage": 40}),
        ("home_media", {"entity_id": "media_player.lounge", "action": "play"}),
        ("home_lighting_preset", {"name": "reading", "fingerprint": "0" * 64}),
    ],
)
def test_new_actions_cannot_omit_the_binding(
    home_app: AppFactory, fake_http: FakeHttp, name: str, args: dict[str, Any]
) -> None:
    app = home_app(configuration())
    assert app.call(name, args, Origin.HUMAN).status == "invalid"
    assert fake_http.requests == []


def test_direct_handler_also_refuses_a_stale_binding(home_app: AppFactory, fake_http: FakeHttp) -> None:
    app = home_app(configuration())
    tool = app.registry.get("home_lights")
    assert tool is not None
    with pytest.raises(ToolError, match="configuration changed"):
        tool.handler({"binding": "0" * 64, "entity_ids": ["light.lounge"], "state": "on"})
    assert fake_http.requests == []


def test_room_alias_is_a_guard_not_an_implicit_entity_expansion(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on")
    add_state(fake_http, "light.lounge", state="on")
    args = action_args(app, room="lounge", entity_ids=["light.lounge"], state="on")
    outcome = app.call("home_lights", args, Origin.HUMAN)
    assert outcome.data["confirmed"] is True
    assert post_bodies(fake_http) == [{"entity_id": "light.lounge"}]
    assert all("light.desk" not in request.url for request in fake_http.requests)


def test_room_only_requests_cannot_choose_targets_after_approval(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    outcome = app.call("home_lights", action_args(app, room="lounge", state="on"), Origin.HUMAN)
    assert outcome.status == "invalid"
    assert fake_http.requests == []


@pytest.mark.parametrize("room", ["unconfigured", "desk_only"])
def test_room_alias_must_explicitly_contain_every_requested_device(
    home_app: AppFactory, fake_http: FakeHttp, room: str
) -> None:
    app = home_app(configuration(rooms={"desk_only": ["light.desk"]}))
    args = action_args(app, room=room, entity_ids=["light.lounge"], state="on")
    assert app.call("home_lights", args, Origin.HUMAN).status == "error"
    assert fake_http.requests == []


def test_exposure_alone_does_not_authorize_new_typed_controls(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(
        {
            "switchboard": {"connectors": ["home"]},
            "connectors": {
                "home": {"base_url": HA, "token": "env:SB_HOME_EXTENSION_TOKEN", "expose": ["light.*"]}
            },
        }
    )
    args = action_args(app, entity_ids=["light.lounge"], state="on")
    assert app.call("home_lights", args, Origin.HUMAN).status == "error"
    assert fake_http.requests == []


@pytest.mark.parametrize("key", ["area_id", "device_id", "floor_id", "label_id", "target", "data"])
def test_typed_tools_reject_extra_target_or_untyped_data_fields(
    home_app: AppFactory, fake_http: FakeHttp, key: str
) -> None:
    app = home_app(configuration())
    args = action_args(app, entity_ids=["light.lounge"], state="on", **{key: "all"})
    assert app.call("home_lights", args, Origin.HUMAN).status == "invalid"
    assert fake_http.requests == []


def test_duplicate_explicit_lights_are_rejected_before_provider_reads(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    args = action_args(app, entity_ids=["light.lounge", "light.lounge"], state="on")
    assert app.call("home_lights", args, Origin.HUMAN).status == "error"
    assert fake_http.requests == []


def test_lighting_posts_only_requested_entities_and_confirms_reported_values(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    changed = [sample("light.lounge", state="on", brightness=102), sample("light.desk", state="on")]
    add_post(fake_http, "light", "turn_on", body=json.dumps(changed))
    add_state(fake_http, "light.lounge", state="on", brightness=102, rgb_color=[10, 20, 30])
    args = action_args(
        app, entity_ids=["light.lounge"], state="on", brightness_pct=40, rgb_color=[10, 20, 30]
    )
    outcome = app.call("home_lights", args, Origin.HUMAN)
    assert outcome.status == "ok"
    assert outcome.data["request_status"] == "accepted"
    assert outcome.data["status"] == "observed"
    assert outcome.data["confirmed"] is True
    assert [item["entity_id"] for item in outcome.data["changed"]] == ["light.lounge"]
    assert post_bodies(fake_http) == [
        {
            "entity_id": "light.lounge",
            "brightness_pct": 40,
            "rgb_color": [10, 20, 30],
        }
    ]
    assert all(not request.follow_redirects for request in fake_http.requests)
    assert "fixture-private-attribute" not in json.dumps(outcome.data)


def test_a_multi_light_call_sends_an_exact_bounded_entity_list(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    for entity_id in ("light.lounge", "light.desk"):
        add_state(fake_http, entity_id)
    add_post(fake_http, "light", "turn_on")
    for entity_id in ("light.lounge", "light.desk"):
        add_state(fake_http, entity_id, state="on")
    args = action_args(app, entity_ids=["light.lounge", "light.desk"], state="on")
    assert app.call("home_lights", args, Origin.HUMAN).data["confirmed"] is True
    assert post_bodies(fake_http) == [{"entity_id": ["light.lounge", "light.desk"]}]


@pytest.mark.parametrize(
    "values",
    [
        {"state": "off", "brightness_pct": 10},
        {"state": "on", "rgb_color": [1, 2, 3], "color_temp_kelvin": 3000},
    ],
)
def test_mutually_incompatible_lighting_values_are_rejected_without_reads(
    home_app: AppFactory, fake_http: FakeHttp, values: dict[str, Any]
) -> None:
    app = home_app(configuration())
    assert (
        app.call("home_lights", action_args(app, entity_ids=["light.lounge"], **values), Origin.HUMAN).status
        == "error"
    )
    assert fake_http.requests == []


@pytest.mark.parametrize(
    ("values", "attributes"),
    [
        ({"brightness_pct": 40}, {"supported_color_modes": ["onoff"]}),
        ({"rgb_color": [1, 2, 3]}, {"supported_color_modes": ["brightness"]}),
        ({"color_temp_kelvin": 1800}, {}),
        ({"color_temp_kelvin": 3000}, {"min_color_temp_kelvin": None}),
        ({"transition": 2}, {"supported_features": 0}),
    ],
)
def test_lighting_does_not_invent_reported_capabilities(
    home_app: AppFactory, fake_http: FakeHttp, values: dict[str, Any], attributes: dict[str, Any]
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge", **attributes)
    outcome = app.call(
        "home_lights", action_args(app, entity_ids=["light.lounge"], state="on", **values), Origin.HUMAN
    )
    assert outcome.status == "error"
    assert post_bodies(fake_http) == []


def test_a_matching_readback_does_not_claim_a_requested_fade_finished(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on")
    add_state(fake_http, "light.lounge", state="on", brightness=102)
    args = action_args(app, entity_ids=["light.lounge"], state="on", brightness_pct=40, transition=5)
    result = app.call("home_lights", args, Origin.HUMAN).data
    assert result["status"] == "requested_unconfirmed"
    assert result["asynchronous"] is True
    assert result["confirmed"] is False
    assert result["observations"][0]["matches_request"] is True
    assert post_bodies(fake_http)[0]["transition"] == 5


def test_service_changed_states_are_not_substituted_for_independent_readback(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on", body=json.dumps([sample("light.lounge", state="on")]))
    add_state(fake_http, "light.lounge", state="off")
    result = app.call(
        "home_lights", action_args(app, entity_ids=["light.lounge"], state="on"), Origin.HUMAN
    ).data
    assert result["request_status"] == "accepted"
    assert result["status"] == "requested_unconfirmed"
    assert result["confirmed"] is False
    assert result["observations"][0]["state"] == "off"
    assert len(post_bodies(fake_http)) == 1


def test_readback_failure_is_unconfirmed_and_never_resends_the_effect(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on")
    fake_http.add_error("GET", f"{HA}/api/states/light.lounge")
    outcome = app.call("home_lights", action_args(app, entity_ids=["light.lounge"], state="on"), Origin.HUMAN)
    assert outcome.status == "ok"
    assert outcome.data["confirmed"] is False
    assert outcome.data["readback_errors"]
    assert len(post_bodies(fake_http)) == 1


def test_an_unreadable_success_reply_can_still_be_reported_only_as_observed_state(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on", body="not a changed-state list")
    add_state(fake_http, "light.lounge", state="on")
    result = app.call(
        "home_lights", action_args(app, entity_ids=["light.lounge"], state="on"), Origin.HUMAN
    ).data
    assert result["request_status"] == "accepted"
    assert result["confirmed"] is True
    assert result["changed"] == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_rejected_actions_are_not_faked_as_success_or_retried(
    home_app: AppFactory, fake_http: FakeHttp, status: int
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on", status=status)
    result = app.call(
        "home_lights", action_args(app, entity_ids=["light.lounge"], state="on"), Origin.HUMAN
    ).data
    assert result["request_status"] == result["status"] == "rejected"
    assert result["confirmed"] is False
    assert len(fake_http.requests) == 2


@pytest.mark.parametrize("status", [408, 500, 503])
def test_partial_or_ambiguous_provider_errors_remain_unconfirmed_even_if_state_matches(
    home_app: AppFactory, fake_http: FakeHttp, status: int
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on", status=status)
    add_state(fake_http, "light.lounge", state="on")
    result = app.call(
        "home_lights", action_args(app, entity_ids=["light.lounge"], state="on"), Origin.HUMAN
    ).data
    assert result["request_status"] == "unknown"
    assert result["status"] == "requested_unconfirmed"
    assert result["confirmed"] is False
    assert len(post_bodies(fake_http)) == 1


def test_a_timed_out_effect_is_observed_but_never_blindly_resent(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    fake_http.add_error("POST", f"{HA}/api/services/light/turn_on", "synthetic-home-token")
    add_state(fake_http, "light.lounge", state="on")
    result = app.call(
        "home_lights", action_args(app, entity_ids=["light.lounge"], state="on"), Origin.HUMAN
    ).data
    assert result["request_status"] == "unknown"
    assert result["confirmed"] is False
    assert result["retry_safe"] is False
    assert "synthetic-home-token" not in json.dumps(result)
    assert len(post_bodies(fake_http)) == 1


@pytest.mark.parametrize(
    ("entity_id", "tool", "values", "attributes"),
    [
        (
            "light.lounge",
            "home_lights",
            {"entity_ids": ["light.lounge"], "state": "on"},
            {"entity_id": ["light.desk"]},
        ),
        ("fan.lounge", "home_fan", {"entity_id": "fan.lounge", "percentage": 40}, {"state": "unavailable"}),
        (
            "media_player.lounge",
            "home_media",
            {"entity_id": "media_player.lounge", "action": "play"},
            {"group_members": ["media_player.lounge", "media_player.other"]},
        ),
    ],
)
def test_reported_groups_and_unavailable_targets_fail_closed(
    home_app: AppFactory,
    fake_http: FakeHttp,
    entity_id: str,
    tool: str,
    values: dict[str, Any],
    attributes: dict[str, Any],
) -> None:
    app = home_app(configuration())
    add_state(fake_http, entity_id, **attributes)
    assert app.call(tool, action_args(app, **values), Origin.HUMAN).status == "error"
    assert post_bodies(fake_http) == []


def test_climate_confirms_the_setpoint_not_that_the_room_reached_it(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "climate.lounge")
    add_post(fake_http, "climate", "set_temperature")
    add_state(fake_http, "climate.lounge", temperature=22, current_temperature=19)
    result = app.call(
        "home_climate",
        action_args(app, entity_id="climate.lounge", unit="C", temperature=22, room="lounge"),
        Origin.HUMAN,
    ).data
    assert result["confirmed"] is True
    assert result["observations"][0]["attributes"]["current_temperature"] == 19
    assert "physical completion" in result["confirmation_scope"]
    assert post_bodies(fake_http) == [{"entity_id": "climate.lounge", "temperature": 22}]


def test_fahrenheit_limits_use_reported_fahrenheit_without_conversion(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration()
    config["connectors"]["home"]["climate"]["climate.lounge"] = {
        "unit": "F",
        "minimum": 60,
        "maximum": 80,
        "max_change": 4,
        "modes": ["off", "heat"],
    }
    app = home_app(config)
    values: dict[str, Any] = {
        "temperature_unit": "\u00b0F",
        "temperature": 70,
        "min_temp": 50,
        "max_temp": 95,
    }
    add_state(fake_http, "climate.lounge", **values)
    add_post(fake_http, "climate", "set_temperature")
    add_state(fake_http, "climate.lounge", **{**values, "temperature": 72})
    args = action_args(app, entity_id="climate.lounge", unit="F", temperature=72)
    assert app.call("home_climate", args, Origin.HUMAN).data["confirmed"] is True
    assert post_bodies(fake_http) == [{"entity_id": "climate.lounge", "temperature": 72}]


@pytest.mark.parametrize(
    ("values", "attributes", "needs_read"),
    [
        ({"unit": "F", "temperature": 22}, {}, False),
        ({"temperature": 27}, {}, True),
        ({"temperature": 25}, {}, True),
        ({"temperature": 22}, {"max_temp": 21.5}, True),
        ({"temperature": 22}, {"temperature_unit": "\u00b0F"}, True),
        ({"temperature": 22}, {"temperature_unit": None}, True),
        ({"temperature": 22}, {"temperature": None}, True),
        ({"temperature": 22}, {"supported_features": 0}, True),
        ({"temperature": 22, "hvac_mode": "cool"}, {"hvac_modes": ["off", "heat"]}, True),
        ({}, {}, False),
        ({"temperature": 22, "target_temp_low": 20, "target_temp_high": 24}, {}, False),
        ({"target_temp_low": 20}, {}, False),
        ({"target_temp_low": 25, "target_temp_high": 21}, {}, False),
        ({"temperature": 22, "hvac_mode": "heat_cool"}, {}, True),
    ],
)
def test_climate_unit_limits_change_caps_and_shape_are_enforced(
    home_app: AppFactory,
    fake_http: FakeHttp,
    values: dict[str, Any],
    attributes: dict[str, Any],
    needs_read: bool,
) -> None:
    app = home_app(configuration())
    if needs_read:
        add_state(fake_http, "climate.lounge", **attributes)
    args = action_args(app, **{"entity_id": "climate.lounge", "unit": "C", **values})
    outcome = app.call("home_climate", args, Origin.HUMAN)
    assert outcome.status == "error"
    assert post_bodies(fake_http) == []
    assert bool(fake_http.requests) is needs_read


def test_climate_range_and_mode_are_explicit_and_bounded(home_app: AppFactory, fake_http: FakeHttp) -> None:
    app = home_app(configuration())
    add_state(fake_http, "climate.lounge")
    add_post(fake_http, "climate", "set_temperature")
    add_state(fake_http, "climate.lounge", state="heat_cool", target_temp_low=21, target_temp_high=25)
    args = action_args(
        app,
        entity_id="climate.lounge",
        unit="C",
        hvac_mode="heat_cool",
        target_temp_low=21,
        target_temp_high=25,
    )
    assert app.call("home_climate", args, Origin.HUMAN).data["confirmed"] is True
    assert post_bodies(fake_http) == [
        {
            "entity_id": "climate.lounge",
            "hvac_mode": "heat_cool",
            "target_temp_low": 21,
            "target_temp_high": 25,
        }
    ]


def test_mode_only_climate_off_does_not_claim_a_temperature_was_reached(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "climate.lounge")
    add_post(fake_http, "climate", "set_hvac_mode")
    add_state(fake_http, "climate.lounge", state="off")
    args = action_args(app, entity_id="climate.lounge", unit="C", hvac_mode="off")
    assert app.call("home_climate", args, Origin.HUMAN).data["confirmed"] is True
    assert post_bodies(fake_http) == [{"entity_id": "climate.lounge", "hvac_mode": "off"}]


@pytest.mark.parametrize(
    ("mode", "attributes"),
    [
        ("heat", {"temperature": 100}),
        ("cool", {"temperature": None}),
        ("heat", {"temperature_unit": None}),
        ("heat_cool", {"target_temp_low": 25, "target_temp_high": 20}),
        ("heat_cool", {"target_temp_high": 90}),
    ],
)
def test_mode_only_climate_cannot_energize_an_unsafe_or_unknown_existing_setpoint(
    home_app: AppFactory, fake_http: FakeHttp, mode: str, attributes: dict[str, Any]
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "climate.lounge", **attributes)
    args = action_args(app, entity_id="climate.lounge", unit="C", hvac_mode=mode)
    assert app.call("home_climate", args, Origin.HUMAN).status == "error"
    assert post_bodies(fake_http) == []


def test_mode_only_heating_checks_and_reads_back_the_existing_safe_setpoint(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "climate.lounge", state="off")
    add_post(fake_http, "climate", "set_hvac_mode")
    add_state(fake_http, "climate.lounge", state="heat")
    args = action_args(app, entity_id="climate.lounge", unit="C", hvac_mode="heat")
    assert app.call("home_climate", args, Origin.HUMAN).data["confirmed"] is True
    assert post_bodies(fake_http) == [{"entity_id": "climate.lounge", "hvac_mode": "heat"}]


def test_climate_readback_with_a_changed_unit_is_not_a_confirmed_requested_temperature(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "climate.lounge")
    add_post(fake_http, "climate", "set_temperature")
    add_state(fake_http, "climate.lounge", temperature=22, temperature_unit="\u00b0F")
    args = action_args(app, entity_id="climate.lounge", unit="C", temperature=22)
    result = app.call("home_climate", args, Origin.HUMAN).data
    assert result["status"] == "requested_unconfirmed"
    assert result["confirmed"] is False


def test_kelvin_readback_requires_the_reported_color_temperature_mode(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on")
    add_state(fake_http, "light.lounge", state="on", color_temp_kelvin=3500, color_mode="color_temp")
    args = action_args(app, entity_ids=["light.lounge"], state="on", color_temp_kelvin=3500)
    assert app.call("home_lights", args, Origin.HUMAN).data["confirmed"] is True
    assert post_bodies(fake_http) == [{"entity_id": "light.lounge", "color_temp_kelvin": 3500}]


@pytest.mark.parametrize("kind", ["climate", "fan", "media", "kelvin", "brightness", "rgb"])
def test_numeric_readback_tolerance_cannot_confirm_out_of_profile_or_invalid_values(
    home_app: AppFactory, fake_http: FakeHttp, kind: str
) -> None:
    config = configuration()
    home = config["connectors"]["home"]
    values: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    if kind == "climate":
        entity_id, tool, domain, service_name = "climate.lounge", "home_climate", "climate", "set_temperature"
        values = {"entity_id": entity_id, "unit": "C", "temperature": 26}
        before, after = {"temperature": 24}, {"temperature": 26.03}
    elif kind == "fan":
        home["fans"]["fan.lounge"]["maximum"] = 40
        entity_id, tool, domain, service_name = "fan.lounge", "home_fan", "fan", "set_percentage"
        values = {"entity_id": entity_id, "percentage": 40}
        before, after = {}, {"percentage": 40.02}
    elif kind == "media":
        home["media_players"]["media_player.lounge"]["maximum"] = 0.4
        entity_id, tool, domain, service_name = (
            "media_player.lounge",
            "home_media",
            "media_player",
            "volume_set",
        )
        values = {"entity_id": entity_id, "action": "volume", "volume_level": 0.4}
        before, after = {}, {"volume_level": 0.405}
    else:
        entity_id, tool, domain, service_name = "light.lounge", "home_lights", "light", "turn_on"
        values = {"entity_ids": [entity_id], "state": "on"}
        before = {}
        if kind == "kelvin":
            values["color_temp_kelvin"] = 6500
            after = {"state": "on", "color_mode": "color_temp", "color_temp_kelvin": 6500.03}
        elif kind == "brightness":
            values["brightness_pct"] = 100
            after = {"state": "on", "brightness": 256}
        else:
            values["rgb_color"] = [255, 0, 0]
            after = {"state": "on", "rgb_color": [256, 0, 0]}
    app = home_app(config)
    add_state(fake_http, entity_id, **before)
    add_post(fake_http, domain, service_name)
    add_state(fake_http, entity_id, **after)
    outcome = app.call(tool, action_args(app, **values), Origin.HUMAN)
    assert outcome.status == "ok"
    assert outcome.data["status"] == "requested_unconfirmed"
    assert outcome.data["confirmed"] is False
    assert len(post_bodies(fake_http)) == 1


@pytest.mark.parametrize(("percentage", "after_state"), [(60, "on"), (0, "off")])
def test_fan_percentages_are_bounded_and_observed(
    home_app: AppFactory, fake_http: FakeHttp, percentage: int, after_state: str
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "fan.lounge")
    add_post(fake_http, "fan", "set_percentage")
    add_state(fake_http, "fan.lounge", state=after_state, percentage=percentage)
    args = action_args(app, entity_id="fan.lounge", percentage=percentage)
    assert app.call("home_fan", args, Origin.HUMAN).data["confirmed"] is True
    assert post_bodies(fake_http) == [{"entity_id": "fan.lounge", "percentage": percentage}]


@pytest.mark.parametrize(
    ("percentage", "attributes", "needs_read"),
    [
        (81, {}, False),
        (60, {"percentage": 0}, True),
        (40, {"percentage": None}, True),
        (40, {"supported_features": 0}, True),
    ],
)
def test_fan_device_bounds_change_limits_and_unknown_capabilities_are_not_bypassed(
    home_app: AppFactory,
    fake_http: FakeHttp,
    percentage: int,
    attributes: dict[str, Any],
    needs_read: bool,
) -> None:
    app = home_app(configuration())
    if needs_read:
        add_state(fake_http, "fan.lounge", **attributes)
    outcome = app.call(
        "home_fan", action_args(app, entity_id="fan.lounge", percentage=percentage), Origin.HUMAN
    )
    assert outcome.status == "error"
    assert post_bodies(fake_http) == []


@pytest.mark.parametrize(
    ("action", "values", "service_name", "after_state", "after_attributes"),
    [
        ("play", {}, "media_play", "playing", {}),
        ("pause", {}, "media_pause", "paused", {}),
        ("stop", {}, "media_stop", "idle", {}),
        ("volume", {"volume_level": 0.4}, "volume_set", "paused", {"volume_level": 0.4}),
        ("mute", {"is_volume_muted": True}, "volume_mute", "paused", {"is_volume_muted": True}),
        ("mute", {"is_volume_muted": False}, "volume_mute", "paused", {"is_volume_muted": False}),
    ],
)
def test_media_actions_have_narrow_call_shapes_and_readback(
    home_app: AppFactory,
    fake_http: FakeHttp,
    action: str,
    values: dict[str, Any],
    service_name: str,
    after_state: str,
    after_attributes: dict[str, Any],
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "media_player.lounge")
    add_post(fake_http, "media_player", service_name)
    add_state(fake_http, "media_player.lounge", state=after_state, **after_attributes)
    args = action_args(app, entity_id="media_player.lounge", action=action, **values)
    result = app.call("home_media", args, Origin.HUMAN).data
    assert result["confirmed"] is True
    assert post_bodies(fake_http) == [{"entity_id": "media_player.lounge", **values}]
    assert "Not returned" not in json.dumps(result)


@pytest.mark.parametrize(
    ("action", "values", "attributes"),
    [
        ("volume", {"volume_level": 0.7}, {}),
        ("play", {}, {"volume_level": 0.8}),
        ("play", {}, {"volume_level": None}),
        ("mute", {"is_volume_muted": False}, {"volume_level": 0.8}),
        ("play", {}, {"supported_features": 0}),
        ("play", {"volume_level": 0.3}, {}),
        ("volume", {}, {}),
        ("mute", {"volume_level": 0.3}, {}),
    ],
)
def test_media_volume_and_capability_rules_fail_closed(
    home_app: AppFactory,
    fake_http: FakeHttp,
    action: str,
    values: dict[str, Any],
    attributes: dict[str, Any],
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "media_player.lounge", **attributes)
    args = action_args(app, entity_id="media_player.lounge", action=action, **values)
    assert app.call("home_media", args, Origin.HUMAN).status == "error"
    assert post_bodies(fake_http) == []


@pytest.mark.parametrize(
    ("action", "values", "allowed", "service_name", "after"),
    [
        ("play", {}, False, "media_play", {}),
        ("mute", {"is_volume_muted": False}, False, "volume_mute", {}),
        ("volume", {"volume_level": 0.4}, False, "volume_set", {}),
        ("volume", {"volume_level": 0.2}, True, "volume_set", {"volume_level": 0.2}),
        ("mute", {"is_volume_muted": True}, True, "volume_mute", {"is_volume_muted": True}),
        ("pause", {}, True, "media_pause", {"state": "paused"}),
        ("stop", {}, True, "media_stop", {"state": "idle"}),
    ],
)
def test_quiet_hours_block_audible_increases_but_keep_quieting_controls(
    home_app: AppFactory,
    fake_http: FakeHttp,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    values: dict[str, Any],
    allowed: bool,
    service_name: str,
    after: dict[str, Any],
) -> None:
    app = home_app(configuration())
    monkeypatch.setattr(controls, "now", lambda: MOMENT + timedelta(hours=11))
    add_state(fake_http, "media_player.lounge")
    if allowed:
        add_post(fake_http, "media_player", service_name)
        add_state(fake_http, "media_player.lounge", **after)
    args = action_args(app, entity_id="media_player.lounge", action=action, **values)
    outcome = app.call("home_media", args, Origin.HUMAN)
    assert outcome.status == ("ok" if allowed else "error")
    assert len(post_bodies(fake_http)) == int(allowed)


def test_media_does_not_accept_a_media_address_or_arbitrary_action(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    for values in (
        {"action": "play_media"},
        {"action": "play", "media_content_id": "https://example.test/unapproved"},
    ):
        args = action_args(app, entity_id="media_player.lounge", **values)
        assert app.call("home_media", args, Origin.HUMAN).status == "invalid"
    assert fake_http.requests == []


@pytest.mark.parametrize(
    ("domain", "entity_id", "service_name", "data"),
    [
        ("climate", "climate.lounge", "set_temperature", {"temperature": 100}),
        ("climate", "climate.lounge", "set_hvac_mode", {"hvac_mode": "auto"}),
        ("fan", "fan.lounge", "set_percentage", {"percentage": 100}),
        ("media_player", "media_player.lounge", "volume_set", {"volume_level": 1}),
        ("media_player", "media_player.lounge", "volume_up", {}),
        ("media_player", "media_player.lounge", "volume_mute", {"is_volume_muted": False}),
        ("light", "light.lounge", "toggle", {}),
        ("light", "light.lounge", "turn_on", {"brightness_pct": 40}),
    ],
)
def test_legacy_generic_tool_cannot_bypass_new_profiles_or_lighting_presets(
    home_app: AppFactory,
    fake_http: FakeHttp,
    domain: str,
    entity_id: str,
    service_name: str,
    data: dict[str, Any],
) -> None:
    app = home_app(configuration())
    outcome = app.call(
        "home_call_service",
        {
            "domain": domain,
            "service": service_name,
            "entity_id": entity_id,
            "data": data,
        },
        Origin.HUMAN,
    )
    assert outcome.status == "error"
    assert "reviewed home profile" in outcome.message
    assert fake_http.requests == []


def test_room_only_profile_also_blocks_the_legacy_generic_escape_hatch(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(climate={}))
    outcome = app.call(
        "home_call_service",
        {
            "domain": "climate",
            "service": "set_temperature",
            "entity_id": "climate.lounge",
            "data": {"temperature": 100},
        },
        Origin.HUMAN,
    )
    assert outcome.status == "error"
    assert "home_climate" in outcome.message
    assert fake_http.requests == []


def test_unprofiled_legacy_generic_calls_keep_the_existing_argument_and_result_contract(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(
        {
            "switchboard": {"connectors": ["home"]},
            "connectors": {
                "home": {
                    "base_url": HA,
                    "token": "env:SB_HOME_EXTENSION_TOKEN",
                    "expose": ["climate.*"],
                }
            },
        }
    )
    add_post(fake_http, "climate", "set_temperature")
    outcome = app.call(
        "home_call_service",
        {
            "domain": "climate",
            "service": "set_temperature",
            "entity_id": "climate.lounge",
            "data": {"temperature": 20.5},
        },
        Origin.HUMAN,
    )
    assert outcome.status == "ok"
    assert outcome.data == {"called": "climate.set_temperature", "entity_id": "climate.lounge", "changed": []}
    assert len(fake_http.requests) == 1


def test_lighting_presets_use_pinned_reviewed_content_and_preflight_every_target(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    info = app.call("home_targets", {}, Origin.ASSISTANT).data
    add_state(fake_http, "light.lounge")
    add_state(fake_http, "light.desk")
    add_post(fake_http, "light", "turn_on")
    add_state(fake_http, "light.lounge", state="on", brightness=102)
    add_post(fake_http, "light", "turn_on")
    add_state(fake_http, "light.desk", state="on", brightness=128)
    args = action_args(app, name="reading", fingerprint=info["lighting_presets"]["reading"]["fingerprint"])
    result = app.call("home_lighting_preset", args, Origin.HUMAN).data
    assert result["confirmed"] is True
    assert result["atomic"] is False
    assert post_bodies(fake_http) == [
        {"entity_id": "light.lounge", "brightness_pct": 40, "transition": 0},
        {"entity_id": "light.desk", "brightness_pct": 50, "transition": 0},
    ]
    assert [request.method for request in fake_http.requests[:3]] == ["GET", "GET", "POST"]


def test_an_unpinned_or_changed_preset_is_not_executed(home_app: AppFactory, fake_http: FakeHttp) -> None:
    app = home_app(configuration())
    args = action_args(app, name="reading", fingerprint="0" * 64)
    assert app.call("home_lighting_preset", args, Origin.HUMAN).status == "error"
    assert fake_http.requests == []


def test_preset_preflight_failure_sends_no_actions_to_any_target(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    info = app.call("home_targets", {}, Origin.ASSISTANT).data
    add_state(fake_http, "light.lounge")
    add_state(fake_http, "light.desk", supported_color_modes=["onoff"])
    args = action_args(app, name="reading", fingerprint=info["lighting_presets"]["reading"]["fingerprint"])
    assert app.call("home_lighting_preset", args, Origin.HUMAN).status == "error"
    assert post_bodies(fake_http) == []


def test_an_ambiguous_preset_effect_stops_remaining_actions_without_compensation_or_resend(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    info = app.call("home_targets", {}, Origin.ASSISTANT).data
    add_state(fake_http, "light.lounge")
    add_state(fake_http, "light.desk")
    fake_http.add_error("POST", f"{HA}/api/services/light/turn_on")
    add_state(fake_http, "light.lounge", state="on", brightness=102)
    args = action_args(app, name="reading", fingerprint=info["lighting_presets"]["reading"]["fingerprint"])
    result = app.call("home_lighting_preset", args, Origin.HUMAN).data
    assert result["confirmed"] is False
    assert result["not_attempted"] == ["light.desk"]
    assert len(post_bodies(fake_http)) == 1


@pytest.mark.parametrize(
    ("status", "effect_unknown", "state"),
    [(200, False, "succeeded"), (400, False, "succeeded"), (503, True, "uncertain")],
)
def test_reports_flag_unknown_delivery_for_the_action_journal(
    home_app: AppFactory, fake_http: FakeHttp, status: int, effect_unknown: bool, state: str
) -> None:
    app = home_app(configuration())
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on", status=status)
    add_state(fake_http, "light.lounge", state="on")
    outcome = app.call("home_lights", action_args(app, entity_ids=["light.lounge"], state="on"), Origin.HUMAN)
    assert outcome.status == "ok"
    assert outcome.data["effect_unknown"] is effect_unknown
    assert outcome.data["confirmed"] is (status == 200)
    assert outcome.action is not None
    assert outcome.action["state"] == state


def test_an_ambiguous_preset_step_marks_the_whole_report_unknown(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    info = app.call("home_targets", {}, Origin.ASSISTANT).data
    add_state(fake_http, "light.lounge")
    add_state(fake_http, "light.desk")
    fake_http.add_error("POST", f"{HA}/api/services/light/turn_on")
    add_state(fake_http, "light.lounge", state="on", brightness=102)
    args = action_args(app, name="reading", fingerprint=info["lighting_presets"]["reading"]["fingerprint"])
    outcome = app.call("home_lighting_preset", args, Origin.HUMAN)
    assert outcome.data["effect_unknown"] is True
    assert [step["effect_unknown"] for step in outcome.data["results"]] == [True]
    assert outcome.action is not None
    assert outcome.action["state"] == "uncertain"
    repeat = app.call("home_lighting_preset", args, Origin.HUMAN)
    assert repeat.status == "denied"
    assert len(post_bodies(fake_http)) == 1

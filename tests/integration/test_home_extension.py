"""UNRUN synthetic application integration: approvals, configuration reloads and durable events."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from typing import Any

import pytest

from switchboard.connectors.home import controls, speech
from switchboard.connectors.home.settings import load_settings
from switchboard.policy import Origin
from tests.fakes import FakeHttp
from tests.unit.test_home_support import (
    HA,
    MOMENT,
    AppFactory,
    action_args,
    add_post,
    add_state,
    add_states,
    configuration,
    post_bodies,
    sample,
)

pytest_plugins = ["tests.unit.test_home_support"]
pytestmark = pytest.mark.integration


def test_assistant_lighting_waits_for_approval_and_executes_the_bound_request_only_once(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    args = action_args(app, entity_ids=["light.lounge"], state="on", brightness_pct=40, room="lounge")
    outcome = app.call("home_lights", args, Origin.ASSISTANT)
    assert outcome.status == "pending" and outcome.approval_id is not None
    assert fake_http.requests == []
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on")
    add_state(fake_http, "light.lounge", state="on", brightness=102)
    approved = app.dispatcher.approve(outcome.approval_id)
    assert approved.status == "ok" and approved.data["confirmed"] is True
    assert post_bodies(fake_http) == [{"entity_id": "light.lounge", "brightness_pct": 40}]
    assert app.dispatcher.approve(outcome.approval_id).status == "error"
    assert len(post_bodies(fake_http)) == 1


def test_approval_cannot_bypass_quiet_hours_that_started_while_it_was_pending(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = home_app(configuration())
    outcome = app.call(
        "home_media", action_args(app, entity_id="media_player.lounge", action="play"), Origin.ASSISTANT
    )
    assert outcome.approval_id is not None
    monkeypatch.setattr(controls, "now", lambda: MOMENT + timedelta(hours=11))
    add_state(fake_http, "media_player.lounge")
    approved = app.dispatcher.approve(outcome.approval_id)
    assert approved.status == "error" and "quiet hours" in approved.message
    assert post_bodies(fake_http) == []
    request = app.approvals.get(outcome.approval_id)
    assert request is not None and request.status == "failed"


def test_approved_announcement_remains_unconfirmed_in_durable_approval_status(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(speaking=True))
    args = action_args(
        app,
        engine="tts.local_voice",
        entity_id="media_player.lounge",
        text="Synthetic reminder.",
        volume_level=0.3,
        sensitive=False,
    )
    pending = app.call("home_announce", args, Origin.ASSISTANT)
    assert pending.approval_id is not None and fake_http.requests == []
    add_state(fake_http, "tts.local_voice")
    add_state(fake_http, "media_player.lounge")
    add_post(fake_http, "tts", "speak")
    add_state(fake_http, "media_player.lounge", state="playing")
    approved = app.dispatcher.approve(pending.approval_id)
    assert approved.status == "ok" and approved.data["status"] == "requested_unconfirmed"
    status = app.call("approvals_status", {"id": pending.approval_id}, Origin.ASSISTANT).data
    assert status["status"] == "executed"
    assert status["result"]["delivery_verified"] is False
    assert app.dispatcher.approve(pending.approval_id).status == "error"
    assert len(post_bodies(fake_http)) == 1


@pytest.mark.parametrize(
    ("change", "tool_name", "values"),
    [
        ("endpoint", "home_lights", {"entity_ids": ["light.lounge"], "state": "on"}),
        ("room_membership", "home_lights", {"entity_ids": ["light.lounge"], "state": "on", "room": "lounge"}),
        ("climate_limits", "home_climate", {"entity_id": "climate.lounge", "unit": "C", "temperature": 22}),
        ("fan_limits", "home_fan", {"entity_id": "fan.lounge", "percentage": 40}),
        (
            "volume_limits",
            "home_media",
            {"entity_id": "media_player.lounge", "action": "volume", "volume_level": 0.4},
        ),
        ("preset_content", "home_lighting_preset", {"name": "reading"}),
        (
            "speech_engine",
            "home_announce",
            {
                "engine": "tts.local_voice",
                "entity_id": "media_player.lounge",
                "text": "Synthetic reminder.",
                "volume_level": 0.3,
                "sensitive": False,
            },
        ),
        (
            "speech_privacy",
            "home_announce",
            {
                "engine": "tts.local_voice",
                "entity_id": "media_player.lounge",
                "text": "Synthetic reminder.",
                "volume_level": 0.3,
                "sensitive": False,
            },
        ),
        ("quiet_hours", "home_media", {"entity_id": "media_player.lounge", "action": "play"}),
    ],
)
def test_pending_approval_never_retargets_or_uses_changed_safety_configuration_after_reload(
    home_app: AppFactory, fake_http: FakeHttp, change: str, tool_name: str, values: dict[str, Any]
) -> None:
    config = configuration(speaking=True)
    app = home_app(copy.deepcopy(config))
    args = action_args(app, **values)
    if tool_name == "home_lighting_preset":
        args["fingerprint"] = app.call("home_targets", {}, Origin.ASSISTANT).data["lighting_presets"][
            "reading"
        ]["fingerprint"]
    queued = app.call(tool_name, args, Origin.ASSISTANT)
    assert queued.status == "pending" and queued.approval_id is not None
    changed = config["connectors"]["home"]
    if change == "endpoint":
        changed["base_url"] = "https://different.example.test"
    elif change == "room_membership":
        changed["rooms"]["lounge"].remove("light.lounge")
    elif change == "climate_limits":
        changed["climate"]["climate.lounge"]["maximum"] = 25
    elif change == "fan_limits":
        changed["fans"]["fan.lounge"]["maximum"] = 70
    elif change == "volume_limits":
        changed["media_players"]["media_player.lounge"]["maximum"] = 0.45
    elif change == "preset_content":
        changed["lighting_presets"]["reading"]["actions"][0]["brightness_pct"] = 30
    elif change == "speech_engine":
        changed["speech"]["engine"] = "tts.replacement"
        changed["expose"].append("tts.replacement")
    elif change == "speech_privacy":
        changed["speech"].update(external_processing=True, allow_external=True)
    else:
        changed["quiet_hours"]["start"] = "21:00"
    restarted = home_app(config)
    approved = restarted.dispatcher.approve(queued.approval_id)
    assert approved.status == "invalid"
    assert "stored arguments no longer match" in approved.message
    assert fake_http.requests == []
    request = restarted.approvals.get(queued.approval_id)
    assert request is not None and request.status == "failed"


def test_a_new_endpoint_needs_a_fresh_binding_and_fresh_approval(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration()
    app = home_app(copy.deepcopy(config))
    old_args = action_args(app, entity_ids=["light.lounge"], state="on")
    pending = app.call("home_lights", old_args, Origin.ASSISTANT)
    assert pending.approval_id is not None
    other = "https://different.example.test"
    config["connectors"]["home"]["base_url"] = other
    restarted = home_app(config)
    assert restarted.dispatcher.approve(pending.approval_id).status == "invalid"
    new_args = action_args(restarted, entity_ids=["light.lounge"], state="on")
    assert new_args["binding"] != old_args["binding"]
    fresh = restarted.call("home_lights", new_args, Origin.ASSISTANT)
    assert fresh.approval_id is not None
    fake_http.add("GET", f"{other}/api/states/light.lounge", body=json.dumps(sample("light.lounge")))
    fake_http.add("POST", f"{other}/api/services/light/turn_on", body="[]")
    fake_http.add(
        "GET", f"{other}/api/states/light.lounge", body=json.dumps(sample("light.lounge", state="on"))
    )
    assert restarted.dispatcher.approve(fresh.approval_id).data["confirmed"] is True
    assert all(request.url.startswith(other) for request in fake_http.requests)


def test_secret_reference_rotation_is_lazy_and_is_not_part_of_the_public_binding(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration()
    app = home_app(copy.deepcopy(config))
    args = action_args(app, entity_ids=["light.lounge"], state="on")
    pending = app.call("home_lights", args, Origin.ASSISTANT)
    assert pending.approval_id is not None
    config["connectors"]["home"]["token"] = "env:SB_HOME_ROTATED_TOKEN"
    monkeypatch.setenv("SB_HOME_ROTATED_TOKEN", "rotated-synthetic-token")
    restarted = home_app(config)
    assert action_args(restarted)["binding"] == args["binding"]
    assert fake_http.requests == []
    add_state(fake_http, "light.lounge")
    add_post(fake_http, "light", "turn_on")
    add_state(fake_http, "light.lounge", state="on")
    assert restarted.dispatcher.approve(pending.approval_id).status == "ok"
    assert all(
        request.headers["authorization"].endswith("rotated-synthetic-token") for request in fake_http.requests
    )


def test_a_sampled_event_can_queue_but_not_execute_a_bound_rule_action_without_approval(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration(polling=True)
    binding = load_settings(config["connectors"]["home"]).binding
    config["rules"] = [
        {
            "name": "Synthetic sampled leak request",
            "on": "home.state_changed",
            "match": {"payload.entity_id": "binary_sensor.leak", "payload.current.state": "on"},
            "do": [
                {
                    "tool": "home_lights",
                    "args": {
                        "binding": binding,
                        "entity_ids": ["light.lounge"],
                        "state": "on",
                    },
                }
            ],
        }
    ]
    app = home_app(config)
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    assert app.process_events() == 0
    add_states(
        fake_http,
        [
            sample("light.lounge"),
            sample("sensor.temperature"),
            sample("binary_sensor.leak", state="on"),
        ],
    )
    assert app.call("home_poll", {}, Origin.ASSISTANT).data["changed"] == 1
    assert app.process_events() == 1
    [pending] = app.approvals.list("pending")
    assert pending.tool == "home_lights" and pending.args["binding"] == binding
    assert post_bodies(fake_http) == []
    assert app.process_events() == 0
    assert len(app.approvals.list("pending")) == 1


def test_events_and_snapshots_survive_restart_before_processing(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration(polling=True)
    app = home_app(config)
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    add_states(
        fake_http,
        [
            sample("light.lounge", state="on"),
            sample("sensor.temperature"),
            sample("binary_sensor.leak"),
        ],
    )
    app.call("home_poll", {}, Origin.ASSISTANT)
    restarted = home_app(config)
    [event] = restarted.ctx.events.pending()
    assert event.payload["current"]["state"] == "on"
    snapshot = next(
        item
        for item in restarted.ctx.require("home").store.snapshots()
        if item["entity_id"] == "light.lounge"
    )
    assert snapshot["state"] == "on" and snapshot["revision"] == event.payload["revision"]
    assert restarted.process_events() == 1
    assert restarted.ctx.events.pending() == []


def test_core_kill_switch_and_current_policy_constraints_are_rechecked_on_approval(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    queued = app.call(
        "home_lights", action_args(app, entity_ids=["light.lounge"], state="on"), Origin.ASSISTANT
    )
    assert queued.approval_id is not None
    (app.ctx.data_dir / "KILL_SWITCH").touch()
    assert app.dispatcher.approve(queued.approval_id).status == "denied"
    assert app.call("home_targets", {}, Origin.ASSISTANT).status == "ok"
    assert fake_http.requests == []


def test_speaker_recipient_allowlist_blocks_an_unapproved_destination_before_network(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration(speaking=True)
    config["policy"]["tools"]["home_announce"]["allow_recipients"] = ["media_player.other"]
    app = home_app(config)
    args = action_args(
        app,
        engine="tts.local_voice",
        entity_id="media_player.lounge",
        text="Synthetic reminder.",
        volume_level=0.3,
        sensitive=False,
    )
    assert app.call("home_announce", args, Origin.ASSISTANT).status == "denied"
    assert fake_http.requests == []


def test_legacy_tool_refuses_a_new_profile_added_while_an_old_generic_approval_was_pending(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    legacy: dict[str, Any] = {
        "switchboard": {"connectors": ["home"]},
        "connectors": {
            "home": {"base_url": HA, "token": "env:SB_HOME_EXTENSION_TOKEN", "expose": ["climate.*"]}
        },
    }
    app = home_app(legacy)
    queued = app.call(
        "home_call_service",
        {
            "domain": "climate",
            "service": "set_temperature",
            "entity_id": "climate.lounge",
            "data": {"temperature": 90},
        },
        Origin.ASSISTANT,
    )
    assert queued.approval_id is not None
    legacy["connectors"]["home"]["climate"] = {
        "climate.lounge": {
            "unit": "C",
            "minimum": 16,
            "maximum": 26,
            "max_change": 3,
            "modes": ["off", "heat"],
        },
    }
    restarted = home_app(legacy)
    approved = restarted.dispatcher.approve(queued.approval_id)
    assert approved.status == "error" and "reviewed home profile" in approved.message
    assert fake_http.requests == []


def test_speech_approval_time_privacy_still_blocks_sensitive_content_without_opt_in(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = home_app(configuration(speaking=True))
    args = action_args(
        app,
        engine="tts.local_voice",
        entity_id="media_player.lounge",
        text="Synthetic private reminder.",
        volume_level=0.3,
        sensitive=True,
    )
    queued = app.call("home_announce", args, Origin.ASSISTANT)
    assert queued.approval_id is not None
    monkeypatch.setattr(speech, "now", lambda: MOMENT)
    approved = app.dispatcher.approve(queued.approval_id)
    assert approved.status == "error" and "sensitive" in approved.message
    assert fake_http.requests == []

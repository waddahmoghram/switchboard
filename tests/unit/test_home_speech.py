"""UNRUN announcement tests: explicit approval, privacy, quiet hours and unverifiable delivery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from switchboard.connectors.home import speech
from switchboard.connectors.home.settings import QuietHours
from switchboard.errors import ConfigError
from switchboard.policy import Origin
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
)

pytest_plugins = ["tests.unit.test_home_support"]


def announcement(app: Any, **overrides: Any) -> dict[str, Any]:
    return action_args(
        app,
        **{
            "engine": "tts.local_voice",
            "entity_id": "media_player.lounge",
            "text": "The synthetic timer finished.",
            "volume_level": 0.3,
            "sensitive": False,
            **overrides,
        },
    )


def ready(fake_http: FakeHttp, **speaker_attributes: Any) -> None:
    add_state(fake_http, "tts.local_voice")
    add_state(fake_http, "media_player.lounge", **speaker_attributes)


def test_speech_tool_is_absent_until_explicitly_enabled(home_app: AppFactory) -> None:
    assert home_app(configuration()).registry.get("home_announce") is None


@pytest.mark.parametrize(
    "rules",
    [
        {},
        {"home_*": {"decision": "ask"}},
        {"home_*": {"decision": "allow"}},
        {"home_announce": {"decision": "allow"}},
    ],
)
def test_enabled_speech_requires_an_exact_non_unattended_policy(
    home_app: AppFactory, fake_http: FakeHttp, rules: dict[str, Any]
) -> None:
    config = configuration(speaking=True)
    config["policy"]["tools"] = rules
    with pytest.raises(ConfigError, match="unattended announcements are not supported"):
        home_app(config)
    assert fake_http.requests == []


def test_an_exact_speech_ask_rule_wins_over_a_broad_allow(home_app: AppFactory, fake_http: FakeHttp) -> None:
    config = configuration(speaking=True)
    config["policy"]["tools"]["home_*"] = {"decision": "allow"}
    app = home_app(config)
    outcome = app.call("home_announce", announcement(app), Origin.ASSISTANT)
    assert outcome.status == "pending"
    assert fake_http.requests == []


def test_explicit_speech_denial_remains_a_denial(home_app: AppFactory, fake_http: FakeHttp) -> None:
    config = configuration(speaking=True)
    config["policy"]["tools"]["home_announce"]["decision"] = "deny"
    app = home_app(config)
    assert app.call("home_announce", announcement(app), Origin.HUMAN).status == "denied"
    assert fake_http.requests == []


def test_announcement_summary_shows_the_bound_text_engine_destination_volume_and_privacy(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(speaking=True))
    args = announcement(app)
    assert app.call("home_announce", args, Origin.ASSISTANT).status == "pending"
    [pending] = app.approvals.list("pending")
    for value in (args["text"], args["engine"], args["entity_id"], "0.3", "sensitive=False", "external=no"):
        assert value in pending.summary
    assert pending.args == args
    assert fake_http.requests == []


def test_longest_allowed_plain_text_summary_fits_the_core_approval_limit(home_app: AppFactory) -> None:
    app = home_app(configuration(speaking=True))
    args = announcement(
        app,
        engine="tts." + "e" * 60,
        entity_id="media_player." + "d" * 51,
        text="\\" * 140,
        volume_level=0.33333333333333337,
    )
    summary = app.ctx.require("home").speech.summarize(args)
    assert len(summary) <= 500
    assert args["engine"] in summary and args["entity_id"] in summary
    assert json.dumps(args["text"]) in summary


def test_speech_uses_verified_speak_shape_and_never_claims_playback_delivery(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(speaking=True))
    ready(fake_http)
    add_post(fake_http, "tts", "speak")
    add_state(fake_http, "media_player.lounge", state="playing")
    outcome = app.call("home_announce", announcement(app), Origin.HUMAN)
    assert outcome.status == "ok"
    assert outcome.data["request_status"] == "accepted"
    assert outcome.data["status"] == "requested_unconfirmed"
    assert outcome.data["confirmed"] is False and outcome.data["delivery_verified"] is False
    assert post_bodies(fake_http) == [
        {
            "entity_id": "tts.local_voice",
            "media_player_entity_id": "media_player.lounge",
            "message": "The synthetic timer finished.",
            "cache": False,
        }
    ]
    assert outcome.data["observed_player"]["state"] == "playing"
    assert "The synthetic timer finished." not in json.dumps(outcome.data)


@pytest.mark.parametrize(
    "overrides",
    [
        {"engine": "tts.unapproved"},
        {"entity_id": "media_player.unapproved"},
        {"sensitive": True},
        {"volume_level": 0.8},
    ],
)
def test_announcement_target_privacy_and_requested_volume_fail_before_network(
    home_app: AppFactory, fake_http: FakeHttp, overrides: dict[str, Any]
) -> None:
    app = home_app(configuration(speaking=True))
    assert app.call("home_announce", announcement(app, **overrides), Origin.HUMAN).status == "error"
    assert fake_http.requests == []


@pytest.mark.parametrize(
    "attributes",
    [
        {"volume_level": 0.6},
        {"volume_level": None},
        {"volume_level": 0.4},
        {"is_volume_muted": True},
        {"is_volume_muted": None},
        {"supported_features": 0},
        {"group_members": ["media_player.lounge", "media_player.unrequested"]},
    ],
)
def test_announcements_do_not_change_volume_unmute_or_expand_groups(
    home_app: AppFactory, fake_http: FakeHttp, attributes: dict[str, Any]
) -> None:
    app = home_app(configuration(speaking=True))
    ready(fake_http, **attributes)
    assert app.call("home_announce", announcement(app), Origin.HUMAN).status == "error"
    assert post_bodies(fake_http) == []


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("", "invalid"),
        (" " * 10, "error"),
        ("<speak>unsafe markup</speak>", "error"),
        ("line one\nline two", "invalid"),
        ("x" * 141, "invalid"),
        ("\u00e9", "invalid"),
    ],
)
def test_announcements_accept_only_bounded_plain_ascii_text(
    home_app: AppFactory, fake_http: FakeHttp, text: str, status: str
) -> None:
    app = home_app(configuration(speaking=True))
    assert app.call("home_announce", announcement(app, text=text), Origin.HUMAN).status == status
    assert fake_http.requests == []


@pytest.mark.parametrize(
    "key", ["area_id", "device_id", "target", "url", "media_content_id", "options", "command", "cache"]
)
def test_speech_rejects_retargeting_media_addresses_engine_options_and_commands(
    home_app: AppFactory, fake_http: FakeHttp, key: str
) -> None:
    app = home_app(configuration(speaking=True))
    assert (
        app.call("home_announce", announcement(app, **{key: "unapproved"}), Origin.HUMAN).status == "invalid"
    )
    assert fake_http.requests == []


def test_quiet_hours_apply_when_the_announcement_would_execute(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = home_app(configuration(speaking=True))
    monkeypatch.setattr(speech, "now", lambda: MOMENT + timedelta(hours=11))
    assert app.call("home_announce", announcement(app), Origin.HUMAN).status == "error"
    assert fake_http.requests == []


def test_quiet_hours_are_checked_again_after_preflight_network_reads(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = home_app(configuration(speaking=True))
    clock = iter([MOMENT, MOMENT + timedelta(hours=11)])
    monkeypatch.setattr(speech, "now", lambda: next(clock))
    ready(fake_http)
    outcome = app.call("home_announce", announcement(app), Origin.HUMAN)
    assert outcome.status == "error" and "started during preflight" in outcome.message
    assert post_bodies(fake_http) == []


@pytest.mark.parametrize(
    ("start", "end", "hour", "minute", "active"),
    [
        ("22:00", "07:00", 22, 0, True),
        ("22:00", "07:00", 6, 59, True),
        ("22:00", "07:00", 7, 0, False),
        ("22:00", "07:00", 21, 59, False),
        ("12:00", "14:00", 12, 0, True),
        ("12:00", "14:00", 14, 0, False),
    ],
)
def test_quiet_hour_boundaries_handle_overnight_and_same_day_windows(
    start: str, end: str, hour: int, minute: int, active: bool
) -> None:
    quiet = QuietHours(True, "UTC", start, end)
    assert quiet.active(datetime(2026, 11, 11, hour, minute, tzinfo=UTC)) is active


@pytest.mark.parametrize("hour", [8, 9])
def test_repeated_daylight_saving_hour_remains_quiet_in_the_configured_zone(hour: int) -> None:
    quiet = QuietHours(True, "America/Los_Angeles", "01:00", "02:00")
    assert quiet.active(datetime(2026, 11, 1, hour, 30, tzinfo=UTC))


def test_declared_external_processing_needs_explicit_allowance_and_is_visible_in_approval(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration(speaking=True)
    config["connectors"]["home"]["speech"].update(external_processing=True, allow_external=True)
    app = home_app(config)
    assert app.call("home_announce", announcement(app), Origin.ASSISTANT).status == "pending"
    [pending] = app.approvals.list("pending")
    assert "external=yes" in pending.summary
    assert fake_http.requests == []


def test_sensitive_local_speech_requires_the_explicit_sensitive_opt_in(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration(speaking=True)
    config["connectors"]["home"]["speech"]["allow_sensitive"] = True
    app = home_app(config)
    ready(fake_http)
    add_post(fake_http, "tts", "speak")
    add_state(fake_http, "media_player.lounge")
    assert (
        app.call("home_announce", announcement(app, sensitive=True), Origin.HUMAN).data["confirmed"] is False
    )
    assert len(post_bodies(fake_http)) == 1


def test_ambiguous_speech_is_not_resent_even_when_the_speaker_reports_playing(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(speaking=True))
    ready(fake_http)
    fake_http.add_error("POST", f"{HA}/api/services/tts/speak")
    add_state(fake_http, "media_player.lounge", state="playing")
    result = app.call("home_announce", announcement(app), Origin.HUMAN).data
    assert result["request_status"] == "unknown"
    assert result["status"] == "requested_unconfirmed"
    assert result["delivery_verified"] is False
    assert len(post_bodies(fake_http)) == 1


def test_speech_readback_failure_does_not_trigger_a_second_announcement(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(speaking=True))
    ready(fake_http)
    add_post(fake_http, "tts", "speak")
    fake_http.add_error("GET", f"{HA}/api/states/media_player.lounge")
    result = app.call("home_announce", announcement(app), Origin.HUMAN).data
    assert result["readback_error"] is not None
    assert len(post_bodies(fake_http)) == 1


def test_speech_does_not_blindly_restore_volume_after_someone_changes_it(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(speaking=True))
    ready(fake_http)
    add_post(fake_http, "tts", "speak")
    add_state(fake_http, "media_player.lounge", volume_level=0.4)
    result = app.call("home_announce", announcement(app), Origin.HUMAN).data
    assert result["warning"] is not None
    assert len(post_bodies(fake_http)) == 1


def test_speech_token_is_still_lazy_and_missing_credentials_send_nothing(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = home_app(configuration(speaking=True))
    monkeypatch.delenv("SB_HOME_EXTENSION_TOKEN")
    assert app.call("home_announce", announcement(app), Origin.HUMAN).status == "error"
    assert fake_http.requests == []

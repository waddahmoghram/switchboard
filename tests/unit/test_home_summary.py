"""UNRUN sensor summaries: original units, explicit unknowns and honest cache freshness."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from switchboard.connectors.home import poll
from switchboard.policy import Origin
from tests.fakes import FakeHttp
from tests.unit.test_home_support import (
    HA,
    MOMENT,
    AppFactory,
    add_states,
    configuration,
    sample,
)

pytest_plugins = ["tests.unit.test_home_support"]


def test_sensor_summary_preserves_units_and_keeps_energy_counters_separate(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_states(fake_http)
    outcome = app.call("home_sensor_summary", {}, Origin.ASSISTANT)
    assert outcome.status == "ok"
    readings = outcome.data["readings"]
    assert readings["temperature"][0]["value"] == 19.5
    assert readings["temperature"][0]["unit"] == "\u00b0C"
    assert readings["humidity"][0]["value"] == 47
    assert readings["humidity"][0]["unit"] == "%"
    assert readings["power"][0]["value"] == 0.75
    assert readings["power"][0]["unit"] == "kW"
    assert [(item["value"], item["unit"]) for item in readings["energy"]] == [(125.5, "kWh"), (4200, "J")]
    assert readings["energy"][0]["state_class"] == "total_increasing"
    assert "total" not in outcome.data
    assert "cost" not in outcome.data
    assert "fixture-private-attribute" not in json.dumps(outcome.data)


def test_binary_motion_leak_and_low_battery_have_explicit_reported_meanings(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_states(fake_http)
    readings = app.call("home_sensor_summary", {}, Origin.ASSISTANT).data["readings"]
    assert readings["motion"][0]["value"] is True
    assert readings["motion"][0]["meaning"] == "motion_detected"
    assert readings["leak"][0]["value"] is False
    assert readings["leak"][0]["meaning"] == "moisture_detected"
    battery = {item["entity_id"]: item for item in readings["battery"]}
    assert battery["binary_sensor.battery"]["value"] is True
    assert battery["binary_sensor.battery"]["meaning"] == "low_battery"
    assert battery["binary_sensor.battery"]["unit"] is None
    assert battery["sensor.battery"]["value"] == 82
    assert battery["sensor.battery"]["unit"] == "%"


def test_device_names_do_not_invent_sensor_capabilities(home_app: AppFactory, fake_http: FakeHttp) -> None:
    app = home_app(configuration())
    add_states(fake_http, [sample("sensor.temperature", device_class=None)])
    result = app.call("home_sensor_summary", {}, Origin.ASSISTANT).data
    assert result["readings"]["temperature"] == []
    assert result["unclassified"][0]["entity_id"] == "sensor.temperature"


@pytest.mark.parametrize(
    ("state", "attributes", "reason"),
    [
        ("NaN", {}, "no finite"),
        ("Infinity", {}, "no finite"),
        ("not-a-number", {}, "no finite"),
        ("20", {"unit_of_measurement": None}, "unit missing"),
        ("20", {"unit_of_measurement": "unrecognized"}, "unit missing"),
    ],
)
def test_invalid_values_and_unknown_units_are_not_usable_measurements(
    home_app: AppFactory, fake_http: FakeHttp, state: str, attributes: dict[str, Any], reason: str
) -> None:
    app = home_app(configuration())
    add_states(fake_http, [sample("sensor.temperature", state=state, **attributes)])
    [reading] = app.call("home_sensor_summary", {}, Origin.ASSISTANT).data["readings"]["temperature"]
    assert reading["usable"] is False
    assert reason in reading["reason"]


@pytest.mark.parametrize("entity_id", ["sensor.humidity", "sensor.battery"])
def test_out_of_range_percentages_are_not_silently_normalized(
    home_app: AppFactory, fake_http: FakeHttp, entity_id: str
) -> None:
    app = home_app(configuration())
    add_states(fake_http, [sample(entity_id, state="125")])
    result = app.call("home_sensor_summary", {}, Origin.ASSISTANT).data
    kind = "humidity" if entity_id == "sensor.humidity" else "battery"
    [reading] = result["readings"][kind]
    assert reading["value"] == 125
    assert reading["usable"] is False
    assert "outside" in reading["reason"]


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_unavailable_readings_never_become_zero_or_false(
    home_app: AppFactory, fake_http: FakeHttp, state: str
) -> None:
    app = home_app(configuration())
    add_states(
        fake_http, [sample("sensor.temperature", state=state), sample("binary_sensor.leak", state=state)]
    )
    result = app.call("home_sensor_summary", {}, Origin.ASSISTANT).data["readings"]
    for kind in ("temperature", "leak"):
        assert result[kind][0]["value"] is None
        assert result[kind][0]["availability"] == state
        assert result[kind][0]["usable"] is False


def test_room_sensor_summary_only_returns_explicit_room_members(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(rooms={"thermometer": ["sensor.temperature"]}))
    add_states(fake_http)
    result = app.call("home_sensor_summary", {"room": "thermometer"}, Origin.ASSISTANT).data
    assert [item["entity_id"] for items in result["readings"].values() for item in items] == [
        "sensor.temperature"
    ]


def test_kind_and_total_limit_apply_without_adding_unreported_capabilities(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_states(fake_http)
    result = app.call("home_sensor_summary", {"kind": "energy", "limit": 1}, Origin.ASSISTANT).data
    assert len(result["readings"]["energy"]) == 1
    assert all(not items for kind, items in result["readings"].items() if kind != "energy")
    assert result["truncated"] is True


def test_hidden_or_wrong_room_sensor_requests_fail_before_network(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(rooms={"only_motion": ["binary_sensor.motion"]}))
    for args in (
        {"entity_id": "sensor.not_exposed"},
        {"entity_id": "sensor.temperature", "room": "only_motion"},
        {"room": "not_configured"},
        {"entity_id": "climate.lounge"},
    ):
        assert app.call("home_sensor_summary", args, Origin.ASSISTANT).status == "error"
    assert fake_http.requests == []


def test_live_reads_do_not_use_old_last_updated_as_an_offline_detector(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    add_states(fake_http, [sample("sensor.temperature")])
    [reading] = app.call("home_sensor_summary", {}, Origin.ASSISTANT).data["readings"]["temperature"]
    assert reading["last_updated"].startswith("2026-01-01")
    assert reading["stale"] is False
    assert reading["usable"] is True


def test_cached_sensor_reads_become_stale_without_network_or_fabricated_events(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    monkeypatch.setattr(poll, "now", lambda: MOMENT + timedelta(seconds=301))
    result = app.call("home_sensor_summary", {"source": "cached"}, Origin.ASSISTANT).data
    [reading] = result["readings"]["temperature"]
    assert reading["value"] == 19.5
    assert reading["stale"] is True
    assert reading["usable"] is False
    assert len(fake_http.requests) == 1
    assert app.ctx.events.recent(source="home") == []


def test_cached_poll_failure_is_reported_even_before_the_age_threshold(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    fake_http.add_error("GET", f"{HA}/api/states")
    app.call("home_poll", {}, Origin.ASSISTANT)
    result = app.call("home_sensor_summary", {"source": "cached"}, Origin.ASSISTANT).data
    assert result["source_error"] is not None
    assert result["readings"]["temperature"][0]["stale"] is False
    assert result["readings"]["temperature"][0]["usable"] is False


def test_a_live_failure_does_not_silently_substitute_old_cached_data(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    fake_http.add_error("GET", f"{HA}/api/states")
    assert app.call("home_sensor_summary", {"source": "live"}, Origin.ASSISTANT).status == "error"


def test_cached_reads_require_polling_and_the_requested_entity_to_be_polled(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration())
    assert app.call("home_sensor_summary", {"source": "cached"}, Origin.ASSISTANT).status == "error"
    app = home_app(configuration(polling=True))
    outcome = app.call(
        "home_sensor_summary",
        {
            "source": "cached",
            "entity_id": "sensor.humidity",
        },
        Origin.ASSISTANT,
    )
    assert outcome.status == "error"
    assert fake_http.requests == []


def test_unobserved_cached_sensors_are_reported_without_inventing_their_device_class(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    result = app.call("home_sensor_summary", {"source": "cached"}, Origin.ASSISTANT).data
    assert all(not items for items in result["readings"].values())
    assert {item["availability"] for item in result["unclassified"]} == {"not_observed"}
    assert fake_http.requests == []

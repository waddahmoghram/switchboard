"""Home Assistant tools: exposure filtering, denied domains, request shapes, and allow_http."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors import home
from switchboard.errors import ConfigError
from switchboard.policy import Origin, Tier
from tests.fakes import FakeHttp

AppFactory = Callable[..., App]

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "home"
STATES = (FIXTURES / "states.json").read_text(encoding="utf-8")
HA = "https://ha.example.test"
TOKEN = "test-ha-token"


def home_config(**overrides: Any) -> dict[str, Any]:
    table: dict[str, Any] = {
        "base_url": HA,
        "token": "env:SB_TEST_HA_TOKEN",
        "expose": ["light.*", "climate.*", "sensor.*"],
    }
    table.update(overrides)
    return {"switchboard": {"connectors": ["home"]}, "connectors": {"home": table}}


@pytest.fixture(autouse=True)
def token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SB_TEST_HA_TOKEN", TOKEN)


def visible_ids(app: App, fake_http: FakeHttp, args: dict[str, Any] | None = None) -> list[str]:
    fake_http.add("GET", f"{HA}/api/states", body=STATES)
    outcome = app.call("home_states", args or {}, Origin.ASSISTANT)
    assert outcome.status == "ok", outcome.message
    return [entity["entity_id"] for entity in outcome.data["entities"]]


def test_tool_tiers(make_app: AppFactory) -> None:
    app = make_app(home_config())
    states = app.registry.get("home_states")
    call = app.registry.get("home_call_service")
    assert states is not None and states.tier is Tier.READ
    assert call is not None and call.tier is Tier.ACT


def test_states_show_only_exposed_entities_with_minimal_attributes(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(home_config())
    fake_http.add("GET", f"{HA}/api/states", body=STATES)

    outcome = app.call("home_states", {}, Origin.ASSISTANT)

    assert outcome.status == "ok"
    [request] = fake_http.requests
    assert (request.method, request.url) == ("GET", f"{HA}/api/states")
    assert request.headers == {"authorization": f"Bearer {TOKEN}"}
    assert request.body is None
    entities = outcome.data["entities"]
    assert [entity["entity_id"] for entity in entities] == [
        "climate.living_room",
        "light.kitchen",
        "light.porch",
        "sensor.outdoor_temperature",
    ]
    assert entities[1] == {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen", "brightness": 180},
        "last_changed": "2026-11-10T18:02:11+00:00",
    }
    assert entities[0]["attributes"] == {
        "friendly_name": "Living room",
        "current_temperature": 20.5,
        "temperature": 21,
        "hvac_action": "heating",
    }
    assert outcome.data["truncated"] is False


def test_denied_domains_stay_hidden_even_when_everything_is_exposed(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(home_config(expose=["*"]))

    assert visible_ids(app, fake_http) == [
        "climate.living_room",
        "light.kitchen",
        "light.porch",
        "sensor.outdoor_temperature",
        "switch.coffee_maker",
    ]


def test_overriding_deny_domains_replaces_the_default_list(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(home_config(expose=["cover.*", "lock.*"], deny_domains=["lock"]))

    assert visible_ids(app, fake_http) == ["cover.garage_door"]


def test_camera_tokens_never_reach_the_assistant(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(home_config(expose=["camera.*"], deny_domains=[]))
    fake_http.add("GET", f"{HA}/api/states", body=STATES)

    outcome = app.call("home_states", {}, Origin.ASSISTANT)

    assert [entity["entity_id"] for entity in outcome.data["entities"]] == ["camera.driveway"]
    assert "sb-test-camera-token" not in json.dumps(outcome.data)


def test_states_filter_by_domain_and_limit(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(home_config())
    fake_http.add("GET", f"{HA}/api/states", body=STATES)

    outcome = app.call("home_states", {"domain": "light", "limit": 1}, Origin.ASSISTANT)

    assert [entity["entity_id"] for entity in outcome.data["entities"]] == ["light.kitchen"]
    assert outcome.data["truncated"] is True


def test_one_exposed_entity_is_read_from_its_own_endpoint(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(home_config())
    sensor = next(item for item in json.loads(STATES) if item["entity_id"] == "sensor.outdoor_temperature")
    fake_http.add("GET", f"{HA}/api/states/sensor.outdoor_temperature", body=json.dumps(sensor))

    outcome = app.call("home_states", {"entity_id": "sensor.outdoor_temperature"}, Origin.ASSISTANT)

    assert outcome.status == "ok"
    [entity] = outcome.data["entities"]
    assert entity["state"] == "12.4"
    assert entity["attributes"] == {
        "friendly_name": "Outdoor temperature",
        "device_class": "temperature",
        "unit_of_measurement": "\u00b0C",
    }


@pytest.mark.parametrize("entity_id", ["switch.coffee_maker", "lock.front_door"])
def test_hidden_entities_cannot_be_read(make_app: AppFactory, fake_http: FakeHttp, entity_id: str) -> None:
    app = make_app(home_config())

    outcome = app.call("home_states", {"entity_id": entity_id}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert outcome.message == f"{entity_id} is not exposed to assistants"
    assert fake_http.requests == []


def test_call_service_posts_the_entity_and_data(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(home_config())
    changed = [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen", "brightness": 102},
        },
        {"entity_id": "switch.coffee_maker", "state": "on", "attributes": {}},
    ]
    fake_http.add("POST", f"{HA}/api/services/light/turn_on", body=json.dumps(changed))
    args = {
        "domain": "light",
        "service": "turn_on",
        "entity_id": "light.kitchen",
        "data": {"brightness_pct": 40},
    }

    outcome = app.call("home_call_service", args, Origin.HUMAN)

    assert outcome.status == "ok"
    [request] = fake_http.requests
    assert (request.method, request.url) == ("POST", f"{HA}/api/services/light/turn_on")
    assert request.headers == {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"}
    assert json.loads(request.body or b"") == {"entity_id": "light.kitchen", "brightness_pct": 40}
    assert outcome.data == {
        "called": "light.turn_on",
        "entity_id": "light.kitchen",
        "changed": [
            {
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"friendly_name": "Kitchen", "brightness": 102},
                "last_changed": None,
            }
        ],
    }


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"},
            "lock entities are blocked",
        ),
        (
            {"domain": "cover", "service": "open_cover", "entity_id": "cover.garage_door"},
            "cover entities are",
        ),
        ({"domain": "switch", "service": "turn_on", "entity_id": "switch.coffee_maker"}, "not exposed"),
        (
            {"domain": "homeassistant", "service": "restart", "entity_id": "light.kitchen"},
            "domain must be light",
        ),
        (
            {"domain": "hassio", "service": "host_shutdown", "entity_id": "light.kitchen"},
            "domain must be light",
        ),
    ],
)
def test_call_service_refuses_blocked_targets(
    make_app: AppFactory, fake_http: FakeHttp, args: dict[str, Any], message: str
) -> None:
    app = make_app(home_config())

    outcome = app.call("home_call_service", args, Origin.HUMAN)

    assert outcome.status == "error"
    assert message in outcome.message
    assert fake_http.requests == []


def test_the_schema_refuses_an_entity_id_with_a_trailing_newline(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(home_config())
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen\n"}

    outcome = app.call("home_call_service", args, Origin.HUMAN)

    assert outcome.status == "invalid"
    assert fake_http.requests == []


@pytest.mark.parametrize(
    "data",
    [{"entity_id": "lock.front_door"}, {"area_id": "garage"}, {"brightness_pct": 400}, {"code": "1234"}],
)
def test_call_service_data_cannot_retarget_or_exceed_bounds(
    make_app: AppFactory, fake_http: FakeHttp, data: dict[str, Any]
) -> None:
    app = make_app(home_config())
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen", "data": data}

    outcome = app.call("home_call_service", args, Origin.HUMAN)

    assert outcome.status == "invalid"
    assert fake_http.requests == []


def test_assistant_calls_wait_for_approval_with_a_clear_summary(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(home_config())
    args = {"domain": "light", "service": "turn_off", "entity_id": "light.porch", "data": {"transition": 2}}

    outcome = app.call("home_call_service", args, Origin.ASSISTANT)

    assert outcome.status == "pending"
    [pending] = app.approvals.list("pending")
    assert pending.summary == 'Home Assistant light.turn_off on light.porch with {"transition":2}'
    assert fake_http.requests == []


def test_plain_http_needs_allow_http(make_app: AppFactory) -> None:
    with pytest.raises(ConfigError, match="allow_http = true"):
        make_app(home_config(base_url="http://homeassistant.local:8123"))


def test_allow_http_reaches_a_server_on_your_own_network(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(home_config(base_url="http://homeassistant.local:8123/", allow_http=True))
    fake_http.add("GET", "http://homeassistant.local:8123/api/states", body="[]")

    outcome = app.call("home_states", {}, Origin.ASSISTANT)

    assert outcome.status == "ok"
    assert outcome.data == {"entities": [], "truncated": False}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"token": "eyJhbGciOiJIUzI1NiJ9.sb-test"}, "token must be a secret reference"),
        ({"base_url": "https://user:pw@ha.example.test"}, "without credentials"),
        ({"base_url": None}, "base_url must be"),
        ({"deny_domains": ["Lock!"]}, "deny_domains must list"),
        ({"expose": "light.*"}, "expose must list"),
        ({"allow_http": "yes"}, "allow_http must be true or false"),
    ],
)
def test_bad_settings_fail_at_startup(make_app: AppFactory, overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        make_app(home_config(**overrides))


def test_a_refused_token_is_reported_without_the_token(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(home_config())
    fake_http.add("GET", f"{HA}/api/states", status=401, body="401: Unauthorized")

    outcome = app.call("home_states", {}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert outcome.message == "Home Assistant refused the access token (HTTP 401)"


def test_doctor_reports_the_token_and_an_empty_expose_list(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SB_TEST_HA_TOKEN")
    app = make_app(home_config(expose=[]))

    assert home.doctor(app.ctx) == [
        "token: environment variable 'SB_TEST_HA_TOKEN' is not set",
        "expose is empty, so the tools can see no entities",
    ]

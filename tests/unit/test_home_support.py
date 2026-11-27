"""Synthetic helpers for the home tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors.home import controls, poll, service, speech
from switchboard.policy import Origin
from tests.fakes import FakeHttp

AppFactory = Callable[..., App]
HA = "https://home.example.test"
MOMENT = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "home"
POLL_ENTITIES = ("light.lounge", "sensor.temperature", "binary_sensor.leak")


@pytest.fixture
def home_app(make_app: AppFactory, monkeypatch: pytest.MonkeyPatch) -> AppFactory:
    monkeypatch.setenv("SB_HOME_EXTENSION_TOKEN", "synthetic-home-token")
    for module in (controls, poll, service, speech):
        monkeypatch.setattr(module, "now", lambda: MOMENT)
    return make_app


def configuration(*, polling: bool = False, speaking: bool = False, **overrides: Any) -> dict[str, Any]:
    members = [item["entity_id"] for item in samples() if not item["entity_id"].startswith("tts.")]
    table: dict[str, Any] = {
        "base_url": HA,
        "token": "env:SB_HOME_EXTENSION_TOKEN",
        "expose": [*members, "tts.local_voice"],
        "lights": ["light.lounge", "light.desk"],
        "rooms": {"lounge": members},
        "climate": {
            "climate.lounge": {
                "unit": "C",
                "minimum": 16,
                "maximum": 26,
                "max_change": 3,
                "modes": ["off", "heat", "cool", "heat_cool"],
            },
        },
        "fans": {"fan.lounge": {"minimum": 0, "maximum": 80, "max_change": 50}},
        "media_players": {"media_player.lounge": {"minimum": 0, "maximum": 0.5}},
        "quiet_hours": {"enabled": True, "timezone": "UTC", "start": "22:00", "end": "07:00"},
        "poll": {"enabled": polling, "entities": list(POLL_ENTITIES)},
        "lighting_presets": {
            "reading": {
                "actions": [
                    {"entity_id": "light.lounge", "state": "on", "brightness_pct": 40},
                    {"entity_id": "light.desk", "state": "on", "brightness_pct": 50},
                ],
            },
        },
    }
    if speaking:
        table["speech"] = {
            "enabled": True,
            "engine": "tts.local_voice",
            "destinations": ["media_player.lounge"],
            "external_processing": False,
        }
    table.update(overrides)
    return {
        "switchboard": {"connectors": ["home"]},
        "connectors": {"home": table},
        "policy": {"tools": {"home_announce": {"decision": "ask"}}},
    }


def samples() -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = json.loads((FIXTURES / "extended_states.json").read_text(encoding="ascii"))
    return states


def sample(identifier: str, *, state: str | None = None, **attributes: Any) -> dict[str, Any]:
    item = next(item for item in samples() if item["entity_id"] == identifier)
    if state is not None:
        item["state"] = state
    item["attributes"].update(attributes)
    return item


def add_state(
    fake_http: FakeHttp, identifier: str, *, state: str | None = None, repeat: bool = False, **attributes: Any
) -> None:
    fake_http.add(
        "GET",
        f"{HA}/api/states/{identifier}",
        body=json.dumps(sample(identifier, state=state, **attributes)),
        repeat=repeat,
    )


def add_states(fake_http: FakeHttp, states: list[dict[str, Any]] | None = None) -> None:
    fake_http.add("GET", f"{HA}/api/states", body=json.dumps(samples() if states is None else states))


def add_post(fake_http: FakeHttp, domain: str, action: str, *, status: int = 200, body: str = "[]") -> None:
    fake_http.add("POST", f"{HA}/api/services/{domain}/{action}", status=status, body=body)


def action_args(app: App, **args: Any) -> dict[str, Any]:
    outcome = app.call("home_targets", {}, Origin.ASSISTANT)
    assert outcome.status == "ok", outcome.message
    return {"binding": outcome.data["binding"], **args}


def post_bodies(fake_http: FakeHttp) -> list[dict[str, Any]]:
    return [json.loads(request.body or b"{}") for request in fake_http.requests if request.method == "POST"]

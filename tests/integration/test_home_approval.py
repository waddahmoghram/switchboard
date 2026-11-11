"""An assistant's home_call_service waits for approval, then runs exactly once."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from switchboard.app import App
from switchboard.policy import Origin
from tests.fakes import FakeHttp

pytestmark = pytest.mark.integration

AppFactory = Callable[..., App]
HA = "https://ha.example.test"


def test_assistant_call_runs_once_after_approval(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_HA_TOKEN", "test-ha-token")
    app = make_app(
        {
            "switchboard": {"connectors": ["home"]},
            "connectors": {
                "home": {"base_url": HA, "token": "env:SB_TEST_HA_TOKEN", "expose": ["climate.*"]},
            },
        }
    )
    fake_http.add("POST", f"{HA}/api/services/climate/set_temperature", body="[]")
    args = {
        "domain": "climate",
        "service": "set_temperature",
        "entity_id": "climate.living_room",
        "data": {"temperature": 20.5},
    }

    outcome = app.call("home_call_service", args, Origin.ASSISTANT)

    assert outcome.status == "pending"
    assert outcome.approval_id is not None
    assert fake_http.requests == []

    approved = app.dispatcher.approve(outcome.approval_id)

    assert approved.status == "ok", approved.message
    posts = [request for request in fake_http.requests if request.method == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0].body or b"") == {"entity_id": "climate.living_room", "temperature": 20.5}

    again = app.dispatcher.approve(outcome.approval_id)

    assert again.status == "error"
    assert len(fake_http.requests) == 1

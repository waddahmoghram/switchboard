"""On-demand status, fixed targets and observed control outcomes. Synthetic and UNRUN."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors import shelly
from switchboard.connectors.shelly.client import ShellyError
from switchboard.connectors.shelly.service import ShellyTools, minimal_status
from switchboard.connectors.shelly.settings import load_settings
from switchboard.context import AppContext
from switchboard.errors import ToolError
from switchboard.httpclient import HttpResponse
from switchboard.policy import Origin, Tier
from tests.conftest import AppFactory
from tests.fakes import FakeHttp, RecordedRequest
from tests.fixtures.shelly import BASE, PASSWORD, accepted, config, fixture, identity, methods, status


def control_args(app: App, *, on: bool = True) -> dict[str, Any]:
    listing = app.call("shelly_devices", {}, Origin.ASSISTANT)
    [device] = listing.data["devices"]
    [control] = device["controls"]
    return {
        "device": device["device"],
        "switch_id": control["switch_id"],
        "target": control["target"],
        "on": on,
    }


def test_offline_registration_and_doctor_do_not_resolve_passwords_or_contact_devices(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_ctx: AppContext, _reference: str) -> str:
        raise AssertionError("offline registration must not resolve secrets")

    monkeypatch.setattr(AppContext, "secret", forbidden)
    app = make_app(config(password=True))
    assert shelly.doctor(app.ctx) == []
    assert fake_http.requests == []
    assert shelly.MIGRATIONS == []
    assert not any(job.name.startswith("shelly.") for job in app.ctx.scheduler.jobs())
    assert not any(name.startswith("shelly.") for name in app.ctx.services)
    assert app.ctx.events.recent(source="shelly") == []


def test_read_only_default_omits_the_control_tool(make_app: AppFactory) -> None:
    app = make_app(config())
    assert app.registry.get("shelly_switch_set") is None
    for name in ("shelly_devices", "shelly_status"):
        tool = app.registry.get(name)
        assert tool is not None
        assert tool.tier is Tier.READ
        assert tool.connector == "shelly"


def test_opted_in_control_uses_the_action_tier_and_has_an_explicit_summary(make_app: AppFactory) -> None:
    app = make_app(config(controls=True))
    tool = app.registry.get("shelly_switch_set")
    assert tool is not None
    assert tool.tier is Tier.ACT
    assert tool.summarize is not None
    summary = tool.summarize(control_args(app, on=False))
    assert "desk_lamp switch:0 OFF" in summary
    assert "<= 60 W" in summary
    assert "nonessential lighting" in summary


def test_device_inventory_is_configuration_not_health_and_omits_sensitive_endpoints(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True, password=True))
    outcome = app.call("shelly_devices", {}, Origin.ASSISTANT)
    assert outcome.status == "ok"
    assert outcome.data["configured_only"] is True
    assert outcome.data["live_validation"] is False
    [device] = outcome.data["devices"]
    assert device["authentication_configured"] is True
    assert device["switch_ids"] == [0]
    assert device["transport"] == "http"
    assert len(device["controls"][0]["target"]) == 64
    assert "192.168" not in json.dumps(outcome.data)
    assert "env:" not in json.dumps(outcome.data)
    assert fake_http.requests == []


def test_status_returns_only_requested_component_state_and_correct_units(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config())
    identity(fake_http)
    status(fake_http, output=True)
    outcome = app.call("shelly_status", {"device": "desk_lamp", "switch_id": 0}, Origin.ASSISTANT)
    assert outcome.status == "ok"
    data = outcome.data
    assert data["output"] is True
    assert data["cached"] is False
    assert data["measurement_age_seconds"] is None
    assert datetime.fromisoformat(data["observed_at"]).tzinfo is not None
    assert data["power"] == {"available": True, "value": 6.5, "unit": "W"}
    assert data["energy"] == {
        "available": True,
        "value": 123.25,
        "unit": "Wh",
        "kind": "device_cumulative_counter",
    }
    assert data["voltage"]["unit"] == "V"
    assert data["current"]["unit"] == "A"
    assert data["returned_energy"]["value"] == 0
    assert "by_minute" not in json.dumps(data)
    assert "synthetic-" not in json.dumps(data)
    assert app.ctx.events.recent(source="shelly") == []


def test_unmetered_components_report_unavailable_measurements_not_zeroes() -> None:
    data = minimal_status(fixture("switch_unmetered.json"), 0)
    assert data["output"] is True
    for name in ("power", "energy", "returned_energy", "voltage", "current"):
        assert data[name]["available"] is False
        assert data[name]["value"] is None
    assert data["errors"] == []


def test_unrecognized_provider_error_text_is_not_returned() -> None:
    raw = fixture("switch_status.json")
    raw["errors"] = ["overtemp", "synthetic-private-provider-value", "overtemp"]
    data = minimal_status(raw, 0)
    assert data["errors"] == ["overtemp", "unrecognized_error"]
    assert "synthetic-private" not in json.dumps(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 1},
        {"id": True},
        {"output": "on"},
        {"apower": True},
        {"apower": float("nan")},
        {"apower": float("inf")},
        {"apower": 1_000_001},
        {"aenergy": {"total": -1}},
        {"aenergy": {"total": "10"}},
        {"aenergy": {"total": 1_000_000_000_001}},
        {"aenergy": []},
        {"ret_aenergy": {"total": float("-inf")}},
        {"voltage": -1},
        {"current": "0.1"},
        {"errors": "overtemp"},
        {"errors": ["x" * 65]},
        {"errors": [False]},
        {"errors": ["overtemp"] * 17},
    ],
)
def test_malformed_status_is_not_partial_or_empty_success(changes: dict[str, Any]) -> None:
    raw = fixture("switch_status.json")
    raw.update(changes)
    with pytest.raises(ShellyError):
        minimal_status(raw, 0)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"device": "unknown", "switch_id": 0},
        {"device": "desk_lamp", "switch_id": 1},
        {"device": BASE, "switch_id": 0},
        {"device": "desk_lamp", "switch_id": True},
        {"device": "desk_lamp", "switch_id": 0, "url": "https://elsewhere.example.test"},
        {"device": "desk_lamp", "switch_id": 0, "method": "Shelly.GetConfig"},
    ],
)
def test_status_schema_rejects_unconfigured_or_injected_targets_before_network(
    make_app: AppFactory, fake_http: FakeHttp, args: dict[str, Any]
) -> None:
    app = make_app(config())
    outcome = app.call("shelly_status", args, Origin.ASSISTANT)
    assert outcome.status == "invalid"
    assert fake_http.requests == []


def test_cross_device_component_union_does_not_expose_unlisted_components(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    data = config()
    second = deepcopy(data["connectors"]["shelly"]["devices"]["desk_lamp"])
    second.update(
        {
            "base_url": "http://192.168.10.41",
            "expected_device_id": "shellypro4pm-000000000002",
            "expected_model": "SPSW-004PE16EU",
            "switch_ids": [1],
        }
    )
    data["connectors"]["shelly"]["devices"]["other"] = second
    app = make_app(data)
    outcome = app.call("shelly_status", {"device": "desk_lamp", "switch_id": 1}, Origin.ASSISTANT)
    assert outcome.status == "error"
    assert fake_http.requests == []


@pytest.mark.parametrize("on", [True, False])
def test_approved_absolute_output_has_one_set_and_one_readback(
    make_app: AppFactory, fake_http: FakeHttp, on: bool
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http, output=not on)
    accepted(fake_http, on=on, was_on=not on)
    status(fake_http, output=on)
    outcome = app.call("shelly_switch_set", control_args(app, on=on), Origin.HUMAN)
    assert outcome.status == "ok"
    assert outcome.data["ok"] is True
    assert outcome.data["status"] == "confirmed"
    assert outcome.data["command_accepted"] is True
    assert outcome.data["matches_requested"] is True
    assert outcome.data["was_on"] is not on
    assert outcome.data["observation"]["output"] is on
    assert outcome.data["automatic_resend"] is False
    assert methods(fake_http) == [
        "Shelly.GetDeviceInfo",
        "Switch.GetStatus",
        "Switch.Set",
        "Switch.GetStatus",
    ]


@pytest.mark.parametrize(
    "changes", [{"apower": 61}, {"apower": -1}, {"apower": None}, {"errors": ["overtemp"]}]
)
def test_safe_power_and_error_preflight_is_required_even_for_turning_off(
    make_app: AppFactory, fake_http: FakeHttp, changes: dict[str, Any]
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http, output=True, **changes)
    outcome = app.call("shelly_switch_set", control_args(app, on=False), Origin.HUMAN)
    assert outcome.status == "error"
    assert methods(fake_http) == ["Shelly.GetDeviceInfo", "Switch.GetStatus"]


def test_command_acceptance_does_not_claim_output_changed_when_readback_differs(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)
    accepted(fake_http)
    status(fake_http)
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert outcome.status == "ok"
    assert outcome.data["ok"] is False
    assert outcome.data["command_accepted"] is True
    assert outcome.data["matches_requested"] is False
    assert outcome.data["status"] == "unconfirmed"
    assert methods(fake_http).count("Switch.Set") == 1


def test_matching_output_after_write_timeout_is_observation_not_proof_of_acceptance(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)
    fake_http.add_error("POST", BASE + "/rpc", PASSWORD)
    status(fake_http, output=True)
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert outcome.status == "ok"
    assert outcome.data["ok"] is False
    assert outcome.data["status"] == "uncertain"
    assert outcome.data["command_accepted"] is None
    assert outcome.data["matches_requested"] is True
    assert outcome.data["command_error"] == "transport_error"
    assert PASSWORD not in json.dumps(outcome.to_dict())
    assert methods(fake_http).count("Switch.Set") == 1


def test_readback_failure_is_explicit_and_never_uses_preflight_as_current_state(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)
    accepted(fake_http)
    fake_http.add_error("POST", BASE + "/rpc", "synthetic-timeout")
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert outcome.data["command_accepted"] is True
    assert outcome.data["ok"] is False
    assert outcome.data["observation"] is None
    assert outcome.data["matches_requested"] is None
    assert outcome.data["readback_error"] == "transport_error"
    assert methods(fake_http).count("Switch.Set") == 1


def test_errors_on_a_matching_readback_still_prevent_a_confirmed_result(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)
    accepted(fake_http)
    status(fake_http, output=True, errors=["overpower"])
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert outcome.data["command_accepted"] is True
    assert outcome.data["matches_requested"] is True
    assert outcome.data["ok"] is False
    assert outcome.data["observation"]["errors"] == ["overpower"]


@pytest.mark.parametrize("http_status", [302, 401, 403, 429, 500])
def test_write_http_failures_produce_safe_explicit_reports_and_no_blind_resend(
    make_app: AppFactory, fake_http: FakeHttp, http_status: int
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)
    fake_http.add(
        "POST",
        BASE + "/rpc",
        status=http_status,
        body="synthetic-secret-provider-error",
        headers={"Location": "https://elsewhere.example.test/rpc"},
    )
    status(fake_http, output=True)
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert outcome.data["ok"] is False
    assert outcome.data["command_error"] is not None
    assert outcome.data["command_accepted"] is (False if http_status in {401, 403} else None)
    assert "synthetic-secret" not in json.dumps(outcome.to_dict())
    assert methods(fake_http).count("Switch.Set") == 1
    assert all(request.url == BASE + "/rpc" for request in fake_http.requests)


@pytest.mark.parametrize("set_result", [{}, {"was_on": "false"}, {"was_on": 0}])
def test_malformed_acceptance_is_uncertain_even_with_a_matching_readback(
    make_app: AppFactory, fake_http: FakeHttp, set_result: dict[str, Any]
) -> None:
    from tests.fixtures.shelly import respond

    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)
    respond(fake_http, "Switch.Set", set_result)
    status(fake_http, output=True)
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert outcome.data["command_accepted"] is None
    assert outcome.data["ok"] is False
    assert outcome.data["command_error"] == "protocol_error"
    assert methods(fake_http).count("Switch.Set") == 1


def test_rpc_errors_are_not_copied_from_the_provider_or_treated_as_acceptance(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)

    def reject(request: RecordedRequest) -> HttpResponse:
        payload = json.loads(request.body or b"{}")
        frame = {
            "id": payload["id"],
            "src": "shellyplusplugs-000000000001",
            "error": {"code": -105, "message": "synthetic-secret-provider-error"},
        }
        return HttpResponse(200, request.url, body=json.dumps(frame).encode())

    fake_http.add_handler("POST", BASE + "/rpc", reject, repeat=False)
    status(fake_http, output=True)
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert outcome.data["ok"] is False
    assert outcome.data["command_accepted"] is None
    assert outcome.data["command_error"] == "rpc_rejected"
    assert "synthetic-secret" not in json.dumps(outcome.to_dict())
    assert methods(fake_http).count("Switch.Set") == 1


def test_direct_handler_calls_cannot_skip_fixed_selection_or_fingerprint_checks(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    tools = ShellyTools(app.ctx, load_settings(config(controls=True)["connectors"]["shelly"]))
    args = control_args(app)
    for changes in ({"target": "0" * 64}, {"switch_id": 1}, {"on": "toggle"}, {"url": BASE}):
        invalid = {**args, **changes}
        with pytest.raises(ToolError):
            tools.set_output(invalid)
    assert fake_http.requests == []


def test_another_current_control_digest_cannot_retarget_the_selected_device(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    data = config(controls=True)
    second = deepcopy(data["connectors"]["shelly"]["devices"]["desk_lamp"])
    second.update(
        {
            "base_url": "http://192.168.10.41",
            "expected_device_id": "shellyplusplugs-000000000002",
        }
    )
    data["connectors"]["shelly"]["devices"]["other"] = second
    app = make_app(data)
    listing = app.call("shelly_devices", {}, Origin.ASSISTANT).data["devices"]
    first, other = listing
    args = {
        "device": first["device"],
        "switch_id": 0,
        "target": other["controls"][0]["target"],
        "on": True,
    }
    assert app.call("shelly_switch_set", args, Origin.HUMAN).status == "error"
    assert fake_http.requests == []


def test_control_fingerprints_do_not_bind_or_resolve_secret_values(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_ctx: AppContext, _reference: str) -> str:
        raise AssertionError("listing target fingerprints must not resolve secrets")

    monkeypatch.setattr(AppContext, "secret", forbidden)
    monkeypatch.setenv("SB_TEST_SHELLY_PASSWORD", "synthetic-first-value")
    first = make_app(config(controls=True, password=True))
    first_target = control_args(first)["target"]
    monkeypatch.setenv("SB_TEST_SHELLY_PASSWORD", "synthetic-rotated-value")
    second = make_app(config(controls=True, password=True))
    assert control_args(second)["target"] == first_target
    assert fake_http.requests == []


def test_a_confirmed_output_is_a_succeeded_action_with_a_boolean_confirmation(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)
    accepted(fake_http)
    status(fake_http, output=True)
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert (outcome.data["confirmed"], outcome.data["effect_unknown"]) == (True, False)
    assert outcome.action is not None
    assert outcome.action["state"] == "succeeded"


def test_unknown_delivery_is_journaled_as_uncertain_and_blocks_a_blind_repeat(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)
    fake_http.add_error("POST", BASE + "/rpc", "synthetic-timeout")
    status(fake_http, output=True)
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert outcome.status == "ok"
    assert (outcome.data["effect_unknown"], outcome.data["confirmed"]) == (True, False)
    assert outcome.action is not None
    assert outcome.action["state"] == "uncertain"

    repeat = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)

    assert repeat.status == "denied"
    assert outcome.action["id"] in repeat.message
    assert methods(fake_http).count("Switch.Set") == 1


def test_a_refused_command_is_a_definite_unconfirmed_report(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    identity(fake_http)
    status(fake_http)
    fake_http.add("POST", BASE + "/rpc", status=403, body="synthetic refusal")
    status(fake_http)
    outcome = app.call("shelly_switch_set", control_args(app), Origin.HUMAN)
    assert outcome.data["command_accepted"] is False
    assert (outcome.data["effect_unknown"], outcome.data["confirmed"]) == (False, False)
    assert outcome.action is not None
    assert outcome.action["state"] == "succeeded"

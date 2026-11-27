"""Core policy and approval integration with FakeHttp only. Every case is authored and UNRUN."""

from __future__ import annotations

from typing import Any

import pytest

from switchboard.app import KNOWN_CONNECTORS, App
from switchboard.policy import Origin
from tests.conftest import AppFactory
from tests.fakes import FakeHttp
from tests.fixtures.shelly import accepted, config, identity, methods, status

pytestmark = pytest.mark.integration


def action(app: App, *, on: bool = True) -> dict[str, Any]:
    listing = app.call("shelly_devices", {}, Origin.ASSISTANT)
    [device] = listing.data["devices"]
    [control] = device["controls"]
    return {"device": device["device"], "switch_id": 0, "target": control["target"], "on": on}


def test_root_registration_is_explicit_opt_in_not_a_default_connector(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    assert KNOWN_CONNECTORS.get("shelly") == "switchboard.connectors.shelly"
    disabled = make_app({"switchboard": {"connectors": []}})
    assert disabled.registry.get("shelly_devices") is None
    assert disabled.registry.get("shelly_switch_set") is None
    enabled = make_app(config())
    assert "shelly" in enabled.connectors
    assert fake_http.requests == []


@pytest.mark.parametrize("origin", [Origin.ASSISTANT, Origin.RULE])
def test_default_action_policy_queues_the_exact_absolute_control_without_network(
    make_app: AppFactory, fake_http: FakeHttp, origin: Origin
) -> None:
    app = make_app(config(controls=True))
    args = action(app, on=False)
    outcome = app.call("shelly_switch_set", args, origin)
    assert outcome.status == "pending"
    assert outcome.approval_id is not None
    stored = app.approvals.get(outcome.approval_id)
    assert stored is not None
    assert stored.args == args
    assert "desk_lamp switch:0 OFF" in stored.summary
    assert args["target"][:12] in stored.summary
    assert fake_http.requests == []


def test_human_approval_executes_once_and_returns_acceptance_separately_from_observation(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    pending = app.call("shelly_switch_set", action(app), Origin.ASSISTANT)
    assert pending.approval_id is not None
    assert fake_http.requests == []
    identity(fake_http)
    status(fake_http)
    accepted(fake_http)
    status(fake_http, output=True)
    result = app.dispatcher.approve(pending.approval_id)
    assert result.status == "ok"
    assert result.data["ok"] is True
    assert result.data["command_accepted"] is True
    assert result.data["observation"]["output"] is True
    again = app.dispatcher.approve(pending.approval_id)
    assert again.status == "error"
    assert methods(fake_http).count("Switch.Set") == 1
    stored = app.approvals.get(pending.approval_id)
    assert stored is not None
    assert stored.result["matches_requested"] is True


def test_uncertain_delivery_survives_in_the_approval_report_without_automatic_retry(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    from tests.fixtures.shelly import BASE

    app = make_app(config(controls=True))
    pending = app.call("shelly_switch_set", action(app), Origin.ASSISTANT)
    assert pending.approval_id is not None
    identity(fake_http)
    status(fake_http)
    fake_http.add_error("POST", BASE + "/rpc", "synthetic-timeout")
    status(fake_http, output=True)
    reported = app.dispatcher.approve(pending.approval_id)
    assert reported.status == "ok"
    assert reported.data["ok"] is False
    stored = app.approvals.get(pending.approval_id)
    assert stored is not None
    assert stored.status == "executed"
    assert stored.result["command_accepted"] is None
    assert stored.result["status"] == "uncertain"
    assert stored.result["matches_requested"] is True
    assert app.dispatcher.approve(pending.approval_id).status == "error"
    assert methods(fake_http).count("Switch.Set") == 1


def test_read_only_configuration_cannot_acquire_a_control_by_policy_override(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    data = config()
    data["policy"] = {"tools": {"shelly_switch_set": {"decision": "allow"}}}
    app = make_app(data)
    outcome = app.call(
        "shelly_switch_set",
        {"device": "desk_lamp", "switch_id": 0, "target": "0" * 64, "on": True},
        Origin.HUMAN,
    )
    assert outcome.status == "invalid"
    assert fake_http.requests == []


def test_policy_deny_and_kill_switch_override_an_approved_control_configuration(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    data = config(controls=True)
    data["policy"] = {"tools": {"shelly_switch_set": {"decision": "deny"}}}
    app = make_app(data)
    assert app.call("shelly_switch_set", action(app), Origin.HUMAN).status == "denied"
    app = make_app(config(controls=True))
    app.policy.kill_switch.write_text("synthetic-test\n", encoding="ascii")
    assert app.call("shelly_switch_set", action(app), Origin.HUMAN).status == "denied"
    assert app.call("shelly_devices", {}, Origin.ASSISTANT).status == "ok"
    assert fake_http.requests == []


@pytest.mark.parametrize(
    "changes",
    [
        {"on": "toggle"},
        {"on": 1},
        {"target": "0" * 64},
        {"switch_id": 1},
        {"device": "http://192.168.10.41"},
        {"method": "Switch.Toggle"},
        {"toggle_after": 1},
        {"on": None},
    ],
)
def test_bad_action_arguments_never_queue_an_approval_or_contact_a_device(
    make_app: AppFactory, fake_http: FakeHttp, changes: dict[str, Any]
) -> None:
    app = make_app(config(controls=True))
    args = {**action(app), **changes}
    assert app.call("shelly_switch_set", args, Origin.ASSISTANT).status == "invalid"
    assert app.approvals.list("pending") == []
    assert fake_http.requests == []


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://192.168.10.41"},
        {"expected_device_id": "shellyplusplugs-000000000002"},
        {"expected_model": "SNPL-00116US"},
        {"password": "env:SB_TEST_CHANGED_PASSWORD"},
    ],
)
def test_pending_approvals_do_not_retarget_after_configuration_changes(
    make_app: AppFactory, fake_http: FakeHttp, changes: dict[str, Any]
) -> None:
    app = make_app(config(controls=True))
    pending = app.call("shelly_switch_set", action(app), Origin.ASSISTANT)
    assert pending.approval_id is not None
    changed = config(controls=True)
    changed["connectors"]["shelly"]["devices"]["desk_lamp"].update(changes)
    restarted = make_app(changed)
    assert restarted.dispatcher.approve(pending.approval_id).status == "invalid"
    assert fake_http.requests == []


def test_withdrawing_or_narrowing_control_permissions_invalidates_pending_approval(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    pending = app.call("shelly_switch_set", action(app), Origin.ASSISTANT)
    assert pending.approval_id is not None
    narrowed = config(controls=True)
    narrowed["connectors"]["shelly"]["devices"]["desk_lamp"]["controls"]["0"]["max_power_watts"] = 20
    assert make_app(narrowed).dispatcher.approve(pending.approval_id).status == "invalid"
    assert fake_http.requests == []
    second = make_app(config(controls=True))
    pending = second.call("shelly_switch_set", action(second), Origin.ASSISTANT)
    assert pending.approval_id is not None
    assert make_app(config()).dispatcher.approve(pending.approval_id).status == "error"
    assert fake_http.requests == []


def test_current_policy_is_rechecked_when_a_pending_control_is_approved(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(controls=True))
    pending = app.call("shelly_switch_set", action(app), Origin.ASSISTANT)
    assert pending.approval_id is not None
    denied = config(controls=True)
    denied["policy"] = {"tools": {"shelly_switch_set": {"decision": "deny"}}}
    assert make_app(denied).dispatcher.approve(pending.approval_id).status == "denied"
    assert fake_http.requests == []


def test_action_rate_caps_apply_to_approved_execution(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(config(controls=True))
    pending = app.call("shelly_switch_set", action(app), Origin.ASSISTANT)
    assert pending.approval_id is not None
    capped = config(controls=True)
    capped["policy"] = {"tools": {"shelly_switch_set": {"max_per_hour": 0}}}
    assert make_app(capped).dispatcher.approve(pending.approval_id).status == "denied"
    assert fake_http.requests == []

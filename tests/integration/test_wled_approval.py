"""Full-dispatcher WLED cases using synthetic responses only; authored and unrun."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.policy import Origin
from tests.fakes import FakeHttp

pytestmark = pytest.mark.integration
AppFactory = Callable[..., App]
BASE = "http://192.168.50.20"
OTHER = "http://192.168.50.21"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wled"


def snapshot() -> dict[str, Any]:
    return dict(json.loads((FIXTURES / "snapshot.json").read_text(encoding="utf-8")))


def config(*, allow_global: bool = True, two_devices: bool = False) -> dict[str, Any]:
    preset = {"seg": [{"id": 0, "on": False}]}
    canonical = json.dumps(preset, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    devices: list[dict[str, Any]] = [
        {
            "name": "desk",
            "base_url": BASE,
            "allow_http": True,
            "allow_global": allow_global,
            "segments": [0],
            "presets": [
                {
                    "name": "rest",
                    "id": 1,
                    "sha256": hashlib.sha256(canonical.encode("ascii")).hexdigest(),
                }
            ],
        }
    ]
    if two_devices:
        devices.append({**devices[0], "name": "shelf", "base_url": OTHER, "allow_global": False})
    return {"switchboard": {"connectors": ["wled"]}, "connectors": {"wled": {"devices": devices}}}


def bound_args(app: App, args: dict[str, Any]) -> dict[str, Any]:
    devices = app.call("wled_devices", {}, Origin.ASSISTANT).data["devices"]
    device = next(device for device in devices if device["device"] == args["device"])
    return {"target_digest": device["target_digest"], **args}


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("wled_set_power", {"on": False}),
        ("wled_set_brightness", {"brightness": 80}),
        ("wled_set_color", {"segment": 0, "color": [1, 2, 3]}),
        ("wled_set_transition", {"transition_deciseconds": 4}),
        ("wled_apply_preset", {"preset": "rest"}),
    ],
)
@pytest.mark.parametrize("origin", [Origin.ASSISTANT, Origin.RULE])
def test_every_action_waits_for_default_approval_without_preflight_calls(
    make_app: AppFactory,
    fake_http: FakeHttp,
    tool: str,
    args: dict[str, Any],
    origin: Origin,
) -> None:
    app = make_app(config())

    arguments = bound_args(app, {"device": "desk", **args})
    pending = app.call(tool, arguments, origin)

    assert pending.status == "pending"
    assert pending.approval_id is not None
    request = app.approvals.get(pending.approval_id)
    assert request is not None
    assert request.args == arguments
    assert "desk" in request.summary
    assert args.get("preset", "") in request.summary
    assert fake_http.requests == []
    assert app.ctx.events.recent(source="wled") == []


def test_approval_executes_stored_scope_and_does_not_resend_same_approval(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(allow_global=False, two_devices=True))
    args = bound_args(app, {"device": "desk", "segment": 0, "on": False})
    pending = app.call("wled_set_power", args, Origin.ASSISTANT)
    assert pending.approval_id is not None
    args.update(device="shelf", on=True)
    before = snapshot()
    after = snapshot()
    after["state"]["seg"][0]["on"] = False
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(before))
    fake_http.add("POST", f"{BASE}/json/state", body='{"success":true}')
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(after))

    approved = app.dispatcher.approve(pending.approval_id)

    assert approved.status == "ok", approved.message
    assert approved.data["confirmed"] is True
    assert approved.data["device"] == "desk"
    posts = [request for request in fake_http.requests if request.method == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0].body or b"") == {
        "seg": [{"id": 0, "on": False}],
        "udpn": {"nn": True},
    }
    assert all(request.url.startswith(f"{BASE}/") for request in fake_http.requests)
    assert len(fake_http.requests) == 3

    repeated = app.dispatcher.approve(pending.approval_id)

    assert repeated.status == "error"
    assert len(fake_http.requests) == 3
    [event] = app.ctx.events.recent(source="wled")
    assert event.payload == approved.data


@pytest.mark.parametrize("readback_unavailable", [False, True])
def test_executed_approval_retains_unconfirmed_result_not_a_claim_of_light_output(
    make_app: AppFactory, fake_http: FakeHttp, readback_unavailable: bool
) -> None:
    app = make_app(config())
    pending = app.call("wled_set_power", bound_args(app, {"device": "desk", "on": False}), Origin.ASSISTANT)
    assert pending.approval_id is not None
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(snapshot()))
    fake_http.add("POST", f"{BASE}/json/state", body='{"success":true}')
    if readback_unavailable:
        fake_http.add_error("GET", f"{BASE}/json/si")
    else:
        fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(snapshot()))

    approved = app.dispatcher.approve(pending.approval_id)

    assert approved.status == "ok"
    assert approved.data["status"] == "unconfirmed"
    status = app.call("approvals_status", {"id": pending.approval_id}, Origin.ASSISTANT)
    assert status.data["status"] == "executed"
    assert status.data["result"]["confirmed"] is False
    assert status.data["result"]["physical_output_verified"] is False


def test_kill_switch_blocks_previously_approved_action_without_contact(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config())
    pending = app.call("wled_set_power", bound_args(app, {"device": "desk", "on": False}), Origin.ASSISTANT)
    assert pending.approval_id is not None
    app.policy.kill_switch.touch()

    blocked = app.dispatcher.approve(pending.approval_id)

    assert blocked.status == "denied"
    assert fake_http.requests == []
    assert app.ctx.events.recent(source="wled") == []


def test_explicit_deny_overrides_human_invocation(make_app: AppFactory, fake_http: FakeHttp) -> None:
    table = config()
    table["policy"] = {"tools": {"wled_*": {"decision": "deny"}}}
    app = make_app(table)

    devices = app.registry.get("wled_devices")
    assert devices is not None
    digest = devices.handler({})["devices"][0]["target_digest"]
    denied = app.call(
        "wled_set_power", {"device": "desk", "target_digest": digest, "on": False}, Origin.HUMAN
    )

    assert denied.status == "denied"
    assert fake_http.requests == []


def test_preset_alias_is_resolved_only_within_requested_device(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    table = config(two_devices=True)
    table["connectors"]["wled"]["devices"][0]["presets"] = []
    app = make_app(table)

    result = app.call(
        "wled_apply_preset", bound_args(app, {"device": "desk", "preset": "rest"}), Origin.HUMAN
    )

    assert result.status == "error"
    assert fake_http.requests == []


def test_confirmed_preset_is_scoped_to_named_origin_and_does_not_post_preset_id(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(allow_global=False, two_devices=True))
    pending = app.call(
        "wled_apply_preset", bound_args(app, {"device": "shelf", "preset": "rest"}), Origin.ASSISTANT
    )
    assert pending.approval_id is not None
    fake_http.add("GET", f"{OTHER}/presets.json", body='{"1":{"seg":[{"id":0,"on":false}]}}')
    after = snapshot()
    after["state"]["seg"][0]["on"] = False
    fake_http.add("GET", f"{OTHER}/json/si", body=json.dumps(snapshot()))
    fake_http.add("POST", f"{OTHER}/json/state", body='{"success":true}')
    fake_http.add("GET", f"{OTHER}/json/si", body=json.dumps(after))

    result = app.dispatcher.approve(pending.approval_id)

    assert result.data["confirmed"] is True
    assert all(request.url.startswith(f"{OTHER}/") for request in fake_http.requests)
    [post] = [request for request in fake_http.requests if request.method == "POST"]
    assert json.loads(post.body or b"") == {"seg": [{"id": 0, "on": False}], "udpn": {"nn": True}}


def test_read_only_operations_remain_available_with_kill_switch(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config())
    app.policy.kill_switch.touch()
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(snapshot()))

    assert app.call("wled_devices", {}, Origin.ASSISTANT).status == "ok"
    assert app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT).status == "ok"
    assert len(fake_http.requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        "endpoint",
        "segments",
        "global_permission",
        "http_consent",
        "preset_hash",
        "preset_id",
        "timeout",
        "byte_limit",
    ],
)
def test_stale_configuration_approval_is_rejected_after_reload_without_network(
    make_app: AppFactory, fake_http: FakeHttp, change: str
) -> None:
    table = config()
    if change == "http_consent":
        table["connectors"]["wled"]["devices"][0]["base_url"] = "https://desk.local"
    initial = make_app(table)
    if change in {"preset_hash", "preset_id"}:
        tool = "wled_apply_preset"
        arguments = bound_args(initial, {"device": "desk", "preset": "rest"})
    else:
        tool = "wled_set_power"
        arguments = bound_args(initial, {"device": "desk", "segment": 0, "on": False})
    pending = initial.call(tool, arguments, Origin.ASSISTANT)
    assert pending.approval_id is not None
    device = table["connectors"]["wled"]["devices"][0]
    if change == "endpoint":
        device["base_url"] = OTHER
    elif change == "segments":
        device["segments"] = [0, 1]
    elif change == "global_permission":
        device["allow_global"] = False
    elif change == "http_consent":
        device["allow_http"] = False
    elif change == "preset_hash":
        device["presets"][0]["sha256"] = "0" * 64
    elif change == "preset_id":
        device["presets"][0]["id"] = 2
    elif change == "timeout":
        table["connectors"]["wled"]["timeout_seconds"] = 10
    else:
        table["connectors"]["wled"]["max_response_bytes"] = 4096
    reloaded = make_app(table)
    current = bound_args(reloaded, {"device": "desk"})
    assert current["target_digest"] != arguments["target_digest"]

    rejected = reloaded.dispatcher.approve(pending.approval_id)

    assert rejected.status == "invalid"
    stored = reloaded.approvals.get(pending.approval_id)
    assert stored is not None and stored.status == "failed"
    assert fake_http.requests == []
    assert reloaded.ctx.events.recent(source="wled") == []


def test_digest_for_another_current_device_cannot_retarget_action(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(two_devices=True))
    other_digest = bound_args(app, {"device": "shelf"})["target_digest"]

    result = app.call(
        "wled_set_power",
        {"device": "desk", "target_digest": other_digest, "segment": 0, "on": False},
        Origin.HUMAN,
    )

    assert result.status == "error"
    assert "configuration changed" in result.message
    assert fake_http.requests == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("wled_set_power", {"on": False}),
        ("wled_set_brightness", {"brightness": 80}),
        ("wled_set_color", {"segment": 0, "color": [1, 2, 3]}),
        ("wled_set_transition", {"transition_deciseconds": 4}),
        ("wled_apply_preset", {"preset": "rest"}),
    ],
)
def test_all_new_actions_require_explicit_current_binding(
    make_app: AppFactory, fake_http: FakeHttp, tool: str, args: dict[str, Any]
) -> None:
    app = make_app(config())

    result = app.call(tool, {"device": "desk", **args}, Origin.ASSISTANT)

    assert result.status == "invalid"
    assert fake_http.requests == []
    assert app.approvals.list("pending") == []

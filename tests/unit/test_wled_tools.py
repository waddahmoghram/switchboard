"""Synthetic WLED payload, scope, capability, and confirmation cases; all authored and unrun."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.httpclient import HttpResponse
from switchboard.policy import Origin
from tests.fakes import FakeHttp, RecordedRequest

AppFactory = Callable[..., App]
BASE = "http://192.168.50.20"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wled"


def fixture(name: str = "snapshot.json") -> dict[str, Any]:
    return dict(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def config(
    *,
    allow_global: bool = False,
    segments: list[int] | None = None,
    preset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    device: dict[str, Any] = {
        "name": "desk",
        "base_url": BASE,
        "allow_http": True,
        "allow_global": allow_global,
        "segments": segments if segments is not None else [0, 1],
    }
    if preset is not None:
        canonical = json.dumps(preset, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        device["presets"] = [
            {"name": "evening", "id": 1, "sha256": hashlib.sha256(canonical.encode("ascii")).hexdigest()}
        ]
    return {"switchboard": {"connectors": ["wled"]}, "connectors": {"wled": {"devices": [device]}}}


def bound_args(app: App, args: dict[str, Any]) -> dict[str, Any]:
    devices = app.call("wled_devices", {}, Origin.ASSISTANT).data["devices"]
    device = next(device for device in devices if device["device"] == args["device"])
    return {"target_digest": device["target_digest"], **args}


def queue_action(
    fake_http: FakeHttp,
    patch: dict[str, Any],
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    acknowledgement: str = '{"success":true}',
) -> None:
    initial = fixture() if before is None else before
    observed = copy.deepcopy(initial) if after is None else after
    if after is None:
        for key, value in patch.items():
            if key != "seg":
                observed["state"][key] = value
        for requested in patch.get("seg", []):
            target = next(item for item in observed["state"]["seg"] if item["id"] == requested["id"])
            target.update(requested)
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(initial))
    fake_http.add("POST", f"{BASE}/json/state", body=acknowledgement)
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(observed))


def test_devices_catalog_is_offline_and_omits_endpoints(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(config())

    outcome = app.call("wled_devices", {}, Origin.ASSISTANT)

    assert outcome.status == "ok"
    digest = outcome.data["devices"][0]["target_digest"]
    assert len(digest) == 64
    assert outcome.data == {
        "devices": [
            {
                "device": "desk",
                "target_digest": digest,
                "segments": [0, 1],
                "global_control": False,
                "presets": [],
            }
        ],
        "live_discovery": False,
        "polling": False,
    }
    assert fake_http.requests == []


def test_status_is_bounded_and_exposes_only_configured_segments(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config())
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(fixture()))

    outcome = app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT)

    assert outcome.status == "ok", outcome.message
    data = outcome.data
    assert data["status"] == "observed"
    assert data["stale"] is False
    assert data["observed_at"]
    assert data["firmware"] == "0.15.0"
    assert data["brightness"] == 128
    assert data["transition_deciseconds"] == 7
    assert data["led_count"] == 90
    assert data["max_segments"] == 16
    assert data["missing_segments"] == []
    assert data["target_digest"] == bound_args(app, {"device": "desk"})["target_digest"]
    assert [segment["id"] for segment in data["segments"]] == [0, 1]
    assert data["segments"][0]["capabilities"] == {
        "reported": True,
        "rgb": True,
        "white": False,
        "color_temperature": False,
    }
    assert data["segments"][1]["capabilities"]["white"] is True
    serialized = json.dumps(data)
    for private in ("synthetic-private-marker", '"mac"', '"ip"', '"wifi"', '"udpn"', '"sel"'):
        assert private not in serialized
    [request] = fake_http.requests
    assert request.body is None
    assert request.url == f"{BASE}/json/si"
    assert request.follow_redirects is False
    assert request.public_only is False
    assert request.max_bytes == 65536
    assert request.timeout == 5
    assert request.headers == {"accept": "application/json"}
    assert app.ctx.events.recent(source="wled") == []


def test_status_marks_missing_and_unreported_capabilities_without_guessing(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(segments=[0, 5]))
    data = fixture()
    data["info"]["leds"].pop("seglc")
    data["state"]["nl"]["on"] = {"untrusted": "must not be returned"}
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(data))

    result = app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT)

    assert result.status == "ok"
    assert result.data["missing_segments"] == [5]
    assert result.data["nightlight_active"] is None
    assert result.data["segments"][0]["capabilities"]["reported"] is False
    assert result.data["segments"][0]["capabilities"]["rgb"] is None
    assert "must not be returned" not in json.dumps(result.data)


@pytest.mark.parametrize(
    ("tool", "args", "patch"),
    [
        ("wled_set_power", {"on": False}, {"on": False}),
        ("wled_set_power", {"segment": 1, "on": True}, {"seg": [{"id": 1, "on": True}]}),
        ("wled_set_brightness", {"brightness": 96}, {"bri": 96}),
        (
            "wled_set_brightness",
            {"segment": 0, "brightness": 93},
            {"seg": [{"id": 0, "bri": 93}]},
        ),
        (
            "wled_set_color",
            {"segment": 0, "color": [12, 34, 56]},
            {"seg": [{"id": 0, "col": [[12, 34, 56]], "fx": 0}]},
        ),
        (
            "wled_set_color",
            {"segment": 1, "color": [1, 2, 3, 4]},
            {"seg": [{"id": 1, "col": [[1, 2, 3, 4]], "fx": 0}]},
        ),
        ("wled_set_transition", {"transition_deciseconds": 4}, {"transition": 4}),
        ("wled_set_transition", {"transition_deciseconds": 0}, {"transition": 0}),
    ],
)
def test_exact_payload_and_state_confirmation(
    make_app: AppFactory,
    fake_http: FakeHttp,
    tool: str,
    args: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    app = make_app(config(allow_global=True))
    queue_action(fake_http, patch)

    outcome = app.call(tool, bound_args(app, {"device": "desk", **args}), Origin.HUMAN)

    assert outcome.status == "ok", outcome.message
    assert outcome.data["status"] == "confirmed"
    assert outcome.data["confirmed"] is True
    assert outcome.data["confirmation"] == "state_matched"
    assert outcome.data["physical_output_verified"] is False
    assert outcome.data["requested"] == patch
    assert outcome.data["stale"] is False
    assert [(request.method, request.url) for request in fake_http.requests] == [
        ("GET", f"{BASE}/json/si"),
        ("POST", f"{BASE}/json/state"),
        ("GET", f"{BASE}/json/si"),
    ]
    post = fake_http.requests[1]
    expected = json.dumps({**patch, "udpn": {"nn": True}}, sort_keys=True, separators=(",", ":"))
    assert post.body == expected.encode("ascii")
    assert post.headers == {"accept": "application/json", "content-type": "application/json"}
    for request in fake_http.requests:
        assert request.follow_redirects is False
        assert request.max_bytes == 65536
        assert request.timeout == 5
    [event] = app.ctx.events.recent(source="wled")
    assert event.type == "wled.action_result"
    assert event.payload == outcome.data


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("wled_set_brightness", {"segment": 0, "brightness": 80}),
        ("wled_set_color", {"segment": 0, "color": [10, 20, 30]}),
    ],
)
def test_brightness_and_color_do_not_implicitly_power_on(
    make_app: AppFactory, fake_http: FakeHttp, tool: str, args: dict[str, Any]
) -> None:
    app = make_app(config())
    before = fixture()
    before["state"]["on"] = False
    patch: dict[str, Any]
    if tool == "wled_set_brightness":
        patch = {"seg": [{"id": 0, "bri": 80}]}
    else:
        patch = {"seg": [{"id": 0, "col": [[10, 20, 30]], "fx": 0}]}
    queue_action(fake_http, patch, before=before)

    result = app.call(tool, bound_args(app, {"device": "desk", **args}), Origin.HUMAN)

    assert result.data["confirmed"] is True
    assert result.data["observation"]["on"] is False
    assert "on" not in json.loads(fake_http.requests[1].body or b"")["seg"][0]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("wled_status", {"device": "other"}),
        ("wled_status", {"device": "http://192.168.50.21"}),
        ("wled_status", {"device": "desk", "url": "http://192.168.50.21"}),
        ("wled_status", {}),
        ("wled_devices", {"limit": 1}),
        ("wled_set_power", {"device": "desk", "on": "t"}),
        ("wled_set_power", {"device": "desk", "on": 1}),
        ("wled_set_power", {"device": "desk", "on": True, "segment": 64}),
        ("wled_set_power", {"device": "desk", "on": True, "segment": -1}),
        ("wled_set_power", {"device": "desk", "on": True, "segment": False}),
        ("wled_set_brightness", {"device": "desk", "brightness": 0}),
        ("wled_set_brightness", {"device": "desk", "brightness": 256}),
        ("wled_set_brightness", {"device": "desk", "brightness": True}),
        ("wled_set_brightness", {"device": "desk", "brightness": 1.5}),
        ("wled_set_color", {"device": "desk", "segment": 0, "color": [0, 0]}),
        ("wled_set_color", {"device": "desk", "segment": 0, "color": [0, 0, 0, 0, 0]}),
        ("wled_set_color", {"device": "desk", "segment": 0, "color": [0, 0, 256]}),
        ("wled_set_color", {"device": "desk", "segment": 0, "color": [0, 0, True]}),
        ("wled_set_color", {"device": "desk", "segment": 0, "color": "r"}),
        ("wled_set_color", {"device": "desk", "color": [0, 0, 0]}),
        ("wled_set_transition", {"device": "desk", "transition_deciseconds": 101}),
        ("wled_set_transition", {"device": "desk", "transition_deciseconds": -1}),
        ("wled_set_transition", {"device": "desk", "transition_deciseconds": 2.5}),
        ("wled_set_transition", {"device": "desk", "transition_deciseconds": 4, "segment": 0}),
        ("wled_apply_preset", {"device": "desk", "preset": 1}),
        ("wled_set_power", {"device": "desk", "on": True, "body": {"rb": True}}),
    ],
)
def test_invalid_arguments_are_refused_before_approval_or_network(
    make_app: AppFactory, fake_http: FakeHttp, tool: str, args: dict[str, Any]
) -> None:
    app = make_app(config(allow_global=True))

    arguments = args if tool in {"wled_status", "wled_devices"} else bound_args(app, args)
    outcome = app.call(tool, arguments, Origin.ASSISTANT)

    assert outcome.status == "invalid"
    assert fake_http.requests == []
    assert app.approvals.list("pending") == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("wled_set_power", {"on": True}),
        ("wled_set_brightness", {"brightness": 80}),
        ("wled_set_transition", {"transition_deciseconds": 4}),
        ("wled_set_power", {"on": True, "segment": 2}),
        ("wled_set_brightness", {"brightness": 80, "segment": 2}),
        ("wled_set_color", {"color": [10, 20, 30], "segment": 2}),
        ("wled_apply_preset", {"preset": "not_configured"}),
    ],
)
def test_out_of_scope_actions_fail_before_network(
    make_app: AppFactory, fake_http: FakeHttp, tool: str, args: dict[str, Any]
) -> None:
    app = make_app(config())

    outcome = app.call(tool, bound_args(app, {"device": "desk", **args}), Origin.HUMAN)

    assert outcome.status == "error"
    assert fake_http.requests == []
    assert app.ctx.events.recent(source="wled") == []


@pytest.mark.parametrize(
    "broken",
    [
        "realtime",
        "playlist",
        "nightlight",
        "unknown_realtime",
        "unknown_playlist",
        "unknown_nightlight",
        "missing_capabilities",
        "no_rgb",
        "inactive",
        "unknown_bounds",
        "unknown_maxseg",
        "outside_maxseg",
        "missing_effect",
        "invalid_colors",
        "missing_segment",
    ],
)
def test_unverified_or_unsupported_color_targets_are_not_written(
    make_app: AppFactory, fake_http: FakeHttp, broken: str
) -> None:
    app = make_app(config())
    data = fixture()
    state, info = data["state"], data["info"]
    segment = state["seg"][0]
    if broken == "realtime":
        info["live"] = True
    elif broken == "playlist":
        state["pl"] = 1
    elif broken == "nightlight":
        state["nl"]["on"] = True
    elif broken == "unknown_realtime":
        info.pop("live")
    elif broken == "unknown_playlist":
        state.pop("pl")
    elif broken == "unknown_nightlight":
        state["nl"]["on"] = "false"
    elif broken == "missing_capabilities":
        info["leds"].pop("seglc")
    elif broken == "no_rgb":
        info["leds"]["seglc"][0] = 2
    elif broken == "inactive":
        segment["stop"] = 0
    elif broken == "unknown_bounds":
        segment.pop("start")
    elif broken == "unknown_maxseg":
        info["leds"].pop("maxseg")
    elif broken == "outside_maxseg":
        info["leds"]["maxseg"] = 0
    elif broken == "missing_effect":
        segment.pop("fx")
    elif broken == "invalid_colors":
        segment["col"] = ["r"]
    else:
        state["seg"] = state["seg"][1:]
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(data))

    result = app.call(
        "wled_set_color",
        bound_args(app, {"device": "desk", "segment": 0, "color": [10, 20, 30]}),
        Origin.HUMAN,
    )

    assert result.status == "error"
    assert len(fake_http.requests) == 1
    assert fake_http.requests[0].method == "GET"


def test_white_channel_requires_segment_capability(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(config())
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(fixture()))

    result = app.call(
        "wled_set_color",
        bound_args(app, {"device": "desk", "segment": 0, "color": [1, 2, 3, 0]}),
        Origin.HUMAN,
    )

    assert result.status == "error"
    assert "white channel" in result.message
    assert len(fake_http.requests) == 1


def test_missing_transition_setting_is_not_assumed(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(config(allow_global=True))
    before = fixture()
    before["state"].pop("transition")
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(before))

    result = app.call(
        "wled_set_transition",
        bound_args(app, {"device": "desk", "transition_deciseconds": 4}),
        Origin.HUMAN,
    )

    assert result.status == "error"
    assert len(fake_http.requests) == 1


@pytest.mark.parametrize("mismatch", ["color", "effect", "segment", "inactive", "realtime", "playlist"])
def test_acknowledgement_cannot_confirm_mismatching_readback(
    make_app: AppFactory, fake_http: FakeHttp, mismatch: str
) -> None:
    app = make_app(config())
    patch = {"seg": [{"id": 0, "col": [[10, 20, 30]], "fx": 0}]}
    after = fixture()
    after["state"]["seg"][0]["col"] = [[10, 20, 30, 0]]
    if mismatch == "color":
        after["state"]["seg"][0]["col"] = [[11, 20, 30, 0]]
    elif mismatch == "effect":
        after["state"]["seg"][0]["fx"] = 1
    elif mismatch == "segment":
        after["state"]["seg"] = after["state"]["seg"][1:]
    elif mismatch == "inactive":
        after["state"]["seg"][0]["stop"] = 0
    elif mismatch == "realtime":
        after["info"]["live"] = True
    else:
        after["state"]["pl"] = 2
    queue_action(fake_http, patch, after=after)

    result = app.call(
        "wled_set_color",
        bound_args(app, {"device": "desk", "segment": 0, "color": [10, 20, 30]}),
        Origin.HUMAN,
    )

    assert result.status == "ok"
    assert result.data["status"] == "unconfirmed"
    assert result.data["confirmed"] is False
    assert result.data["acknowledged"] is True
    assert result.data["effect_unknown"] is True
    assert result.data["confirmation"] == "state_mismatch"
    [event] = app.ctx.events.recent(source="wled")
    assert event.payload["status"] == "unconfirmed"


@pytest.mark.parametrize(
    ("tool", "args", "patch"),
    [
        ("wled_set_power", {"on": False}, {"on": False}),
        ("wled_set_brightness", {"brightness": 80}, {"bri": 80}),
        ("wled_set_brightness", {"segment": 0, "brightness": 80}, {"seg": [{"id": 0, "bri": 80}]}),
        ("wled_set_transition", {"transition_deciseconds": 4}, {"transition": 4}),
    ],
)
def test_each_numeric_and_power_control_compares_readback(
    make_app: AppFactory,
    fake_http: FakeHttp,
    tool: str,
    args: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    app = make_app(config(allow_global=True))
    queue_action(fake_http, patch, after=fixture())

    result = app.call(tool, bound_args(app, {"device": "desk", **args}), Origin.HUMAN)

    assert result.data["status"] == "unconfirmed"
    assert result.data["confirmed"] is False


def test_readback_unavailability_is_explicit_and_does_not_retry(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(allow_global=True))
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(fixture()))
    fake_http.add("POST", f"{BASE}/json/state", body='{"success":true}')
    fake_http.add_error("GET", f"{BASE}/json/si", message="synthetic-private-network-error")

    result = app.call("wled_set_power", bound_args(app, {"device": "desk", "on": False}), Origin.HUMAN)

    assert result.status == "ok"
    assert result.data["status"] == "unconfirmed"
    assert result.data["stale"] is True
    assert result.data["observation"] is None
    assert result.data["confirmed"] is False
    assert result.data["acknowledged"] is True
    assert "readback unavailable" in result.data["error"]
    assert "synthetic-private-network-error" not in json.dumps(result.data)
    assert len(fake_http.requests) == 3


def test_transport_failure_during_write_is_an_unknown_effect_not_a_retry(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config(allow_global=True))
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(fixture()))
    fake_http.add_error("POST", f"{BASE}/json/state", message="synthetic-private-network-error")

    result = app.call("wled_set_power", bound_args(app, {"device": "desk", "on": False}), Origin.HUMAN)

    assert result.status == "ok"
    assert result.data["status"] == "error"
    assert result.data["attempted"] is True
    assert result.data["confirmation"] == "unconfirmed"
    assert result.data["effect_unknown"] is True
    assert result.data["stale"] is True
    assert "synthetic-private-network-error" not in json.dumps(result.data)
    assert len(fake_http.requests) == 2
    [event] = app.ctx.events.recent(source="wled")
    assert event.payload["status"] == "error"


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 403, 404, 429, 500, 503])
def test_http_write_failures_and_redirects_do_not_leak_bodies_or_follow(
    make_app: AppFactory, fake_http: FakeHttp, status: int
) -> None:
    app = make_app(config(allow_global=True))
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(fixture()))
    fake_http.add(
        "POST",
        f"{BASE}/json/state",
        status=status,
        body="synthetic-private-body",
        headers={"Location": "https://external.example.test/synthetic-private-location"},
    )

    result = app.call("wled_set_power", bound_args(app, {"device": "desk", "on": False}), Origin.HUMAN)

    assert result.data["status"] == "error"
    assert result.data["confirmed"] is False
    assert result.data["effect_unknown"] is True
    assert "synthetic-private" not in json.dumps(result.data)
    assert len(fake_http.requests) == 2
    assert all(request.follow_redirects is False for request in fake_http.requests)


@pytest.mark.parametrize(
    "body", ['{"error":5}', '{"success":false}', '{"error":"synthetic-private-provider-error"}']
)
def test_provider_errors_are_not_success_even_with_http_200(
    make_app: AppFactory, fake_http: FakeHttp, body: str
) -> None:
    app = make_app(config(allow_global=True))
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(fixture()))
    fake_http.add("POST", f"{BASE}/json/state", body=body)

    result = app.call("wled_set_power", bound_args(app, {"device": "desk", "on": False}), Origin.HUMAN)

    assert result.data["status"] == "error"
    assert result.data["confirmed"] is False
    assert "provider error" in result.data["error"]
    assert "synthetic-private-provider-error" not in json.dumps(result.data)
    assert len(fake_http.requests) == 2


@pytest.mark.parametrize("body", ["", "not-json", "{}", '{"success":"true"}'])
def test_unreadable_acknowledgement_can_only_be_confirmed_by_matching_state(
    make_app: AppFactory, fake_http: FakeHttp, body: str
) -> None:
    app = make_app(config(allow_global=True))
    queue_action(fake_http, {"on": False}, acknowledgement=body)

    result = app.call("wled_set_power", bound_args(app, {"device": "desk", "on": False}), Origin.HUMAN)

    assert result.data["confirmed"] is True
    assert result.data["acknowledged"] is None
    assert len(fake_http.requests) == 3


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_status_http_failures_never_return_cached_state(
    make_app: AppFactory, fake_http: FakeHttp, status: int
) -> None:
    app = make_app(config())
    fake_http.add("GET", f"{BASE}/json/si", status=status, body="synthetic-private-body")

    result = app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT)

    assert result.status == "error"
    assert result.data is None
    assert "synthetic-private-body" not in result.message
    assert len(fake_http.requests) == 1


def test_status_transport_failure_is_redacted(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(config())
    fake_http.add_error("GET", f"{BASE}/json/si", message="synthetic-private-network-error")

    result = app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT)

    assert result.status == "error"
    assert "synthetic-private-network-error" not in result.message


def test_changed_response_origin_is_refused(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(config())

    def redirected(_request: RecordedRequest) -> HttpResponse:
        return HttpResponse(200, "https://external.example.test/json/si", body=b"{}")

    fake_http.add_handler("GET", f"{BASE}/json/si", redirected)

    result = app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT)

    assert result.status == "error"
    assert "redirect refused" in result.message
    assert len(fake_http.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        "not-json",
        "[]",
        "{}",
        '{"error":7}',
        '{"state":{"on":true,"bri":NaN}}',
        '{"state":{"on":true,"bri":1e9999}}',
        '{"state":{"on":true,"bri":true},"info":{"leds":{}}}',
    ],
)
def test_invalid_status_documents_fail_closed(make_app: AppFactory, fake_http: FakeHttp, body: str) -> None:
    app = make_app(config())
    fake_http.add("GET", f"{BASE}/json/si", body=body)

    assert app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT).status == "error"


@pytest.mark.parametrize("broken", ["duplicate", "missing_id", "many", "nonobject"])
def test_invalid_segment_lists_fail_closed(make_app: AppFactory, fake_http: FakeHttp, broken: str) -> None:
    app = make_app(config())
    data = fixture()
    if broken == "duplicate":
        data["state"]["seg"][1]["id"] = 0
    elif broken == "missing_id":
        data["state"]["seg"][0].pop("id")
    elif broken == "many":
        data["state"]["seg"] = [data["state"]["seg"][0]] * 65
    else:
        data["state"]["seg"] = [None]
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(data))

    assert app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT).status == "error"


def test_oversized_response_is_refused_even_when_fake_ignores_limit(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config())
    data = fixture()
    data["private_padding"] = "x" * 65536
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(data))

    result = app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT)

    assert result.status == "error"
    assert "byte limit" in result.message


def test_reviewed_preset_is_copied_as_static_state_not_executed_by_identifier(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    presets = fixture("presets.json")
    preset = presets["1"]
    app = make_app(config(preset=preset))
    fake_http.add("GET", f"{BASE}/presets.json", body=json.dumps(presets))
    patch = {"seg": [{"id": 0, "on": True, "bri": 96, "col": [[255, 160, 40]], "fx": 0}]}
    queue_action(fake_http, patch)

    result = app.call(
        "wled_apply_preset", bound_args(app, {"device": "desk", "preset": "evening"}), Origin.HUMAN
    )

    assert result.data["confirmed"] is True
    assert result.data["preset"]["id"] == 1
    assert result.data["requested"] == patch
    posts = [request for request in fake_http.requests if request.method == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0].body or b"") == {**patch, "udpn": {"nn": True}}
    assert "ps" not in json.loads(posts[0].body or b"")
    assert "n" not in json.loads(posts[0].body or b"")
    assert result.data["observation"]["preset_id"] == -1


@pytest.mark.parametrize("change", ["brightness", "metadata", "missing"])
def test_preset_fingerprint_or_missing_object_prevents_any_write(
    make_app: AppFactory, fake_http: FakeHttp, change: str
) -> None:
    preset = fixture("presets.json")["1"]
    app = make_app(config(preset=preset))
    altered = copy.deepcopy(preset)
    if change == "brightness":
        altered["seg"][0]["bri"] = 97
    else:
        altered["n"] = "Changed name"
    remote = {} if change == "missing" else {"1": altered}
    fake_http.add("GET", f"{BASE}/presets.json", body=json.dumps(remote))

    result = app.call(
        "wled_apply_preset", bound_args(app, {"device": "desk", "preset": "evening"}), Origin.HUMAN
    )

    assert result.status == "error"
    assert len(fake_http.requests) == 1
    assert fake_http.requests[0].method == "GET"


@pytest.mark.parametrize(
    "preset",
    [
        {"api": "T=2"},
        {"on": "t"},
        {"bri": "~10"},
        {"bri": 0},
        {"rb": True},
        {"cfg": {"reboot": True}},
        {"ps": 1},
        {"psave": 1},
        {"pdel": 1},
        {"playlist": {"ps": [1, 2]}},
        {"udpn": {"send": True}},
        {"nl": {"on": True}},
        {"seg": [{"id": 0, "start": 0, "stop": 60}]},
        {"seg": [{"id": 0, "col": ["r"], "fx": 0}]},
        {"seg": [{"id": 0, "fx": 1}]},
        {"seg": [{"id": 0}]},
        {"transition": 101},
        {"n": "Metadata only"},
    ],
)
def test_even_fingerprint_pinned_unsafe_or_dynamic_presets_are_refused(
    make_app: AppFactory, fake_http: FakeHttp, preset: dict[str, Any]
) -> None:
    app = make_app(config(allow_global=True, preset=preset))
    fake_http.add("GET", f"{BASE}/presets.json", body=json.dumps({"1": preset}))

    result = app.call(
        "wled_apply_preset", bound_args(app, {"device": "desk", "preset": "evening"}), Origin.HUMAN
    )

    assert result.status == "error"
    assert len(fake_http.requests) == 1


@pytest.mark.parametrize(
    "preset",
    [
        {"on": True},
        {"bri": 96},
        {"transition": 4},
        {"seg": [{"id": 2, "on": True}]},
        {"seg": [{"id": 0, "on": True}, {"id": 0, "on": False}]},
        {"seg": [{"id": 0, "col": [[10, 20, 30]]}]},
        {"seg": [{"id": 0, "col": [[10, 20, 30, 0]], "fx": 0}]},
    ],
)
def test_reviewed_presets_still_obey_scope_and_segment_capabilities(
    make_app: AppFactory, fake_http: FakeHttp, preset: dict[str, Any]
) -> None:
    app = make_app(config(preset=preset))
    fake_http.add("GET", f"{BASE}/presets.json", body=json.dumps({"1": preset}))
    fake_http.add("GET", f"{BASE}/json/si", body=json.dumps(fixture()))

    result = app.call(
        "wled_apply_preset", bound_args(app, {"device": "desk", "preset": "evening"}), Origin.HUMAN
    )

    assert result.status == "error"
    assert len(fake_http.requests) == 2
    assert all(request.method == "GET" for request in fake_http.requests)


def test_globally_reviewed_preset_requires_and_honors_global_opt_in(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    preset = fixture("presets.json")["2"]
    app = make_app(config(allow_global=True, preset=preset))
    fake_http.add("GET", f"{BASE}/presets.json", body=json.dumps({"1": preset}))
    patch = {"on": True, "bri": 96, "transition": 4}
    queue_action(fake_http, patch)

    result = app.call(
        "wled_apply_preset", bound_args(app, {"device": "desk", "preset": "evening"}), Origin.HUMAN
    )

    assert result.data["confirmed"] is True
    post = next(request for request in fake_http.requests if request.method == "POST")
    assert json.loads(post.body or b"") == {**patch, "udpn": {"nn": True}}

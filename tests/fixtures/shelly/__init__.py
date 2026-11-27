"""Synthetic Shelly devices and exact-request fake responses. Authored tests are UNRUN."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from switchboard.httpclient import HttpResponse
from tests.fakes import FakeHttp, RecordedRequest

BASE = "http://192.168.10.40"
DEVICE_ID = "shellyplusplugs-000000000001"
MODEL = "SNPL-00112EU"
PASSWORD = "synthetic-only-shelly-password"
REFERENCE = "env:SB_TEST_SHELLY_PASSWORD"
_FIXTURES = Path(__file__).parent


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((_FIXTURES / name).read_text(encoding="ascii"))
    return data


def config(*, controls: bool = False, password: bool = False, **overrides: Any) -> dict[str, Any]:
    device: dict[str, Any] = {
        "base_url": BASE,
        "allow_http": True,
        "expected_device_id": DEVICE_ID,
        "expected_model": MODEL,
        "expected_generation": 2,
        "switch_ids": [0],
    }
    if password:
        device["password"] = REFERENCE
    if controls:
        device["allow_control"] = True
        device["controls"] = {
            "0": {"safe_load": "nonessential_lighting", "max_power_watts": 60, "safety_acknowledged": True}
        }
    device.update(overrides)
    return {
        "switchboard": {"connectors": ["shelly"]},
        "connectors": {"shelly": {"devices": {"desk_lamp": device}}},
    }


def rpc_frame(request: RecordedRequest, result: Any, /, **overrides: Any) -> HttpResponse:
    payload = json.loads(request.body or b"{}")
    frame = {"id": payload["id"], "src": DEVICE_ID, "dst": "switchboard", "result": result}
    frame.update(overrides)
    return HttpResponse(200, request.url, {"content-type": "application/json"}, json.dumps(frame).encode())


def respond(http: FakeHttp, method: str, result: Any, *, params: dict[str, Any] | None = None) -> None:
    def handler(request: RecordedRequest) -> HttpResponse:
        payload = json.loads(request.body or b"{}")
        assert payload["method"] == method
        assert payload["src"] == "switchboard"
        assert set(payload) == {"id", "src", "method", "params"}
        if params is not None:
            assert payload["params"] == params
        return rpc_frame(request, result)

    http.add_handler("POST", BASE + "/rpc", handler, repeat=False)


def identity(http: FakeHttp, *, authenticated: bool = False, **overrides: Any) -> None:
    result = fixture("device_info.json")
    result["auth_en"] = authenticated
    result.update(overrides)
    respond(http, "Shelly.GetDeviceInfo", result, params={"ident": False})


def status(http: FakeHttp, *, output: bool = False, **overrides: Any) -> None:
    result = fixture("switch_status.json")
    result["output"] = output
    result.update(overrides)
    respond(http, "Switch.GetStatus", result, params={"id": 0})


def accepted(http: FakeHttp, *, on: bool = True, was_on: bool = False) -> None:
    respond(http, "Switch.Set", {"was_on": was_on}, params={"id": 0, "on": on})


def challenge(http: FakeHttp, *, nonce: str = "AAAAAABnSyntheticToken==", **overrides: str) -> None:
    values = {
        "qop": '"auth"',
        "realm": f'"{DEVICE_ID}"',
        "nonce": f'"{nonce}"',
        "algorithm": "SHA-256",
    }
    values.update(overrides)
    header = "Digest " + ", ".join(f"{key}={value}" for key, value in values.items())
    http.add("POST", BASE + "/rpc", status=401, headers={"WWW-Authenticate": header})


def methods(http: FakeHttp) -> list[str]:
    return [json.loads(request.body or b"{}")["method"] for request in http.requests]

"""HTTP faults injected through a fault transport; authored and unrun.

Every request fails in one configured way: an ``HttpError`` (connection reset, timeout, response byte cap)
or a response with a server error, a client error, or a malformed, truncated, non-object, non-UTF-8, or
deeply nested body. Reads must report an error without leaking provider detail into the outcome, the
protocol reply, or the audit log, and must not be retried. A write whose transport failed must be recorded as
uncertain and must block an identical repeat until the operator reconciles it (contracts/README.md).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from switchboard.app import App
from switchboard.errors import HttpError, ToolError
from switchboard.httpclient import HttpResponse
from switchboard.mcp_server import McpServer
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool

pytestmark = pytest.mark.faults

AppFactory = Callable[..., App]
MARKER = "synthetic-private-fault-detail"
DEVICE = "https://device.example.test/state"
WLED = "http://192.168.50.20"
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
TRANSPORT_FAULTS = ("reset", "timeout", "byte_limit")
RESPONSE_FAULTS: dict[str, tuple[int, bytes]] = {
    "status_500": (500, b'{"error":"' + MARKER.encode("ascii") + b'"}'),
    "status_404": (404, MARKER.encode("ascii")),
    "malformed": (200, b'{"state":'),
    "truncated": (200, b'{"state":{"on":true,"bri":12'),
    "not_object": (200, b'["' + MARKER.encode("ascii") + b'"]'),
    "not_utf8": (200, b'{"text":"\xff\xfe"}'),
    "deeply_nested": (200, b"[" * 5000 + b"]" * 5000),
}
FAULTS = (*TRANSPORT_FAULTS, *RESPONSE_FAULTS)


class FaultTransport:
    """An ``HttpTransport`` that fails every request in one configured way and records each attempt."""

    def __init__(self, fault: str) -> None:
        self.fault = fault
        self.requests: list[str] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        allow_http: bool = False,
        follow_redirects: bool = True,
        public_only: bool = False,
    ) -> HttpResponse:
        self.requests.append(f"{method} {url}")
        if self.fault in TRANSPORT_FAULTS:
            raise HttpError(f"{self.fault.replace('_', ' ')} after the request was sent ({MARKER})")
        status, payload = RESPONSE_FAULTS[self.fault]
        return HttpResponse(
            status=status, url=url, headers={"content-type": "application/json"}, body=payload
        )


def add_probes(app: App) -> None:
    """A read and an act-tier write that each make exactly one request through the app's transport."""

    def read(_args: dict[str, Any]) -> dict[str, Any]:
        response = app.ctx.http.request("GET", DEVICE, timeout=5.0, max_bytes=65536)
        if not response.ok:
            raise ToolError("the device refused the read")
        data = response.json()
        if not isinstance(data, dict):
            raise ToolError("the device returned an unexpected document")
        return {"state": data}

    def write(args: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"target": args["target"], "on": False}).encode("utf-8")
        response = app.ctx.http.request("POST", DEVICE, body=payload, timeout=5.0, max_bytes=65536)
        return {"status": response.status, "confirmed": False}

    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    target = {
        "type": "object",
        "properties": {"target": {"type": "string", "enum": ["lamp", "desk"]}},
        "required": ["target"],
        "additionalProperties": False,
    }
    app.registry.add(
        Tool("fault_read", "Synthetic read through the fault transport.", Tier.READ, empty, read)
    )
    app.registry.add(
        Tool("fault_write", "Synthetic write through the fault transport.", Tier.ACT, target, write)
    )


def tool_call(request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments, "_meta": MODERN},
    }


@pytest.mark.parametrize("fault", FAULTS)
def test_read_faults_are_reported_without_leaking_or_retrying(make_app: AppFactory, fault: str) -> None:
    transport = FaultTransport(fault)
    app = make_app(http=transport)
    add_probes(app)

    outcome = app.call("fault_read", {}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert outcome.data is None
    assert outcome.action is None
    assert MARKER not in outcome.message
    assert len(transport.requests) == 1

    reply = McpServer(tools=app.registry.all, call=app.dispatcher.call).handle(tool_call(7, "fault_read", {}))
    assert reply is not None
    assert reply["result"]["isError"] is True
    assert MARKER not in json.dumps(reply, default=str)
    assert len(transport.requests) == 2
    assert MARKER not in app.ctx.audit.path.read_text(encoding="utf-8")
    assert app.ctx.audit.verify().ok


@pytest.mark.parametrize("fault", FAULTS)
def test_device_connector_reads_report_faults(make_app: AppFactory, fault: str) -> None:
    transport = FaultTransport(fault)
    device = {"name": "desk", "base_url": WLED, "allow_http": True, "allow_global": False, "segments": [0, 1]}
    app = make_app(
        {"switchboard": {"connectors": ["wled"]}, "connectors": {"wled": {"devices": [device]}}},
        http=transport,
    )

    outcome = app.call("wled_status", {"device": "desk"}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert outcome.data is None
    assert MARKER not in outcome.message
    assert len(transport.requests) == 1
    assert app.ctx.audit.verify().ok


@pytest.mark.parametrize("fault", TRANSPORT_FAULTS)
def test_transport_failure_during_a_write_blocks_identical_repeats(make_app: AppFactory, fault: str) -> None:
    transport = FaultTransport(fault)
    app = make_app(http=transport)
    add_probes(app)

    first = app.call("fault_write", {"target": "lamp"}, Origin.HUMAN)
    repeat = app.call("fault_write", {"target": "lamp"}, Origin.HUMAN)
    other = app.call("fault_write", {"target": "desk"}, Origin.HUMAN)

    assert first.status == "error"
    assert first.action is not None
    assert first.action["state"] == "uncertain"
    assert MARKER not in first.message
    assert repeat.status == "denied"
    assert other.status == "error"
    assert transport.requests == [f"POST {DEVICE}", f"POST {DEVICE}"]
    assert app.ctx.audit.verify().ok

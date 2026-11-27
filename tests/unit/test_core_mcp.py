"""MCP server: both protocol eras, error mapping, and the stdio framing."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from switchboard.mcp_server import SUPPORTED_VERSIONS, McpServer, serve_stdio
from switchboard.policy import Origin, Tier
from switchboard.tools import CallOutcome, Tool

MODERN = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
}


def _server(calls: list[tuple[str, dict[str, Any], Origin]] | None = None) -> McpServer:
    echo_schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    tool = Tool("demo_echo", "Echo.", Tier.READ, echo_schema, lambda args: args)
    recorded = calls if calls is not None else []

    def call(name: str, args: dict[str, Any], origin: Origin) -> CallOutcome:
        recorded.append((name, args, origin))
        if args.get("text") == "fail":
            return CallOutcome("error", name, message="nope")
        return CallOutcome("ok", name, data=args)

    return McpServer(tools=lambda: [tool], call=call)


def _request(method: str, params: dict[str, Any] | None = None, msg_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


def _reply(server: McpServer, message: dict[str, Any]) -> dict[str, Any]:
    reply = server.handle(message)
    assert reply is not None
    return reply


def test_legacy_clients_initialize_then_list_tools() -> None:
    server = _server()
    init = _reply(server, _request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "switchboard"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _reply(server, _request("ping", msg_id=2))["result"] == {}
    listing = _reply(server, _request("tools/list", msg_id=3))["result"]
    assert [tool["name"] for tool in listing["tools"]] == ["demo_echo"]
    assert "resultType" not in listing


def test_legacy_clients_asking_for_an_unknown_version_get_the_latest_legacy_one() -> None:
    reply = _reply(_server(), _request("initialize", {"protocolVersion": "1999-01-01"}))
    assert reply["result"]["protocolVersion"] == "2025-11-25"


def test_modern_clients_discover_and_list_without_a_handshake() -> None:
    server = _server()
    discover = _reply(server, _request("server/discover", MODERN))["result"]
    assert discover["resultType"] == "complete"
    assert discover["supportedVersions"] == ["2026-07-28"]
    assert discover["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "switchboard"
    listing = _reply(server, _request("tools/list", MODERN, msg_id=2))["result"]
    assert listing["resultType"] == "complete"
    assert listing["cacheScope"] == "private"
    assert listing["ttlMs"] > 0
    assert listing["tools"][0]["annotations"]["readOnlyHint"] is True


def test_unknown_modern_versions_are_rejected_with_the_supported_list() -> None:
    meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}}
    error = _reply(_server(), _request("tools/list", meta))["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": list(SUPPORTED_VERSIONS), "requested": "1900-01-01"}


def test_requests_without_a_handshake_or_version_are_refused() -> None:
    assert _reply(_server(), _request("tools/list"))["error"]["code"] == -32600


def test_ping_no_longer_exists_in_the_modern_protocol() -> None:
    assert _reply(_server(), _request("ping", MODERN))["error"]["code"] == -32601


def test_tool_call_results_and_errors() -> None:
    calls: list[tuple[str, dict[str, Any], Origin]] = []
    server = _server(calls)
    params = {**MODERN, "name": "demo_echo", "arguments": {"text": "hi"}}
    ok = _reply(server, _request("tools/call", params))["result"]
    assert ok["isError"] is False
    assert ok["resultType"] == "complete"
    assert ok["structuredContent"]["data"] == {"text": "hi"}
    assert '"hi"' in ok["content"][0]["text"]
    assert calls[0] == ("demo_echo", {"text": "hi"}, Origin.ASSISTANT)

    failing = {**MODERN, "name": "demo_echo", "arguments": {"text": "fail"}}
    assert _reply(server, _request("tools/call", failing, msg_id=2))["result"]["isError"] is True


def test_unknown_or_hidden_tools_are_protocol_errors() -> None:
    params = {**MODERN, "name": "mail_send", "arguments": {}}
    error = _reply(_server(), _request("tools/call", params))["error"]
    assert error == {"code": -32602, "message": "Unknown tool: mail_send"}


def test_malformed_messages() -> None:
    server = _server()
    assert _reply(server, {"id": 1, "method": "tools/list"})["error"]["code"] == -32600
    assert server.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None


def test_stdio_framing_handles_bad_lines() -> None:
    lines = [
        json.dumps(_request("server/discover", MODERN)).encode(),
        b"not json",
        b"",
        json.dumps(_request("tools/list", MODERN, msg_id=2)).encode(),
    ]
    output = io.BytesIO()
    serve_stdio(_server(), [line + b"\n" for line in lines], output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(replies) == 3
    assert replies[0]["result"]["resultType"] == "complete"
    assert replies[1]["error"]["code"] == -32700
    assert replies[2]["id"] == 2


def test_a_lone_surrogate_cannot_break_the_output_stream() -> None:
    meta = json.dumps(MODERN["_meta"])
    raw = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"_meta":'
        + meta
        + ',"name":"\\ud800","arguments":{}}}'
    ).encode("ascii")
    output = io.BytesIO()
    serve_stdio(_server(), [raw + b"\n"], output)
    reply = json.loads(output.getvalue())
    assert reply["error"]["code"] == -32602
    assert reply["error"]["message"] == "Unknown tool: \ud800"
    output.getvalue().decode("ascii")  # the wire format stays ASCII-only


@pytest.mark.parametrize("arguments", [None, [], "", False, 0])
def test_falsey_non_object_arguments_do_not_become_empty_arguments(arguments: Any) -> None:
    calls: list[tuple[str, dict[str, Any], Origin]] = []
    params = {**MODERN, "name": "demo_echo", "arguments": arguments}
    error = _reply(_server(calls), _request("tools/call", params))["error"]
    assert error["code"] == -32602
    assert calls == []


@pytest.mark.parametrize("params", [None, [], "", False, 0])
def test_falsey_non_object_parameters_are_invalid(params: Any) -> None:
    error = _reply(_server(), {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": params})[
        "error"
    ]
    assert error["code"] == -32600


@pytest.mark.parametrize("msg_id", [None, True, [], {}, 1.5])
def test_request_identifiers_follow_the_protocol(msg_id: Any) -> None:
    reply = _reply(_server(), {"jsonrpc": "2.0", "id": msg_id, "method": "server/discover", "params": MODERN})
    assert reply["id"] is None
    assert reply["error"]["code"] == -32600


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_non_finite_json_is_a_parse_error(literal: str) -> None:
    output = io.BytesIO()
    serve_stdio(_server(), [f'{{"value":{literal}}}\n'.encode()], output)
    assert json.loads(output.getvalue())["error"]["code"] == -32700


def test_an_empty_batch_is_an_invalid_request() -> None:
    output = io.BytesIO()
    serve_stdio(_server(), [b"[]\n"], output)
    assert json.loads(output.getvalue())["error"]["code"] == -32600


def test_batches_are_supported_for_initialized_2025_03_26_clients() -> None:
    output = io.BytesIO()
    lines = [
        _request("initialize", {"protocolVersion": "2025-03-26"}),
        [_request("tools/list", msg_id=2), _request("ping", msg_id=3)],
    ]
    serve_stdio(_server(), [json.dumps(line).encode() + b"\n" for line in lines], output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [reply["id"] for reply in replies[1]] == [2, 3]


@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25", "2026-07-28"])
def test_newer_protocols_cannot_execute_batched_tool_calls(version: str) -> None:
    output = io.BytesIO()
    calls: list[tuple[str, dict[str, Any], Origin]] = []
    initialization = _request("initialize", {"protocolVersion": version})
    batch = [_request("tools/call", {**MODERN, "name": "demo_echo", "arguments": {}})]
    serve_stdio(_server(calls), [json.dumps(value).encode() for value in (initialization, batch)], output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[-1]["error"]["code"] == -32600
    assert calls == []


def test_tool_calls_without_identifiers_are_not_executed() -> None:
    calls: list[tuple[str, dict[str, Any], Origin]] = []
    result = _server(calls).handle(
        {"jsonrpc": "2.0", "method": "tools/call", "params": {**MODERN, "name": "demo_echo"}}
    )
    assert result is None
    assert calls == []

"""Protocol-stream faults for the standard input/output server loop; authored and unrun.

Malformed, non-UTF-8, oversized, deeply nested, and wrongly shaped lines must each get one error reply while
the session keeps answering later requests.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any

import pytest

from switchboard.app import App
from switchboard.mcp_server import McpServer, serve_stdio

pytestmark = pytest.mark.faults

AppFactory = Callable[..., App]
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
FINAL = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {"_meta": MODERN}}).encode()


def run(app: App, lines: list[bytes]) -> list[dict[str, Any]]:
    writer = io.BytesIO()
    serve_stdio(McpServer(tools=app.registry.all, call=app.dispatcher.call), lines, writer)
    replies = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert all(isinstance(reply, dict) for reply in replies)
    return replies


def test_malformed_lines_each_get_one_error_and_the_session_continues(make_app: AppFactory) -> None:
    lines = [
        b"{not json\n",
        b"\xff\xfe\xfd\n",
        b"[]\n",
        b"42\n",
        b'{"jsonrpc":"2.0","id":{"nested":true},"method":"ping"}\n',
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":"not an object"}\n',
        b'{"jsonrpc":"1.0","id":2,"method":"tools/list"}\n',
        b'"' + b"x" * 1_000_000 + b'"\n',
        b"[" + b"1," * 50_000 + b"1]\n",
        b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"no_such_tool","_meta":'
        + json.dumps(MODERN).encode()
        + b"}}\n",
        b"\n",
        FINAL + b"\n",
    ]

    replies = run(make_app(), lines)

    assert len(replies) == len(lines) - 1
    assert all("error" in reply for reply in replies[:-1])
    assert replies[-1]["id"] == 99
    assert replies[-1]["result"]["tools"]


def test_deeply_nested_line_gets_an_error_and_the_session_continues(make_app: AppFactory) -> None:
    replies = run(make_app(), [b"[" * 100_000 + b"]" * 100_000 + b"\n", FINAL + b"\n"])

    assert len(replies) == 2
    assert replies[0]["error"]["message"] == "JSON nested too deeply"
    assert replies[1]["id"] == 99

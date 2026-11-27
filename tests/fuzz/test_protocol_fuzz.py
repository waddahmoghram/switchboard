"""Seeded mutation fuzzing of the protocol request handler.

Mutated JSON-RPC messages go to ``McpServer.handle`` for an app with no connectors, so only core read tools
exist and nothing leaves the process. Every message must produce either no reply (a notification) or one
well-formed reply with exactly one of ``result`` and ``error`` that serializes the way the stdio loop writes
it, and the handler must never raise.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from switchboard.app import App
from switchboard.mcp_server import McpServer
from switchboard.util import json_loads
from tests.fuzz.mutation import generator, iterations, mutate

pytestmark = pytest.mark.fuzz

AppFactory = Callable[..., App]
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
SEEDS: tuple[dict[str, Any], ...] = (
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN}},
    {"jsonrpc": "2.0", "id": "a", "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "switchboard_status", "_meta": MODERN},
    },
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "actions_recent",
            "arguments": {"state": "uncertain", "limit": 5},
            "_meta": MODERN,
        },
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "approvals_status", "arguments": {"id": "0123abcd"}, "_meta": MODERN},
    },
    {"jsonrpc": "2.0", "id": 5, "method": "server/discover", "params": {"_meta": MODERN}},
    {"jsonrpc": "2.0", "id": 6, "method": "ping"},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
)


def test_handle_never_raises_and_replies_are_well_formed(make_app: AppFactory) -> None:
    app = make_app()
    server = McpServer(tools=app.registry.all, call=app.dispatcher.call)
    rng = generator("mcp_handle")
    corpus = [json.dumps(seed).encode("utf-8") for seed in SEEDS]
    replies = 0
    for _ in range(iterations(400)):
        data = mutate(rng, rng.choice(corpus), corpus, limit=2048)
        try:
            message = json_loads(data)
        except (ValueError, RecursionError):
            continue
        reply = server.handle(message)
        if reply is None:
            continue
        replies += 1
        assert reply["jsonrpc"] == "2.0"
        assert "id" in reply
        assert ("result" in reply) != ("error" in reply)
        if "error" in reply:
            assert isinstance(reply["error"]["code"], int)
            assert isinstance(reply["error"]["message"], str)
        json.dumps(reply, ensure_ascii=True, separators=(",", ":"), default=str)
    assert replies > 0
    assert app.ctx.audit.verify().ok

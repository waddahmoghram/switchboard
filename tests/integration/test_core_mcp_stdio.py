"""End-to-end: the MCP server as a real subprocess speaking newline-delimited JSON-RPC."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parents[2] / "src"


def test_stdio_server_speaks_both_protocol_eras(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[switchboard]\nconnectors = []\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8"
    )
    config.chmod(0o600)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")])),
    }
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "switchboard_status", "arguments": {}},
        },
    ]
    payload = "".join(json.dumps(message) + "\n" for message in messages).encode("utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "switchboard", "--config", str(config), "serve-mcp"],
        input=payload,
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    replies = {reply["id"]: reply for reply in map(json.loads, completed.stdout.decode("utf-8").splitlines())}
    assert sorted(replies) == [1, 2, 3, 4]
    assert replies[1]["result"]["supportedVersions"] == ["2026-07-28"]
    assert replies[2]["result"]["protocolVersion"] == "2025-11-25"
    names = {tool["name"] for tool in replies[3]["result"]["tools"]}
    assert {"switchboard_status", "approvals_pending", "approvals_status"} <= names
    status = replies[4]["result"]["structuredContent"]
    assert status["status"] == "ok"
    assert status["data"]["connectors"] == []

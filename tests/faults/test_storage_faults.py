"""SQLite faults injected at ``Database.connect``; authored and unrun.

The sweep makes the n-th connection attempt raise ``sqlite3.OperationalError`` (as a locked, full, or
unreadable database would) for n = 0, 1, 2, ... until a call completes without reaching the fault. The
protocol layer must answer every request without leaking the synthetic detail, and once the fault is gone the
same call must succeed with an intact audit chain. Startup must refuse a database that is newer than this
engine, not a regular file, or read-only.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.errors import ConfigError
from switchboard.mcp_server import McpServer, serve_stdio
from switchboard.policy import Origin

pytestmark = pytest.mark.faults

AppFactory = Callable[..., App]
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
SYNTHETIC = "synthetic fault: database is locked"
IS_ROOT = os.geteuid() == 0


class ConnectFaults:
    """Delegates to the real ``connect`` except for the scheduled failing attempts."""

    def __init__(
        self, connect: Callable[[], sqlite3.Connection], fail_at: int, *, sticky: bool = False
    ) -> None:
        self._connect = connect
        self.fail_at = fail_at
        self.sticky = sticky
        self.calls = 0
        self.raised = 0

    def __call__(self) -> sqlite3.Connection:
        index = self.calls
        self.calls += 1
        if index == self.fail_at or (self.sticky and index > self.fail_at):
            self.raised += 1
            raise sqlite3.OperationalError(SYNTHETIC)
        return self._connect()


def server_for(app: App) -> McpServer:
    return McpServer(tools=app.registry.all, call=app.dispatcher.call)


def tool_call(request_id: int, name: str = "switchboard_status") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}, "_meta": MODERN},
    }


def answered(reply: dict[str, Any] | None, request_id: int) -> dict[str, Any]:
    assert reply is not None
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == request_id
    assert ("result" in reply) != ("error" in reply)
    assert "synthetic fault" not in json.dumps(reply, default=str)
    return reply


def test_connect_failure_at_each_point_of_a_read_call_is_answered(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    server = server_for(app)
    original = app.ctx.db.connect
    completed = False
    for fail_at in range(64):
        faults = ConnectFaults(original, fail_at)
        with monkeypatch.context() as patch:
            patch.setattr(app.ctx.db, "connect", faults)
            reply = answered(server.handle(tool_call(fail_at)), fail_at)
        if faults.raised == 0:
            assert reply["result"]["isError"] is False
            completed = True
            break
    assert completed, "the call still reached the database fault after 64 connection attempts"
    assert answered(server.handle(tool_call(100)), 100)["result"]["isError"] is False
    assert app.ctx.audit.verify().ok


def test_persistent_database_failure_never_ends_the_protocol_session(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    faults = ConnectFaults(app.ctx.db.connect, 0, sticky=True)
    monkeypatch.setattr(app.ctx.db, "connect", faults)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN}},
        tool_call(2),
        tool_call(3, "approvals_pending"),
        tool_call(4, "actions_recent"),
        {"jsonrpc": "2.0", "id": 5, "method": "server/discover", "params": {"_meta": MODERN}},
    ]
    writer = io.BytesIO()

    serve_stdio(server_for(app), [json.dumps(item).encode("utf-8") + b"\n" for item in requests], writer)

    replies = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3, 4, 5]
    assert faults.raised >= 3
    assert replies[0]["result"]["tools"]
    for reply in replies[1:4]:
        assert "error" in reply or reply["result"]["isError"] is True
    assert "result" in replies[4]
    assert b"synthetic fault" not in writer.getvalue()


def test_newer_database_schema_is_refused_at_startup(make_app: AppFactory, data_dir: Path) -> None:
    make_app()
    with closing(sqlite3.connect(data_dir / "switchboard.sqlite3")) as conn:
        conn.execute(
            "INSERT INTO schema_migrations (component, version, applied_at) VALUES ('core', 99, ?)",
            ("2026-11-11T00:00:00+00:00",),
        )
        conn.commit()

    with pytest.raises(ConfigError, match="newer"):
        make_app()


def test_database_path_that_is_not_a_regular_file_is_refused(make_app: AppFactory, data_dir: Path) -> None:
    data_dir.mkdir(mode=0o700, parents=True)
    (data_dir / "switchboard.sqlite3").mkdir()

    with pytest.raises(ConfigError, match="database"):
        make_app()


@pytest.mark.skipif(IS_ROOT, reason="permission faults are not observable as root")
def test_read_only_database_is_refused_then_recovers(make_app: AppFactory, data_dir: Path) -> None:
    assert make_app().call("switchboard_status", {}, Origin.ASSISTANT).status == "ok"
    path = data_dir / "switchboard.sqlite3"
    path.chmod(0o400)
    try:
        with pytest.raises(ConfigError, match="cannot open the database"):
            make_app()
    finally:
        path.chmod(0o600)

    restored = make_app()
    assert restored.call("switchboard_status", {}, Origin.ASSISTANT).status == "ok"
    assert restored.ctx.audit.verify().ok

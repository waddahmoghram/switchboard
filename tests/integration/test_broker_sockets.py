"""End-to-end broker over real Unix domain sockets in a temporary directory.

Peer identities are injected where a test needs two accounts; one test reads real kernel credentials on Linux.
Both sockets run under the test's single account here, which shows the protocol and authorization paths, not
account separation. Authored, not run on the authoring host.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from switchboard import broker
from switchboard.app import App
from switchboard.policy import Tier
from switchboard.tools import Tool
from tests.conftest import AppFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix domain sockets"),
]

ASSISTANT = broker.PeerCredentials(uid=60001, gid=60001, pid=1111)
OPERATOR = broker.PeerCredentials(uid=60002, gid=60002, pid=2222)
MODERN = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}


@dataclass
class Running:
    app: App
    server: broker.BrokerServer
    thread: threading.Thread
    client: Path
    operator: Path
    sent: list[dict[str, Any]]

    def stop(self) -> None:
        self.server.request_stop()
        self.thread.join(timeout=10)
        self.server.close()


def _settings(socket_dir: Path, **overrides: Any) -> broker.BrokerSettings:
    run = socket_dir / "run"
    run.mkdir(exist_ok=True)
    run.chmod(0o700)
    settings = broker.BrokerSettings(
        client_socket=run / "c.sock",
        operator_socket=run / "o.sock",
        client_uids=frozenset({ASSISTANT.uid}),
        operator_uids=frozenset({OPERATOR.uid}),
    )
    return replace(settings, **overrides)


def _by_socket(settings: broker.BrokerSettings) -> broker.PeerLookup:
    """The assistant connects to the client socket and the operator to the operator socket."""

    def lookup(conn: socket.socket) -> broker.PeerCredentials:
        return ASSISTANT if conn.getsockname() == os.fspath(settings.client_socket) else OPERATOR

    return lookup


def _start(
    make_app: AppFactory,
    settings: broker.BrokerSettings,
    lookup: broker.PeerLookup | None = None,
) -> Running:
    app = make_app()
    sent: list[dict[str, Any]] = []

    def send(args: dict[str, Any]) -> dict[str, Any]:
        sent.append(dict(args))
        return {"sent_to": args["to"]}

    schema = {
        "type": "object",
        "properties": {"to": {"type": "string", "maxLength": 80}},
        "required": ["to"],
        "additionalProperties": False,
    }
    app.registry.add(Tool("demo_send", "Send a synthetic message.", Tier.ACT, schema, send))
    server = broker.BrokerServer(
        broker.Gateway(app),
        settings,
        peer_lookup=lookup or _by_socket(settings),
        in_use=broker.socket_in_use,
        grace_seconds=5.0,
    )
    server.start()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return Running(app, server, thread, settings.client_socket, settings.operator_socket, sent)


@pytest.fixture
def running(make_app: AppFactory, socket_dir: Path) -> Iterator[Running]:
    state = _start(make_app, _settings(socket_dir))
    try:
        yield state
    finally:
        state.stop()


def _exchange(path: Path, *messages: Any) -> list[dict[str, Any]]:
    """Send lines, half-close, and read every reply until the broker closes.

    A refused connection is closed with the request unread, so the kernel can report a reset (or a broken
    pipe while sending) after the refusal line; that ends the exchange like end of input.
    """
    channel = broker.connect_unix(path)
    replies: list[dict[str, Any]] = []
    try:
        with suppress(BrokenPipeError, ConnectionResetError):
            for message in messages:
                channel.send(json.dumps(message).encode("utf-8") + b"\n")
        channel.end_input()
        with suppress(ConnectionResetError):
            for frame in broker.read_frames(channel):
                if frame.strip():
                    replies.append(json.loads(frame))
        return replies
    finally:
        channel.close()


def test_sockets_are_group_mode_660_and_removed_on_close(make_app: AppFactory, socket_dir: Path) -> None:
    state = _start(make_app, _settings(socket_dir))
    try:
        assert state.client.stat().st_mode & 0o777 == 0o660
        assert state.operator.stat().st_mode & 0o777 == 0o660
    finally:
        state.stop()
    assert not state.client.exists()
    assert not state.operator.exists()


def test_the_operator_approves_what_the_assistant_requested_over_separate_sockets(running: Running) -> None:
    replies = _exchange(
        running.client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": MODERN},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {**MODERN, "name": "demo_send", "arguments": {"to": "friend@example.invalid"}},
        },
    )
    assert "demo_send" in {tool["name"] for tool in replies[0]["result"]["tools"]}
    pending = replies[1]["result"]["structuredContent"]
    assert pending["status"] == "pending"
    assert running.sent == []

    with broker.open_operator_client(running.operator) as client:
        assert client.call("status")["data"]["peer_uid"] == OPERATOR.uid
        assert [item["id"] for item in client.call("approvals")["data"]] == [pending["approval_id"]]
        approval = client.call("approval", id=pending["approval_id"])
        assert approval["args"] == {"to": "friend@example.invalid"}
        outcome = client.call("approve", id=pending["approval_id"])

    assert outcome["status"] == "ok"
    assert running.sent == [{"to": "friend@example.invalid"}]
    approvals = [record for record in running.app.ctx.audit.records() if record["kind"] == "operator.approve"]
    assert approvals[0]["peer_uid"] == OPERATOR.uid


def test_the_assistant_identity_is_refused_on_the_operator_socket_and_audited(
    make_app: AppFactory, socket_dir: Path
) -> None:
    state = _start(make_app, _settings(socket_dir), lookup=lambda _conn: ASSISTANT)
    try:
        [refusal] = _exchange(state.operator, {"op": "kill_switch", "enable": True})
        assert (refusal["ok"], refusal["op"], refusal["request_id"]) == (False, None, None)
        assert refusal["error"]["code"] == "forbidden"
        assert not state.app.policy.kill_switch.exists()
        refused = [record for record in state.app.ctx.audit.records() if record["kind"] == "broker.refused"]
        assert refused[0]["peer_uid"] == ASSISTANT.uid
        replies = _exchange(
            state.client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": MODERN}
        )
        assert replies[0]["result"]["tools"]
    finally:
        state.stop()


def test_unlisted_accounts_are_refused_on_the_client_socket(make_app: AppFactory, socket_dir: Path) -> None:
    stranger = broker.PeerCredentials(uid=60003)
    state = _start(make_app, _settings(socket_dir), lookup=lambda _conn: stranger)
    try:
        [refusal] = _exchange(
            state.client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": MODERN}
        )
        assert refusal["error"]["code"] == -32600
        assert "may not use the client socket" in refusal["error"]["message"]
    finally:
        state.stop()


def test_connections_beyond_the_limit_are_refused(make_app: AppFactory, socket_dir: Path) -> None:
    state = _start(make_app, _settings(socket_dir, max_connections=1))
    first = broker.connect_unix(state.client)
    try:
        first.send(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": MODERN}).encode() + b"\n"
        )
        assert json.loads(next(broker.read_frames(first)))["id"] == 1
        [refusal] = _exchange(
            state.client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": MODERN}
        )
        assert "too many connections" in refusal["error"]["message"]
    finally:
        first.close()
        state.stop()


def test_the_shim_relays_a_whole_session(running: Running) -> None:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "switchboard_status", "arguments": {}},
        },
    ]
    stdin = io.BytesIO(b"".join(json.dumps(message).encode("utf-8") + b"\n" for message in messages))
    stdout = io.BytesIO()

    assert broker.run_shim(running.client, stdin=stdin, stdout=stdout) == 0

    replies = {reply["id"]: reply for reply in map(json.loads, stdout.getvalue().splitlines())}
    assert replies[1]["result"]["protocolVersion"] == "2025-11-25"
    assert replies[2]["result"]["structuredContent"]["status"] == "ok"


def test_a_stale_socket_is_replaced_and_a_live_one_is_refused(make_app: AppFactory, socket_dir: Path) -> None:
    settings = _settings(socket_dir)
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    leftover.bind(os.fspath(settings.client_socket))
    leftover.close()
    state = _start(make_app, settings)
    try:
        assert _exchange(state.client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": MODERN})
        second = broker.BrokerServer(broker.Gateway(state.app), settings, peer_lookup=_by_socket(settings))
        with pytest.raises(broker.BrokerError, match="accepting connections"):
            second.start()
        assert state.client.exists()
    finally:
        state.stop()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads SO_PEERCRED from the kernel")
def test_kernel_peer_credentials_identify_the_connecting_account(
    make_app: AppFactory, socket_dir: Path
) -> None:
    uid = os.getuid()
    settings = _settings(socket_dir, client_uids=frozenset({uid}), operator_uids=frozenset({uid}))
    state = _start(make_app, settings, lookup=broker.peer_credentials)
    try:
        with broker.open_operator_client(state.operator) as client:
            assert client.call("status")["data"]["peer_uid"] == uid
    finally:
        state.stop()

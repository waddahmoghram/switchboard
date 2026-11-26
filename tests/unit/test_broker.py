"""Broker: peer credentials, framing, serialized dispatch, operator-only operations, socket files, and the
shim relay. Uses fakes, in-memory streams, and temporary directories; authored, not run on the authoring host.
"""

from __future__ import annotations

import io
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from switchboard import broker
from switchboard.actions import ActionJournal
from switchboard.app import App
from switchboard.config import config_from_dict
from switchboard.errors import ConfigError, UncertainOutcomeError
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool
from tests.conftest import AppFactory

MODERN = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
OPERATOR = broker.PeerCredentials(uid=1000, gid=1000, pid=4242)
ASSISTANT = broker.PeerCredentials(uid=1001, gid=1001, pid=5151)
SEND_SCHEMA = {
    "type": "object",
    "properties": {"to": {"type": "string", "minLength": 1, "maxLength": 80}},
    "required": ["to"],
    "additionalProperties": False,
}


class FakeCredentialSocket:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.requests: list[tuple[int, int, int]] = []

    def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes:
        self.requests.append((level, optname, buflen))
        return self.raw


def _register_send(app: App, calls: list[dict[str, Any]]) -> None:
    def send(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(args))
        return {"sent_to": args["to"]}

    app.registry.add(
        Tool(
            "demo_send",
            "Send a synthetic message.",
            Tier.ACT,
            SEND_SCHEMA,
            send,
            summarize=lambda args: f"send to {args['to']}",
        )
    )


def _pending(app: App, to: str = "someone@example.invalid") -> str:
    outcome = app.dispatcher.call("demo_send", {"to": to}, Origin.ASSISTANT)
    assert outcome.status == "pending"
    assert outcome.approval_id is not None
    return outcome.approval_id


def _session(gateway: broker.Gateway, *messages: Any, raw: bytes = b"") -> list[dict[str, Any]]:
    payload = b"".join(json.dumps(message).encode("utf-8") + b"\n" for message in messages) + raw
    output = io.BytesIO()
    broker.serve_client_connection(gateway, io.BytesIO(payload), output, ASSISTANT)
    return [json.loads(line) for line in output.getvalue().splitlines()]


def _request(method: str, params: dict[str, Any] | None = None, msg_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


def _ok(api: broker.OperatorApi, op: str, **fields: Any) -> Any:
    reply = api.handle({"op": op, "request_id": 7, **fields}, OPERATOR)
    assert reply["ok"] is True, reply
    assert (reply["op"], reply["request_id"]) == (op, 7)
    return reply["result"]


def _error(api: broker.OperatorApi, request: Any, peer: broker.PeerCredentials = OPERATOR) -> dict[str, Any]:
    reply = api.handle(request, peer)
    assert reply["ok"] is False, reply
    assert "result" not in reply
    error = reply["error"]
    assert isinstance(error, dict)
    return error


def _records(app: App, kind: str) -> list[dict[str, Any]]:
    return [record for record in app.ctx.audit.records() if record["kind"] == kind]


# ---- peer credentials -------------------------------------------------------------------------------


def test_linux_peer_credentials_decode_struct_ucred() -> None:
    # Checked here rather than with a skipif mark, so mypy treats the rest as unreachable off Linux.
    if not sys.platform.startswith("linux"):
        pytest.skip("SO_PEERCRED is Linux-only")
    fake = FakeCredentialSocket(struct.pack("=iII", 4242, 1000, 100))
    peer = broker.peer_credentials(fake, platform="linux")
    assert peer == broker.PeerCredentials(uid=1000, gid=100, pid=4242)
    assert fake.requests == [(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)]


def test_macos_peer_credentials_decode_struct_xucred() -> None:
    raw = struct.pack("=IIh2x16I", 0, 501, 1, 20, *([0] * 15))
    assert len(raw) == 76
    fake = FakeCredentialSocket(raw)
    assert broker.peer_credentials(fake, platform="darwin") == broker.PeerCredentials(uid=501, gid=20)
    assert fake.requests == [(0, 0x001, 76)]


@pytest.mark.parametrize(
    "raw",
    [b"", b"\x00" * 8, struct.pack("=IIh2x16I", 1, 501, 1, 20, *([0] * 15))],
)
def test_truncated_or_unknown_xucred_structures_are_refused(raw: bytes) -> None:
    with pytest.raises(broker.PeerCredentialError):
        broker.parse_xucred(raw)


def test_ucred_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(broker.PeerCredentialError):
        broker.parse_ucred(b"\x00" * 8)


def test_platforms_without_peer_credentials_are_refused_rather_than_trusted() -> None:
    with pytest.raises(broker.PeerCredentialError, match="not implemented"):
        broker.peer_credentials(FakeCredentialSocket(b""), platform="win32")
    with pytest.raises(broker.BrokerError):
        broker.check_peer_credential_support("freebsd14")
    broker.check_peer_credential_support("darwin")


def test_authorization_compares_numeric_user_identifiers() -> None:
    assert broker.authorized(OPERATOR, frozenset({1000}))
    assert not broker.authorized(ASSISTANT, frozenset({1000}))
    assert not broker.authorized(broker.PeerCredentials(uid=0), frozenset({1000}))


# ---- framing ----------------------------------------------------------------------------------------


def test_frames_split_lines_and_keep_a_final_partial_line() -> None:
    frames = list(broker.read_frames(io.BytesIO(b"one\ntwo\n\nthree")))
    assert frames == [b"one", b"two", b"", b"three"]


def test_frames_at_the_limit_pass_and_longer_ones_end_the_stream() -> None:
    assert list(broker.read_frames(io.BytesIO(b"abcd\n"), limit=4)) == [b"abcd"]
    frames = broker.read_frames(io.BytesIO(b"ok\nabcde\nnever\n"), limit=4)
    assert next(frames) == b"ok"
    with pytest.raises(broker.FrameTooLargeError):
        next(frames)


def test_encoded_frames_are_single_ascii_lines() -> None:
    line = broker.encode_frame({"text": "\ud800 caf\u00e9\nnext"})
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    line.decode("ascii")


# ---- the client socket ------------------------------------------------------------------------------


def test_client_sessions_list_only_exposed_tools_and_call_as_the_assistant(make_app: AppFactory) -> None:
    app = make_app({"mcp": {"expose": ["switchboard_status", "demo_send"]}})
    calls: list[dict[str, Any]] = []
    _register_send(app, calls)
    gateway = broker.Gateway(app)

    replies = _session(
        gateway,
        _request("tools/list", MODERN),
        _request("tools/call", {**MODERN, "name": "demo_send", "arguments": {"to": "a@example.invalid"}}, 2),
        _request("tools/call", {**MODERN, "name": "approvals_pending", "arguments": {}}, 3),
    )

    assert {tool["name"] for tool in replies[0]["result"]["tools"]} == {"switchboard_status", "demo_send"}
    assert replies[1]["result"]["structuredContent"]["status"] == "pending"
    assert replies[2]["error"]["code"] == -32602
    assert calls == []
    requests = _records(app, "tool.request")
    assert [record["origin"] for record in requests] == ["assistant"]


def test_the_gateway_forces_the_assistant_origin_and_exposure(make_app: AppFactory) -> None:
    app = make_app({"mcp": {"expose": ["demo_send"]}})
    calls: list[dict[str, Any]] = []
    _register_send(app, calls)
    gateway = broker.Gateway(app)

    claimed_human = gateway.call_tool("demo_send", {"to": "a@example.invalid"}, Origin.HUMAN)
    hidden = gateway.call_tool("switchboard_status", {}, Origin.HUMAN)

    assert claimed_human.status == "pending"
    assert calls == []
    assert hidden.status == "invalid"


def test_hints_behind_a_broker_name_the_operator_client(make_app: AppFactory) -> None:
    app = make_app({"mcp": {"expose": ["demo_send"]}})
    _register_send(app, [])
    gateway = broker.Gateway(app)

    pending = gateway.call_tool("demo_send", {"to": "a@example.invalid"}, Origin.ASSISTANT)

    assert pending.approval_id is not None
    assert f"'switchboard operator approvals approve {pending.approval_id}'" in pending.message
    assert "'switchboard approvals approve" not in pending.message
    assert "'switchboard operator approvals approve <id>'" in gateway.instructions
    assert "'switchboard operator actions resolve <id>'" in gateway.instructions
    assert "'switchboard actions resolve" not in gateway.instructions


def test_operator_hint_rewriting_is_exact_and_idempotent() -> None:
    direct = "run 'switchboard approvals approve 0123abcd' or 'switchboard actions resolve " + "a" * 32 + "'"
    once = broker.operator_hints(direct)
    assert once == (
        "run 'switchboard operator approvals approve 0123abcd' or "
        "'switchboard operator actions resolve " + "a" * 32 + "'"
    )
    assert broker.operator_hints(once) == once
    assert broker.operator_hints("switchboard approvals approve without quotes") == (
        "switchboard approvals approve without quotes"
    )


def test_errors_stored_with_an_approval_behind_a_broker_name_the_operator_client(
    make_app: AppFactory,
) -> None:
    app = make_app({"mcp": {"expose": ["demo_unsure", "approvals_status"]}})

    def unsure(args: dict[str, Any]) -> dict[str, Any]:
        raise UncertainOutcomeError("the reply was lost")

    app.registry.add(
        Tool(
            "demo_unsure",
            "A synthetic write whose outcome is unknown.",
            Tier.ACT,
            SEND_SCHEMA,
            unsure,
            summarize=lambda args: f"unsure about {args['to']}",
        )
    )
    gateway = broker.Gateway(app)
    pending = gateway.call_tool("demo_unsure", {"to": "a@example.invalid"}, Origin.ASSISTANT)
    assert pending.approval_id is not None

    approved = _ok(broker.OperatorApi(gateway), "approve", id=pending.approval_id)
    status = gateway.call_tool("approvals_status", {"id": pending.approval_id}, Origin.ASSISTANT)

    action_id = approved["action"]["id"]
    assert approved["action"]["state"] == "uncertain"
    assert f"'switchboard operator actions resolve {action_id}'" in approved["message"]
    assert f"'switchboard operator actions resolve {action_id}'" in status.data["error"]
    assert "'switchboard actions resolve" not in status.data["error"]


def test_operator_requests_on_the_client_socket_are_protocol_errors(make_app: AppFactory) -> None:
    app = make_app()
    calls: list[dict[str, Any]] = []
    _register_send(app, calls)
    approval_id = _pending(app)
    gateway = broker.Gateway(app)

    replies = _session(
        gateway,
        {"op": "approve", "id": approval_id, "request_id": 1},
        _request("initialize", {"protocolVersion": "2025-11-25"}),
        _request("approve", {"id": approval_id}, 2),
        _request("kill_switch", {"enable": True}, 3),
    )

    assert replies[0]["error"]["code"] == -32600
    assert replies[2]["error"]["code"] == -32601
    assert replies[3]["error"]["code"] == -32601
    request = app.approvals.get(approval_id)
    assert request is not None and request.status == "pending"
    assert calls == []
    assert not app.policy.kill_switch.exists()


def test_an_oversized_client_line_ends_the_session(make_app: AppFactory) -> None:
    gateway = broker.Gateway(make_app())
    oversized = b"x" * (broker.MAX_LINE_BYTES + 1) + b"\n"
    after = json.dumps(_request("tools/list", MODERN)).encode("utf-8") + b"\n"

    replies = _session(gateway, raw=oversized + after)

    assert len(replies) == 1
    assert replies[0]["error"]["code"] == -32600
    assert "exceeded" in replies[0]["error"]["message"]


def test_the_gateway_never_runs_two_operations_at_once(make_app: AppFactory) -> None:
    gateway = broker.Gateway(make_app())
    guard = threading.Lock()
    active = 0
    peak = 0

    def operation() -> None:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with guard:
            active -= 1

    threads = [threading.Thread(target=gateway.run, args=(operation,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert peak == 1


def test_a_draining_gateway_waits_for_the_running_operation_then_refuses_new_ones(
    make_app: AppFactory,
) -> None:
    gateway = broker.Gateway(make_app())
    started = threading.Event()
    release = threading.Event()

    def slow() -> None:
        started.set()
        release.wait(timeout=10)

    worker = threading.Thread(target=gateway.run, args=(slow,))
    worker.start()
    assert started.wait(timeout=10)
    assert gateway.drain(0.05) is False
    release.set()
    worker.join(timeout=10)
    assert gateway.drain(1.0) is True
    with pytest.raises(broker.BrokerClosedError):
        gateway.run(lambda: 1)


# ---- the operator interface -------------------------------------------------------------------------


def test_operator_status_reports_the_engine_counts_and_the_peer(make_app: AppFactory) -> None:
    result = _ok(broker.OperatorApi(broker.Gateway(make_app())), "status")
    assert (result["status"], result["tool"]) == ("ok", "switchboard_status")
    data = result["data"]
    assert data["backend"] == "python"
    assert data["pending_approvals"] == 0
    assert data["kill_switch"] is False
    assert data["open_actions"] == {"in_flight": 0, "uncertain": 0}
    assert data["same_user_sandbox"] is False
    assert data["peer_uid"] == OPERATOR.uid
    assert len(data["audit_head"]) == 64


def test_operator_reviews_then_approves_exactly_the_stored_arguments(make_app: AppFactory) -> None:
    app = make_app()
    calls: list[dict[str, Any]] = []
    _register_send(app, calls)
    approval_id = _pending(app, "reviewed@example.invalid")
    api = broker.OperatorApi(broker.Gateway(app))

    listed = _ok(api, "approvals")
    assert (listed["status"], listed["tool"]) == ("ok", "approvals_pending")
    assert [item["id"] for item in listed["data"]] == [approval_id]
    assert "args" not in listed["data"][0]
    shown = _ok(api, "approval", id=approval_id)
    assert (shown["status"], shown["args"]) == ("pending", {"to": "reviewed@example.invalid"})

    executed = _ok(api, "approve", id=approval_id)
    assert executed["status"] == "ok"
    assert calls == [{"to": "reviewed@example.invalid"}]
    again = _ok(api, "approve", id=approval_id)
    assert again["status"] == "error"
    assert calls == [{"to": "reviewed@example.invalid"}]

    records = _records(app, "operator.approve")
    assert [record["approval"] for record in records] == [approval_id, approval_id]
    assert (records[0]["peer_uid"], records[0]["peer_pid"]) == (OPERATOR.uid, OPERATOR.pid)


def test_operator_deny_and_unknown_or_malformed_approvals(make_app: AppFactory) -> None:
    app = make_app()
    _register_send(app, [])
    approval_id = _pending(app)
    api = broker.OperatorApi(broker.Gateway(app))

    assert _ok(api, "deny", id=approval_id) == {"id": approval_id, "status": "denied"}
    request = app.approvals.get(approval_id)
    assert request is not None and request.status == "denied"
    assert _ok(api, "deny", id=approval_id)["status"] == "error"
    assert _ok(api, "deny", id="ffffffff")["status"] == "error"
    assert _ok(api, "deny", id="XYZ")["status"] == "invalid"
    assert _ok(api, "approve", id="ffffffff")["status"] == "invalid"
    assert _error(api, {"op": "approval", "id": "XYZ"})["code"] == "invalid"
    assert _error(api, {"op": "approval", "id": "ffffffff"})["code"] == "invalid"
    assert _error(api, {"op": "deny", "id": 5})["code"] == "invalid"
    assert _records(app, "operator.deny")[0]["peer_uid"] == OPERATOR.uid


def test_operator_lists_shows_and_reconciles_journal_actions(make_app: AppFactory) -> None:
    app = make_app()
    journal = ActionJournal(app.ctx.db)
    attempt = journal.begin(
        tool="demo_send",
        args={"to": "x@example.invalid"},
        origin="assistant",
        approval_id=None,
        timeout_seconds=30,
    )
    journal.finish(attempt.id, "uncertain", error="the connection dropped")
    api = broker.OperatorApi(broker.Gateway(app))

    listed = _ok(api, "actions", state="uncertain", limit=5)
    assert [item["id"] for item in listed["actions"]] == [attempt.id]
    assert "args" not in listed["actions"][0]
    one = _ok(api, "actions", id=attempt.id)["action"]
    assert one["args"] == {"to": "x@example.invalid"}
    assert "owner" not in one
    for refused in (
        {"id": attempt.id, "state": "uncertain"},
        {"limit": 101},
        {"limit": 0},
        {"state": "done"},
    ):
        assert _ok(api, "actions", **refused)["status"] == "invalid"
    assert _error(api, {"op": "actions", "limit": True})["code"] == "invalid"
    assert _error(api, {"op": "actions", "state": 5})["code"] == "invalid"

    for bad in (
        {"id": attempt.id, "resolution": "maybe", "note": "checked"},
        {"id": attempt.id, "resolution": "applied", "note": "   "},
        {"id": attempt.id, "resolution": "applied", "note": "x" * 501},
        {"id": "nothex", "resolution": "applied", "note": "checked"},
        {"id": "0" * 32, "resolution": "applied", "note": "checked"},
    ):
        assert _ok(api, "reconcile", **bad)["status"] == "invalid"
    assert _error(api, {"op": "reconcile", "id": attempt.id, "resolution": "applied"})["code"] == "invalid"

    resolved = _ok(api, "reconcile", id=attempt.id, resolution="applied", note="found it in the outbox")
    assert resolved["status"] == "ok"
    assert resolved["action"]["state"] == "reconciled_applied"
    assert "nothing was repeated" in resolved["message"]
    repeat = _ok(api, "reconcile", id=attempt.id, resolution="not_applied", note="again")
    assert repeat["status"] == "error"
    [record] = _records(app, "action.reconciled")
    assert record["peer_uid"] == OPERATOR.uid
    assert (record["tool"], record["state"]) == ("demo_send", "reconciled_applied")


def test_operator_kill_switch_reports_switches_and_audits(make_app: AppFactory) -> None:
    app = make_app()
    api = broker.OperatorApi(broker.Gateway(app))

    report = _ok(api, "kill_switch")
    assert (report["status"], report["kill_switch"], report["changed"]) == ("ok", False, False)
    assert _records(app, "kill_switch.on") == []
    assert (_ok(api, "kill_switch", enable=True)["kill_switch"], app.policy.kill_switch.exists()) == (
        True,
        True,
    )
    assert app.policy.kill_switch.stat().st_mode & 0o777 == 0o600
    assert _ok(api, "kill_switch", enable=True)["changed"] is False
    off = _ok(api, "kill_switch", enable=False)
    assert (off["kill_switch"], off["changed"]) == (False, True)
    assert _error(api, {"op": "kill_switch", "enable": "yes"})["code"] == "invalid"

    on_records, off_records = _records(app, "kill_switch.on"), _records(app, "kill_switch.off")
    assert [record["changed"] for record in on_records] == [True, False]
    assert len(off_records) == 1
    assert on_records[0]["via"] == off_records[0]["via"] == "operator_socket"
    assert on_records[0]["peer_uid"] == off_records[0]["peer_uid"] == OPERATOR.uid


@pytest.mark.parametrize(
    "request_body",
    [
        [],
        "status",
        {},
        {"op": 5},
        {"op": ""},
        {"op": "x" * 33},
        {"op": "status", "extra": 1},
        {"op": "status", "args": {}},
        {"op": "approvals.list"},
        {"op": "approval"},
        {"op": "approve", "id": "1a2b3c4d", "args_sha256": "0" * 64},
        {"op": "reconcile", "id": "a" * 32, "resolution": "applied"},
        {"op": "kill_switch", "enable": 1},
        {"op": "status", "request_id": True},
        {"op": "status", "request_id": 1.5},
        {"op": "status", "request_id": "x" * 129},
    ],
)
def test_requests_outside_the_protocol_are_refused(make_app: AppFactory, request_body: Any) -> None:
    error = _error(broker.OperatorApi(broker.Gateway(make_app())), request_body)
    assert error["code"] == "invalid"


def test_responses_echo_the_operation_and_a_well_formed_request_id(make_app: AppFactory) -> None:
    api = broker.OperatorApi(broker.Gateway(make_app()))
    assert api.handle({"op": "status", "request_id": "abc"}, OPERATOR)["request_id"] == "abc"
    assert api.handle({"op": "status", "request_id": 0}, OPERATOR)["request_id"] == 0
    refused = api.handle({"op": "status", "request_id": True}, OPERATOR)
    assert (refused["ok"], refused["op"], refused["request_id"]) == (False, "status", None)
    unknown = api.handle({"op": "nonesuch", "request_id": 3}, OPERATOR)
    assert (unknown["ok"], unknown["op"], unknown["request_id"]) == (False, "nonesuch", 3)
    assert api.handle({"op": "x" * 33}, OPERATOR)["op"] is None


def test_operator_connections_answer_line_by_line(make_app: AppFactory) -> None:
    api = broker.OperatorApi(broker.Gateway(make_app()))
    lines = b'{"op":"status"}\nnot json\n\n{"request_id":"a","op":"kill_switch"}\n'
    output = io.BytesIO()

    broker.serve_operator_connection(api, io.BytesIO(lines), output, OPERATOR)

    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [reply["ok"] for reply in replies] == [True, False, True]
    assert [reply["op"] for reply in replies] == ["status", None, "kill_switch"]
    assert replies[1]["error"]["code"] == "invalid"
    assert replies[2]["request_id"] == "a"


def test_an_oversized_operator_line_ends_the_connection(make_app: AppFactory) -> None:
    api = broker.OperatorApi(broker.Gateway(make_app()))
    lines = b"x" * (broker.MAX_LINE_BYTES + 1) + b'\n{"op":"kill_switch","enable":true}\n'
    output = io.BytesIO()

    broker.serve_operator_connection(api, io.BytesIO(lines), output, OPERATOR)

    [reply] = [json.loads(line) for line in output.getvalue().splitlines()]
    assert reply["error"]["code"] == "limit"
    assert not api.gateway.app.policy.kill_switch.exists()


def test_a_closed_gateway_answers_unavailable(make_app: AppFactory) -> None:
    api = broker.OperatorApi(broker.Gateway(make_app()))
    assert api.gateway.drain(1.0)
    assert _error(api, {"op": "status"})["code"] == "unavailable"


# ---- settings and separation ------------------------------------------------------------------------


def _broker_table(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    table: dict[str, Any] = {
        "client_socket": str(tmp_path / "client.sock"),
        "operator_socket": str(tmp_path / "operator.sock"),
        "client_uids": [1001],
        "operator_uids": [1000],
    }
    table.update(overrides)
    return {key: value for key, value in table.items() if value is not None}


def test_broker_settings_are_complete_absolute_and_distinct(tmp_path: Path) -> None:
    settings = broker.broker_settings(config_from_dict({"broker": _broker_table(tmp_path)}, tmp_path))
    assert settings.client_socket == tmp_path / "client.sock"
    assert settings.client_uids == frozenset({1001})
    assert settings.operator_uids == frozenset({1000})
    for overrides in (
        {"operator_uids": None},
        {"client_socket": "client.sock"},
        {"operator_socket": str(tmp_path / "client.sock")},
        {"client_socket": "/" + "x" * 120},
    ):
        config = config_from_dict({"broker": _broker_table(tmp_path, **overrides)}, tmp_path)
        with pytest.raises(ConfigError):
            broker.broker_settings(config)
    with pytest.raises(ConfigError):
        broker.broker_settings(config_from_dict({}, tmp_path))


def test_shared_accounts_and_a_client_service_account_give_no_separation(tmp_path: Path) -> None:
    separate = broker.broker_settings(config_from_dict({"broker": _broker_table(tmp_path)}, tmp_path))
    assert broker.separation_problems(separate, service_uid=999) == []
    shared = broker.broker_settings(
        config_from_dict({"broker": _broker_table(tmp_path, client_uids=[1000, 1001])}, tmp_path)
    )
    [problem] = broker.separation_problems(shared, service_uid=999)
    assert "1000" in problem and "approve its own requests" in problem
    assert len(broker.separation_problems(separate, service_uid=1001)) == 1


# ---- socket files -----------------------------------------------------------------------------------


def _socket_file(path: Path) -> None:
    """Leave a socket file behind, as a crashed broker would."""
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        leftover.bind(os.fspath(path))
    finally:
        leftover.close()


def _private(tmp_path: Path) -> Path:
    """A socket directory with explicit permissions, independent of the test runner's umask."""
    directory = tmp_path / "run"
    directory.mkdir()
    directory.chmod(0o700)
    return directory


def test_the_socket_directory_must_exist_be_real_and_not_writable_by_others(tmp_path: Path) -> None:
    uid = os.getuid()
    private = _private(tmp_path)
    with pytest.raises(broker.BrokerError, match="does not exist"):
        broker.check_socket_directory(tmp_path / "missing", service_uid=uid)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(broker.BrokerError, match="writable"):
        broker.check_socket_directory(shared, service_uid=uid)
    link = tmp_path / "link"
    link.symlink_to(private)
    with pytest.raises(broker.BrokerError, match="real directory"):
        broker.check_socket_directory(link, service_uid=uid)
    if uid != 0:
        with pytest.raises(broker.BrokerError, match="belong"):
            broker.check_socket_directory(private, service_uid=uid + 1)
    broker.check_socket_directory(private, service_uid=uid)


def test_files_links_and_foreign_sockets_are_never_replaced(tmp_path: Path) -> None:
    uid = os.getuid()
    private = _private(tmp_path)
    regular = private / "regular.sock"
    regular.write_text("keep me", encoding="utf-8")
    with pytest.raises(broker.BrokerError, match="not a socket"):
        broker.clear_stale_socket(regular, service_uid=uid, in_use=lambda _path: False)
    assert regular.read_text(encoding="utf-8") == "keep me"
    link = private / "link.sock"
    link.symlink_to(regular)
    with pytest.raises(broker.BrokerError, match="symbolic link"):
        broker.clear_stale_socket(link, service_uid=uid, in_use=lambda _path: False)
    stale = private / "stale.sock"
    _socket_file(stale)
    with pytest.raises(broker.BrokerError, match="another account"):
        broker.clear_stale_socket(stale, service_uid=uid + 1, in_use=lambda _path: False)
    assert stale.exists()


def test_only_a_stale_socket_owned_by_the_service_account_is_removed(tmp_path: Path) -> None:
    path = _private(tmp_path) / "stale.sock"
    _socket_file(path)
    with pytest.raises(broker.BrokerError, match="accepting connections"):
        broker.clear_stale_socket(path, service_uid=os.getuid(), in_use=lambda _path: True)
    assert path.exists()
    broker.clear_stale_socket(path, service_uid=os.getuid(), in_use=lambda _path: False)
    assert not path.exists()
    broker.clear_stale_socket(path, service_uid=os.getuid(), in_use=lambda _path: True)


def test_bound_sockets_are_mode_660_and_removed_only_while_unchanged(tmp_path: Path) -> None:
    path = _private(tmp_path) / "c.sock"
    listener, identity = broker.bind_socket(
        path, service_uid=os.getuid(), group=None, in_use=lambda _path: False
    )
    try:
        assert path.stat().st_mode & 0o777 == 0o660
        assert listener.getblocking() is False
    finally:
        listener.close()
    broker.remove_socket(path, (identity[0], identity[1] + 1))
    assert path.exists()
    broker.remove_socket(path, identity)
    assert not path.exists()


def test_binding_refuses_an_unsafe_directory_without_creating_anything(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(broker.BrokerError):
        broker.bind_socket(shared / "c.sock", service_uid=os.getuid(), group=None, in_use=lambda _path: False)
    assert list(shared.iterdir()) == []


# ---- the shim ---------------------------------------------------------------------------------------


class EchoChannel:
    """A broker stand-in that answers each request line and closes after the client half-closes."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.ended = threading.Event()
        self.closed = False
        self._replies: queue.Queue[bytes] = queue.Queue()

    def send(self, data: bytes) -> None:
        self.sent.append(data)
        self._replies.put(json.dumps({"echo": data.decode("utf-8").strip()}).encode("utf-8") + b"\n")

    def end_input(self) -> None:
        self.ended.set()
        self._replies.put(b"")

    def readline(self, size: int, /) -> bytes:
        return self._replies.get(timeout=10)

    def close(self) -> None:
        self.closed = True


class BlockingInput:
    """Standard input that never delivers a line until released."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def readline(self, size: int, /) -> bytes:
        self.release.wait(timeout=10)
        return b""


def test_the_relay_copies_lines_both_ways_and_ends_cleanly() -> None:
    channel = EchoChannel()
    output = io.BytesIO()

    problem = broker.relay(io.BytesIO(b'{"a":1}\n{"b":2}'), output, channel)

    assert problem is None
    assert channel.sent == [b'{"a":1}\n', b'{"b":2}\n']
    assert channel.ended.is_set()
    assert [json.loads(line)["echo"] for line in output.getvalue().splitlines()] == ['{"a":1}', '{"b":2}']


def test_the_relay_reports_a_broker_that_closes_first() -> None:
    class ClosedChannel(EchoChannel):
        def readline(self, size: int, /) -> bytes:
            return b""

    stdin = BlockingInput()
    try:
        assert broker.relay(stdin, io.BytesIO(), ClosedChannel()) == "the broker closed the connection"
    finally:
        stdin.release.set()


def test_the_relay_stops_on_an_oversized_request_line() -> None:
    channel = EchoChannel()
    problem = broker.relay(io.BytesIO(b"x" * 9 + b"\n"), io.BytesIO(), channel, limit=8)
    assert problem is not None and "exceeded" in problem
    assert channel.sent == []
    assert channel.ended.is_set()


def test_run_shim_needs_no_configuration_and_reports_an_unreachable_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the shim must not load configuration")

    monkeypatch.setattr("switchboard.config.load_config", forbidden)
    monkeypatch.setenv("SWITCHBOARD_CONFIG", str(tmp_path / "absent.toml"))
    channel = EchoChannel()
    output = io.BytesIO()
    shim = broker.run_shim(
        tmp_path / "c.sock", stdin=io.BytesIO(b"{}\n"), stdout=output, connect=lambda _p: channel
    )
    assert shim == 0
    assert channel.closed
    assert json.loads(output.getvalue())["echo"] == "{}"

    def unreachable(path: Path) -> broker.LineChannel:
        raise FileNotFoundError(2, "No such file or directory")

    assert (
        broker.run_shim(tmp_path / "c.sock", stdin=io.BytesIO(), stdout=io.BytesIO(), connect=unreachable)
        == 1
    )
    assert "cannot reach the broker" in capsys.readouterr().err

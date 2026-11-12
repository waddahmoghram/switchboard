"""Audit-log write failures injected at ``AuditLog.append`` and by an unwritable log; authored and unrun.

The dispatcher writes the request record before it runs a handler, so a failure there must stop the call
before any effect. Every injected failure must still produce a well-formed protocol reply without the
synthetic detail, no call may be retried automatically, and the chain must verify once writes succeed.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Callable
from typing import Any

import pytest

from switchboard.app import App
from switchboard.mcp_server import McpServer
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool

pytestmark = pytest.mark.faults

AppFactory = Callable[..., App]
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
SYNTHETIC = "synthetic fault: no space left on device"
IS_ROOT = os.geteuid() == 0


class Probe:
    """A read-tier handler that only counts how often it actually ran."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"probed": True}


class AppendFaults:
    """Delegates to the real ``append`` except for the scheduled failing write."""

    def __init__(self, append: Callable[..., dict[str, Any]], fail_at: int) -> None:
        self._append = append
        self.fail_at = fail_at
        self.calls = 0
        self.raised = 0

    def __call__(self, kind: str, **fields: Any) -> dict[str, Any]:
        index = self.calls
        self.calls += 1
        if index == self.fail_at:
            self.raised += 1
            raise OSError(errno.ENOSPC, SYNTHETIC)
        return self._append(kind, **fields)


def add_probe(app: App) -> Probe:
    probe = Probe()
    app.registry.add(
        Tool(
            "fault_probe",
            "Synthetic probe for audit fault injection.",
            Tier.READ,
            {"type": "object", "properties": {}, "additionalProperties": False},
            probe,
        )
    )
    return probe


def probe_call(request_id: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "fault_probe", "arguments": {}, "_meta": MODERN},
    }


def failed_reply(reply: dict[str, Any] | None, request_id: int) -> bool:
    assert reply is not None
    assert reply["id"] == request_id
    assert "synthetic fault" not in json.dumps(reply, default=str)
    return "error" in reply or reply["result"]["isError"] is True


def call_status(app: App) -> str:
    """The outcome status, or "raised" when the failure propagated as an operating-system error."""
    try:
        return app.call("fault_probe", {}, Origin.ASSISTANT).status
    except OSError:
        return "raised"


def test_audit_failure_before_execution_fails_closed(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    probe = add_probe(app)
    faults = AppendFaults(app.ctx.audit.append, 0)
    monkeypatch.setattr(app.ctx.audit, "append", faults)

    reply = McpServer(tools=app.registry.all, call=app.dispatcher.call).handle(probe_call(1))

    assert faults.raised == 1
    assert probe.calls == 0
    assert failed_reply(reply, 1)


def test_audit_failure_at_each_write_of_a_call_is_answered_and_never_retried(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    probe = add_probe(app)
    server = McpServer(tools=app.registry.all, call=app.dispatcher.call)
    original = app.ctx.audit.append
    completed = False
    for fail_at in range(16):
        before = probe.calls
        faults = AppendFaults(original, fail_at)
        with monkeypatch.context() as patch:
            patch.setattr(app.ctx.audit, "append", faults)
            reply = server.handle(probe_call(fail_at))
        assert probe.calls - before <= 1
        if faults.raised == 0:
            assert not failed_reply(reply, fail_at)
            assert probe.calls - before == 1
            completed = True
            break
        if fail_at == 0:
            assert failed_reply(reply, fail_at)
            assert probe.calls == before
        else:
            # A later write fails after the handler ran; the reply must still be well-formed and sanitized.
            failed_reply(reply, fail_at)
    assert completed, "the call still reached the audit fault after 16 writes"
    assert app.ctx.audit.verify().ok


@pytest.mark.skipif(IS_ROOT, reason="permission faults are not observable as root")
def test_unwritable_audit_log_blocks_execution_until_restored(make_app: AppFactory) -> None:
    app = make_app()
    probe = add_probe(app)
    assert call_status(app) == "ok"
    path = app.ctx.audit.path
    path.chmod(0o400)
    try:
        assert call_status(app) in {"raised", "error"}
    finally:
        path.chmod(0o600)

    assert probe.calls == 1
    assert call_status(app) == "ok"
    assert probe.calls == 2
    assert app.ctx.audit.verify().ok

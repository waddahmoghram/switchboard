"""Authored UNRUN application flows; transport, completion reports, and capacity are synthetic."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors.monitoring import checks, doctor
from switchboard.connectors.monitoring.checks import Observation
from switchboard.connectors.monitoring.service import MonitoringService
from switchboard.context import AppContext
from switchboard.errors import ToolError
from switchboard.httpclient import HttpResponse
from switchboard.policy import Origin, Tier
from tests.conftest import AppFactory
from tests.fakes import FakeHttp, RecordedRequest
from tests.fixtures.monitoring import (
    ENDPOINT,
    NOW,
    PRIVATE_MARKER,
    Usage,
    backup,
    config,
    endpoint,
    report,
)

pytestmark = pytest.mark.integration


def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("no host measurement or secret resolution belongs in this operation")


@pytest.fixture(autouse=True)
def no_real_measurements(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "read_report", _forbidden)
    monkeypatch.setattr(checks, "disk_usage", _forbidden)


def _service(app: App) -> MonitoringService:
    result: MonitoringService = app.ctx.require("monitoring")
    result.clock = lambda: NOW
    return result


def _call(app: App, tool: str, args: dict[str, Any] | None = None) -> Any:
    outcome = app.call(tool, args or {}, Origin.ASSISTANT)
    assert outcome.status == "ok", outcome.message
    return outcome.data


def test_registration_migration_doctor_and_cached_status_are_offline(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AppContext, "secret", _forbidden)
    raw = config()
    raw["connectors"]["monitoring"]["checks"]["server"]["bearer_token"] = "env:SB_MISSING_SYNTHETIC_TOKEN"
    app = make_app(raw)
    service = _service(app)
    assert {tool.name: tool.tier for tool in app.registry.all() if tool.connector == "monitoring"} == {
        "monitoring_check": Tier.READ,
        "monitoring_status": Tier.READ,
    }
    [job] = [job for job in app.ctx.scheduler.jobs() if job.name.startswith("monitoring.")]
    assert job.name == "monitoring.poll"
    assert job.interval == 300
    assert job.func == service.poll_job
    assert doctor(app.ctx) == []
    assert fake_http.requests == []
    assert app.ctx.events.recent() == []
    cached = _call(app, "monitoring_status")
    assert len(cached["checks"]) == 3
    assert all(item["status"] == "unchecked" and item["checked_at"] is None for item in cached["checks"])
    assert all(item["error"] == "not_checked" and item["revision"] == 0 for item in cached["checks"])
    with app.ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitoring_snapshots").fetchone()[0] == 0
        assert (
            conn.execute("SELECT version FROM schema_migrations WHERE component = 'monitoring'").fetchone()[0]
            == 1
        )
    assert app.ctx.db.migrate() == []
    assert fake_http.requests == []


def test_poll_healthy_failed_stale_recovery_and_bounded_events(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(config())
    service = _service(app)
    state: dict[str, Any] = {"http_status": 200, "body": report(), "usage": Usage(1000, 500, 500)}

    def respond(request: RecordedRequest) -> HttpResponse:
        return HttpResponse(
            state["http_status"],
            request.url,
            body=b'{"health":{"ready":true},"private":"SYNTHETIC_PRIVATE_RESPONSE_DO_NOT_STORE"}',
        )

    fake_http.add_handler("GET", ENDPOINT, respond)
    monkeypatch.setattr(checks, "read_report", lambda _check: state["body"])
    monkeypatch.setattr(checks, "disk_usage", lambda _path: state["usage"])
    assert service.poll() == {"checked": 3, "changed": 3, "events_queued": 3, "record_errors": 0}
    assert {item["status"] for item in service.status({})["checks"]} == {"healthy"}
    state.update(http_status=503, body=report(status="failed"), usage=Usage(1000, 900, 100))
    assert service.poll() == {"checked": 3, "changed": 3, "events_queued": 6, "record_errors": 0}
    assert service.poll()["events_queued"] == 0
    assert len(app.ctx.events.recent(100, event_type="monitoring.alert")) == 3
    state.update(http_status=200, usage=Usage(1000, 500, 500))
    service.clock = lambda: NOW + timedelta(seconds=1)
    assert service.poll()["events_queued"] == 4
    current = {item["name"]: item for item in service.status({})["checks"]}
    assert current["backup"]["status"] == "stale"
    assert current["backup"]["error"] == "backup_report_stale"
    assert current["backup"]["report"]["status"] == "failed"
    assert current["server"]["status"] == current["disk"]["status"] == "healthy"
    state["body"] = report(started_at="2030-01-02T11:59:58Z", finished_at="2030-01-02T12:00:00Z")
    assert service.poll()["events_queued"] == 1
    assert {item["status"] for item in service.status({})["checks"]} == {"healthy"}
    events = app.ctx.events.recent(100)
    assert all(event.source == "monitoring" for event in events)
    assert all(event.type in {"monitoring.changed", "monitoring.alert"} for event in events)
    for event in events:
        assert len(json.dumps(event.payload)) < 4096
        assert PRIVATE_MARKER not in json.dumps(event.payload)
        assert not {"url", "path", "directory", "filename", "bearer_token"} & set(event.payload)
        assert set(event.payload) == {
            "name",
            "kind",
            "status",
            "error",
            "reachable",
            "ready",
            "measurements",
            "report",
            "checked_at",
            "changed_at",
            "revision",
            "previous_status",
        }
    assert all(request.method == "GET" and request.body is None for request in fake_http.requests)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("monitoring_check", {}),
        ("monitoring_check", {"name": "unconfigured"}),
        ("monitoring_check", {"name": "server", "url": "https://other.example.test/"}),
        ("monitoring_check", {"name": "disk", "path": "/other"}),
        ("monitoring_check", {"name": "backup", "command": "ignored"}),
        ("monitoring_check", {"name": ["server"]}),
        ("monitoring_status", {"name": "unconfigured"}),
        ("monitoring_status", {"name": "x" * 65}),
        ("monitoring_status", {"name": None}),
        ("monitoring_status", {"refresh": True}),
    ],
)
def test_tools_cannot_select_arbitrary_addresses_paths_or_commands(
    make_app: AppFactory, fake_http: FakeHttp, tool: str, args: dict[str, Any]
) -> None:
    app = make_app(config())
    assert app.call(tool, args, Origin.ASSISTANT).status == "invalid"
    assert fake_http.requests == []
    assert app.ctx.events.recent() == []


def test_read_policy_denial_prevents_measurements_and_read_tier_survives_kill_switch(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    raw = config({"server": endpoint()})
    raw["policy"] = {"tools": {"monitoring_check": {"decision": "deny"}}}
    denied = make_app(raw)
    assert denied.call("monitoring_check", {"name": "server"}, Origin.ASSISTANT).status == "denied"
    assert fake_http.requests == []
    assert _call(denied, "monitoring_status")["checks"][0]["status"] == "unchecked"
    allowed = make_app(config({"server": endpoint()}))
    allowed.ctx.data_dir.joinpath("KILL_SWITCH").write_text("synthetic\n", encoding="ascii")
    fake_http.add("GET", ENDPOINT, body='{"health":{"ready":true}}')
    assert _call(allowed, "monitoring_check", {"name": "server"})["check"]["status"] == "healthy"
    assert not allowed.approvals.list("pending")


@pytest.mark.parametrize("event_type", ["monitoring.changed", "monitoring.alert"])
def test_event_failure_rolls_back_first_snapshot_and_all_events_then_retry_is_safe(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    app = make_app(config({"server": endpoint()}))
    service = _service(app)
    original = app.ctx.events.emit
    connections: list[sqlite3.Connection] = []

    def fail(source: str, emitted_type: str, payload: dict[str, Any], **kwargs: Any) -> int | None:
        conn = kwargs["connection"]
        assert isinstance(conn, sqlite3.Connection)
        assert conn.in_transaction
        connections.append(conn)
        result = original(source, emitted_type, payload, **kwargs)
        if emitted_type == event_type:
            raise RuntimeError("synthetic event failure")
        return result

    monkeypatch.setattr(app.ctx.events, "emit", fail)
    fake_http.add("GET", ENDPOINT, status=503, repeat=True)
    with pytest.raises(RuntimeError, match="synthetic event failure"):
        service.check({"name": "server"})
    assert connections
    assert all(conn is connections[0] for conn in connections)
    with app.ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitoring_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    monkeypatch.setattr(app.ctx.events, "emit", original)
    saved = service.check({"name": "server"})
    assert saved["check"]["revision"] == 1
    assert saved["events_queued"] == 2
    assert service.check({"name": "server"})["events_queued"] == 0


def test_event_failure_does_not_replace_a_previous_healthy_snapshot(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(config({"server": endpoint()}))
    service = _service(app)
    fake_http.add("GET", ENDPOINT, body='{"health":{"ready":true}}')
    old = service.check({"name": "server"})["check"]
    original = app.ctx.events.emit

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("synthetic event failure")

    monkeypatch.setattr(app.ctx.events, "emit", fail)
    fake_http.add("GET", ENDPOINT, status=503)
    service.clock = lambda: NOW + timedelta(seconds=1)
    with pytest.raises(RuntimeError):
        service.check({"name": "server"})
    cached = service.status({})["checks"][0]
    assert cached["status"] == "healthy"
    assert cached["checked_at"] == old["checked_at"]
    assert cached["revision"] == 1
    assert len(app.ctx.events.recent()) == 1
    monkeypatch.setattr(app.ctx.events, "emit", original)
    fake_http.add("GET", ENDPOINT, status=503)
    assert service.check({"name": "server"})["check"]["revision"] == 2


def test_transition_deduplication_survives_restart_and_a_new_episode_alerts_again(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    raw = config({"server": endpoint()})
    app = make_app(raw)
    service = _service(app)
    for http_status, body, revision, queued in [
        (200, '{"health":{"ready":true}}', 1, 1),
        (503, PRIVATE_MARKER, 2, 2),
        (503, PRIVATE_MARKER + "changed response size", 2, 0),
        (200, '{"health":{"ready":true}}', 3, 1),
        (503, PRIVATE_MARKER, 4, 2),
    ]:
        fake_http.add("GET", ENDPOINT, status=http_status, body=body)
        result = service.check({"name": "server"})
        assert result["check"]["revision"] == revision
        assert result["events_queued"] == queued
    assert len(app.ctx.events.recent(100, event_type="monitoring.alert")) == 2
    requests = len(fake_http.requests)
    restarted = make_app(raw)
    again = _service(restarted)
    assert len(fake_http.requests) == requests
    assert again.status({})["checks"][0]["revision"] == 4
    fake_http.add("GET", ENDPOINT, status=503)
    assert again.check({"name": "server"})["events_queued"] == 0
    with app.ctx.db.session() as conn:
        keys = [row[0] for row in conn.execute("SELECT dedupe_key FROM events")]
    assert len(keys) == len(set(keys)) == 6


def test_configuration_changes_invalidate_cache_but_do_not_reuse_transition_keys(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(config({"server": endpoint()}))
    service = _service(app)
    fake_http.add("GET", ENDPOINT, body='{"health":{"ready":true}}')
    service.check({"name": "server"})
    address = "https://replacement.example.test/ready"
    replacement = make_app(config({"server": endpoint(url=address)}))
    new_service = _service(replacement)
    assert new_service.status({})["checks"][0]["status"] == "unchecked"
    fake_http.add("GET", address, body='{"health":{"ready":true}}')
    result = new_service.check({"name": "server"})
    assert result["changed"]
    assert result["check"]["revision"] == 2
    [event] = replacement.ctx.events.recent(1)
    assert event.payload["previous_status"] is None


def test_old_observations_cannot_overwrite_newer_snapshots(make_app: AppFactory) -> None:
    app = make_app(config({"server": endpoint()}))
    service = _service(app)
    [target] = service.settings.checks
    latest = service.store.record(
        target,
        Observation("healthy", reachable=True, ready=True),
        (NOW + timedelta(seconds=1)).isoformat(timespec="microseconds"),
    )
    ignored = service.store.record(
        target,
        Observation("error", "endpoint_unreachable", reachable=False),
        NOW.isoformat(timespec="microseconds"),
    )
    assert ignored.snapshot == latest.snapshot
    assert ignored.events_queued == 0
    assert not ignored.changed
    assert len(app.ctx.events.recent()) == 1


def test_cached_status_ages_snapshots_and_backup_evidence_without_reading_or_emitting(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(config({"backup": backup()}))
    service = _service(app)
    monkeypatch.setattr(checks, "read_report", lambda _check: report())
    saved = service.check({"name": "backup"})["check"]
    monkeypatch.setattr(checks, "read_report", _forbidden)
    service.clock = lambda: NOW + timedelta(seconds=1)
    aged_report = _call(app, "monitoring_status")["checks"][0]
    assert aged_report["status"] == "stale"
    assert aged_report["report_stale"]
    assert aged_report["error"] == "backup_report_stale"
    assert not aged_report["snapshot_stale"]
    service.clock = lambda: NOW + timedelta(seconds=600)
    assert not service.status({})["checks"][0]["snapshot_stale"]
    service.clock = lambda: NOW + timedelta(seconds=601)
    expired = service.status({})["checks"][0]
    assert expired["snapshot_stale"]
    assert expired["report_stale"]
    assert expired["status"] == "stale"
    assert expired["error"] == "snapshot_stale"
    assert expired["observed_status"] == "healthy"
    assert expired["observed_error"] is None
    assert expired["checked_at"] == saved["checked_at"]
    assert len(app.ctx.events.recent()) == 1
    service.clock = lambda: NOW - timedelta(seconds=1)
    assert service.status({})["checks"][0]["snapshot_stale"]


def test_one_recording_failure_does_not_prevent_other_named_checks(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(config())
    service = _service(app)
    original = service.store.record

    def record(target: Any, observation: Observation, checked_at: str) -> Any:
        if target.name == "backup":
            raise sqlite3.OperationalError(PRIVATE_MARKER)
        return original(target, observation, checked_at)

    monkeypatch.setattr(service.store, "record", record)
    monkeypatch.setattr(checks, "read_report", lambda _check: report())
    monkeypatch.setattr(checks, "disk_usage", lambda _path: Usage(1000, 500, 500))
    fake_http.add("GET", ENDPOINT, body='{"health":{"ready":true}}', repeat=True)
    result = service.poll()
    assert result == {"checked": 2, "changed": 2, "events_queued": 2, "record_errors": 1}
    assert service.status({"name": "server"})["checks"][0]["status"] == "healthy"
    assert service.status({"name": "backup"})["checks"][0]["status"] == "unchecked"
    with pytest.raises(ToolError, match="other checks were attempted") as caught:
        service.poll_job()
    assert PRIVATE_MARKER not in str(caught.value)


def test_service_callers_cannot_bypass_the_name_only_contract(make_app: AppFactory) -> None:
    service = _service(make_app(config()))
    with pytest.raises(ToolError):
        service.check({"name": "server", "url": "https://other.example.test/"})
    with pytest.raises(ToolError):
        service.check({})
    with pytest.raises(ToolError):
        service.status({"name": "not_configured"})


def test_rules_notify_only_the_configured_self_channel_after_event_processing(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = config({"server": endpoint()})
    raw["switchboard"]["connectors"].append("notify")
    raw["connectors"]["notify"] = {
        "channels": {
            "self": {
                "type": "ntfy",
                "server": "https://notify.example.test",
                "topic": "env:SB_TEST_SELF_TOPIC",
            },
        }
    }
    raw["rules"] = [
        {
            "name": "Synthetic monitoring alert",
            "on": "monitoring.alert",
            "match": {"payload.name": "server"},
            "do": [
                {
                    "tool": "notify_self",
                    "args": {
                        "title": "Check {payload.name}",
                        "message": "{payload.status}: {payload.error}",
                        "channels": ["self"],
                    },
                }
            ],
        }
    ]
    monkeypatch.setenv("SB_TEST_SELF_TOPIC", "synthetic-self-topic")
    app = make_app(raw)
    service = _service(app)
    fake_http.add("GET", ENDPOINT, status=503, body=PRIVATE_MARKER, repeat=True)
    fake_http.add("POST", "https://notify.example.test/synthetic-self-topic", body="accepted")
    assert service.check({"name": "server"})["events_queued"] == 2
    assert all(request.method == "GET" for request in fake_http.requests)
    assert app.process_events() == 2
    notifications = [request for request in fake_http.requests if request.method == "POST"]
    assert len(notifications) == 1
    assert notifications[0].body == b"unhealthy: unexpected_http_status"
    assert PRIVATE_MARKER not in (notifications[0].body or b"").decode()
    assert service.check({"name": "server"})["events_queued"] == 0
    assert app.process_events() == 0
    assert len([request for request in fake_http.requests if request.method == "POST"]) == 1

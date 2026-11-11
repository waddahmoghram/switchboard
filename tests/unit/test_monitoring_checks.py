"""Authored UNRUN measurement tests using only fake transport and substituted operating-system interfaces."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors.monitoring import checks
from switchboard.connectors.monitoring.files import MAX_REPORT_BYTES, CheckError
from switchboard.connectors.monitoring.service import MonitoringService
from switchboard.errors import SecretError, ToolError
from switchboard.httpclient import HttpResponse
from tests.conftest import AppFactory
from tests.fakes import FakeHttp, RecordedRequest
from tests.fixtures.monitoring import (
    ENDPOINT,
    NOW,
    PRIVATE_MARKER,
    TOKEN,
    Usage,
    config,
    endpoint,
    report,
)


def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a synthetic measurement stub is required")


@pytest.fixture(autouse=True)
def no_real_measurements(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "read_report", _forbidden)
    monkeypatch.setattr(checks, "disk_usage", _forbidden)


@pytest.fixture
def app(make_app: AppFactory) -> App:
    result = make_app(config())
    service: MonitoringService = result.ctx.require("monitoring")
    service.clock = lambda: NOW
    return result


def _check(app: App, name: str = "server") -> dict[str, Any]:
    service: MonitoringService = app.ctx.require("monitoring")
    return service.check({"name": name})["check"]


def test_endpoint_readiness_uses_only_the_configured_criterion(app: App, fake_http: FakeHttp) -> None:
    fake_http.add("GET", ENDPOINT, body='{"health":{"ready":true},"private":"unused secret"}')
    result = _check(app)
    assert result["status"] == "healthy"
    assert result["reachable"] is result["ready"] is True
    assert result["error"] is None
    assert result["checked_at"] == NOW.isoformat(timespec="microseconds")
    assert result["measurements"]["http_status"] == 200
    assert "unused secret" not in json.dumps(result)
    [request] = fake_http.requests
    assert request.method == "GET" and request.url == ENDPOINT and request.body is None
    assert request.follow_redirects is request.public_only is False
    assert request.timeout == 5 and request.max_bytes == checks.MAX_RESPONSE_BYTES


@pytest.mark.parametrize(
    ("status", "body", "error", "readiness"),
    [
        (503, PRIVATE_MARKER, "unexpected_http_status", False),
        (401, PRIVATE_MARKER, "unexpected_http_status", False),
        (302, PRIVATE_MARKER, "redirect_refused", False),
        (200, PRIVATE_MARKER, "response_malformed", False),
        (200, "{}", "readiness_field_missing", False),
        (200, '{"health":{"ready":false}}', "readiness_mismatch", False),
        (200, '{"health":{"ready":1}}', "readiness_mismatch", False),
        (200, '{"health":{"ready":"true"}}', "readiness_mismatch", False),
        (200, '{"health":{"ready":true,"ready":false}}', "response_malformed", False),
        (200, '{"health":{"ready":true},"number":NaN}', "response_malformed", False),
        (200, '{"health":{"ready":true},"number":1e999}', "response_malformed", False),
        (200, "[]", "response_malformed", False),
    ],
)
def test_http_response_is_not_automatically_readiness(
    app: App, fake_http: FakeHttp, status: int, body: str, error: str, readiness: bool
) -> None:
    fake_http.add(
        "GET", ENDPOINT, status=status, body=body, headers={"location": "https://other.example.test/"}
    )
    result = _check(app)
    assert result["status"] in {"unhealthy", "error"}
    assert result["error"] == error
    assert result["reachable"] is True and result["ready"] is readiness
    assert len(fake_http.requests) == 1
    assert PRIVATE_MARKER not in json.dumps(result)
    assert all(PRIVATE_MARKER not in json.dumps(event.payload) for event in app.ctx.events.recent())


def test_http_200_without_a_criterion_is_reachability_only(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(config({"server": {"kind": "endpoint", "url": ENDPOINT}}))
    fake_http.add("GET", ENDPOINT, body=PRIVATE_MARKER)
    result = _check(app)
    assert result["status"] == "reachable"
    assert result["reachable"] is True and result["ready"] is None
    assert result["report"] == {}
    assert PRIVATE_MARKER not in json.dumps(result)


def test_final_address_changes_are_refused_even_with_a_successful_body(app: App, fake_http: FakeHttp) -> None:
    def respond(_request: RecordedRequest) -> HttpResponse:
        return HttpResponse(200, "https://other.example.test/", body=b'{"health":{"ready":true}}')

    fake_http.add_handler("GET", ENDPOINT, respond)
    assert _check(app)["error"] == "redirect_refused"
    assert len(fake_http.requests) == 1


def test_network_failures_are_distinct_from_reached_but_unready(app: App, fake_http: FakeHttp) -> None:
    fake_http.add_error("GET", ENDPOINT, PRIVATE_MARKER)
    result = _check(app)
    assert result["status"] == "error" and result["error"] == "endpoint_unreachable"
    assert result["reachable"] is False and result["ready"] is None
    assert PRIVATE_MARKER not in json.dumps(result)


def test_response_size_is_bounded_even_when_the_transport_fake_does_not_enforce_it(
    app: App, fake_http: FakeHttp
) -> None:
    fake_http.add("GET", ENDPOINT, body=b"x" * (checks.MAX_RESPONSE_BYTES + 1))
    assert _check(app)["error"] == "response_too_large"


def test_secrets_are_resolved_only_at_request_time_and_never_appear_in_results(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(config({"server": endpoint(bearer_token="env:SB_SYNTHETIC_MONITORING")}))
    service: MonitoringService = app.ctx.require("monitoring")
    service.clock = lambda: NOW
    calls: list[str] = []

    def secret(reference: str) -> str:
        calls.append(reference)
        return TOKEN

    monkeypatch.setattr(app.ctx, "secret", secret)
    assert calls == [] and fake_http.requests == []
    fake_http.add("GET", ENDPOINT, body=f'{{"health":{{"ready":true}},"secret":"{TOKEN}"}}')
    result = _check(app)
    assert calls == ["env:SB_SYNTHETIC_MONITORING"]
    assert fake_http.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(result)
    assert TOKEN not in json.dumps([event.payload for event in app.ctx.events.recent()])


@pytest.mark.parametrize("token", ["", "line\r\ninjection", "has space", "x" * 4097, "\u00e9"])
def test_invalid_header_secrets_never_reach_the_transport(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    app = make_app(config({"server": endpoint(bearer_token="env:SB_SYNTHETIC_MONITORING")}))
    monkeypatch.setattr(app.ctx, "secret", lambda _reference: token)
    result = _check(app)
    assert result["error"] == "secret_invalid" and result["reachable"] is None
    assert fake_http.requests == []


def test_missing_secret_has_no_reachability_claim_or_exception_text(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(config({"server": endpoint(bearer_token="env:SB_SYNTHETIC_MONITORING")}))

    def missing(_reference: str) -> str:
        raise SecretError(PRIVATE_MARKER)

    monkeypatch.setattr(app.ctx, "secret", missing)
    result = _check(app)
    assert result["error"] == "secret_unavailable"
    assert result["reachable"] is result["ready"] is None
    assert PRIVATE_MARKER not in json.dumps(result)
    assert fake_http.requests == []


def test_explicit_private_http_consent_is_passed_to_the_transport(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    address = "http://192.168.10.5:8000/ready"
    app = make_app(config({"server": endpoint(url=address, allow_http=True)}))
    fake_http.add("GET", address, body='{"health":{"ready":true}}')
    assert _check(app)["status"] == "healthy"
    assert fake_http.requests[0].url == address


@pytest.mark.parametrize(
    ("filename", "status", "error"),
    [("healthy.json", "healthy", None), ("failed.json", "unhealthy", "backup_failed")],
)
def test_owned_completion_report_schema_with_synthetic_fixtures(
    app: App, monkeypatch: pytest.MonkeyPatch, filename: str, status: str, error: str | None
) -> None:
    raw = (Path(__file__).parents[1] / "fixtures" / "monitoring" / filename).read_bytes()
    monkeypatch.setattr(checks, "read_report", lambda _check: raw)
    result = _check(app, "backup")
    assert result["status"] == status and result["error"] == error
    assert result["measurements"]["age_seconds"] == 3600
    assert result["measurements"]["duration_seconds"] == 3600
    assert result["report"]["finished_at"] == "2030-01-02T11:00:00.000000+00:00"
    assert result["reachable"] is result["ready"] is None


def test_backup_freshness_boundary_and_stale_failed_report_are_explicit(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checks, "read_report", lambda _check: report())
    assert _check(app, "backup")["status"] == "healthy"
    service: MonitoringService = app.ctx.require("monitoring")
    service.clock = lambda: NOW + timedelta(microseconds=1)
    stale = _check(app, "backup")
    assert stale["status"] == "stale" and stale["error"] == "backup_report_stale"
    monkeypatch.setattr(checks, "read_report", lambda _check: report(status="failed"))
    failed = _check(app, "backup")
    assert failed["status"] == "stale" and failed["report"]["status"] == "failed"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2030-01-02",
        "2030-01-02T11:00:00",
        "2030-01-02 11:00:00+00:00",
        "2030-02-30T11:00:00+00:00",
        "2030-01-02T11:00:00+00:60",
        "2030-01-02T11:00:00-00:00",
        "2030-01-02T11:00:00+24:00",
        "0001-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59-23:59",
        "2030-01-02T12:00:01+00:00",
    ],
)
def test_report_dates_must_be_valid_aware_nonfuture_and_bounded(
    app: App, monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    monkeypatch.setattr(checks, "read_report", lambda _check: report(finished_at=value))
    result = _check(app, "backup")
    assert result["status"] == "error" and result["error"] == "report_time_invalid"


def test_report_started_after_finished_is_invalid(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "read_report", lambda _check: report(started_at="2030-01-02T11:00:01Z"))
    assert _check(app, "backup")["error"] == "report_time_invalid"


def test_report_offsets_and_fractional_boundaries_preserve_the_instant(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        checks,
        "read_report",
        lambda _check: report(
            started_at="2030-01-02T12:00:00+02:00", finished_at="2030-01-02T13:00:00.5+02:00"
        ),
    )
    result = _check(app, "backup")
    assert result["status"] == "healthy"
    assert result["measurements"]["age_seconds"] == 3599.5
    assert result["report"]["finished_at"] == "2030-01-02T11:00:00.500000+00:00"


def test_report_published_during_the_read_uses_the_post_read_clock(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: MonitoringService = app.ctx.require("monitoring")
    current = [NOW]
    service.clock = lambda: current[0]

    def completed_during_read(_check: Any) -> bytes:
        current[0] = NOW + timedelta(seconds=1)
        return report(finished_at=current[0].isoformat())

    monkeypatch.setattr(checks, "read_report", completed_during_read)
    result = _check(app, "backup")
    assert result["status"] == "healthy" and result["measurements"]["age_seconds"] == 0
    assert result["checked_at"] == current[0].isoformat(timespec="microseconds")


def test_naive_observation_clocks_are_refused_before_any_measurement(app: App, fake_http: FakeHttp) -> None:
    service: MonitoringService = app.ctx.require("monitoring")
    service.clock = lambda: NOW.replace(tzinfo=None)
    with pytest.raises(ToolError, match="timezone aware"):
        service.check({"name": "server"})
    assert fake_http.requests == [] and app.ctx.events.recent() == []


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"schema_version": 1.0},
        {"check": "some_other_backup"},
        {"status": "running"},
        {"status": True},
        {"bytes_written": -1},
        {"bytes_written": True},
        {"bytes_written": 2**63},
        {"bytes_written": 1.5},
        {"private_output": PRIVATE_MARKER},
    ],
)
def test_completion_reports_reject_wrong_schema_and_never_echo_content(
    app: App, monkeypatch: pytest.MonkeyPatch, updates: dict[str, Any]
) -> None:
    monkeypatch.setattr(checks, "read_report", lambda _check: report(**updates))
    result = _check(app, "backup")
    assert result["status"] == "error"
    assert result["error"] in {"report_schema_invalid", "report_check_mismatch"}
    assert PRIVATE_MARKER not in json.dumps(result)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"not-json",
        b"\xff",
        b'{"schema_version":1,"schema_version":2}',
        b'{"value":NaN}',
        b'{"value":1e999}',
        b"x" * (MAX_REPORT_BYTES + 1),
        b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
    ],
)
def test_missing_or_malformed_report_fields_do_not_certify_a_backup(
    app: App, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    monkeypatch.setattr(checks, "read_report", lambda _check: raw)
    result = _check(app, "backup")
    assert result["status"] == "error"
    assert result["error"] in {"report_malformed", "report_schema_invalid"}


@pytest.mark.parametrize(
    "error",
    ["report_missing", "report_not_private", "report_permission_denied", "report_unavailable"],
)
def test_unreadable_report_is_an_explicit_error(
    app: App, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    def unreadable(_check: Any) -> bytes:
        raise CheckError(error)

    monkeypatch.setattr(checks, "read_report", unreadable)
    assert _check(app, "backup")["error"] == error


@pytest.mark.parametrize(
    ("usage", "status"),
    [
        ((1000, 500, 500), "healthy"),
        ((1000, 800, 200), "healthy"),
        ((1000, 801, 199), "unhealthy"),
        ((1000, 400, 300), "healthy"),
    ],
)
def test_disk_measurements_have_units_and_threshold_equality_is_healthy(
    app: App, monkeypatch: pytest.MonkeyPatch, usage: tuple[int, int, int], status: str
) -> None:
    paths: list[Path] = []

    def capacity(path: Path) -> Usage:
        paths.append(path)
        return Usage(*usage)

    monkeypatch.setattr(checks, "disk_usage", capacity)
    result = _check(app, "disk")
    assert result["status"] == status
    assert paths == [Path("/synthetic-disk")]
    assert result["measurements"]["free_bytes"] == usage[2]
    assert result["measurements"]["free_percent"] == usage[2] / usage[0] * 100
    assert result["error"] == ("disk_below_threshold" if status == "unhealthy" else None)


@pytest.mark.parametrize(
    "usage",
    [(0, 0, 0), (100, 60, 60), (100, 50, -1), (100, 50, True), (100, 50, 1.5), (2**63, 0, 1)],
)
def test_unavailable_or_invalid_disk_data_is_never_zero_free_space(
    app: App, monkeypatch: pytest.MonkeyPatch, usage: tuple[Any, Any, Any]
) -> None:
    monkeypatch.setattr(checks, "disk_usage", lambda _path: usage)
    result = _check(app, "disk")
    assert result["status"] == "error" and result["error"] == "disk_data_invalid"
    assert result["measurements"] == {}


@pytest.mark.parametrize("error", [OSError(PRIVATE_MARKER), NotImplementedError(PRIVATE_MARKER)])
def test_disk_unavailability_is_explicit_and_sanitized(
    app: App, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def unavailable(_path: Path) -> Usage:
        raise error

    monkeypatch.setattr(checks, "disk_usage", unavailable)
    result = _check(app, "disk")
    assert result["error"] == "disk_unavailable" and result["measurements"] == {}
    assert PRIVATE_MARKER not in json.dumps(result)

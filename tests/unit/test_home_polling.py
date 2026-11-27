"""UNRUN durable sampling tests; all provider traffic uses synthetic responses."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from switchboard.connectors.home import poll
from switchboard.connectors.home.state import observation
from switchboard.policy import Origin
from tests.fakes import FakeHttp
from tests.unit.test_home_support import (
    HA,
    MOMENT,
    AppFactory,
    add_states,
    configuration,
    sample,
    samples,
)

pytest_plugins = ["tests.unit.test_home_support"]


def test_registration_is_offline_with_lazy_secrets_and_only_opt_in_jobs(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SB_HOME_EXTENSION_TOKEN")
    app = home_app(configuration(polling=True))
    assert fake_http.requests == []
    assert [job.name for job in app.ctx.scheduler.jobs() if job.name.startswith("home.")] == ["home.poll"]
    assert app.call("home_targets", {}, Origin.ASSISTANT).status == "ok"
    assert app.call("home_poll_status", {}, Origin.ASSISTANT).status == "ok"
    assert fake_http.requests == []


def test_disabled_polling_neither_schedules_nor_samples(home_app: AppFactory, fake_http: FakeHttp) -> None:
    app = home_app(configuration())
    assert not any(job.name == "home.poll" for job in app.ctx.scheduler.jobs())
    outcome = app.call("home_poll", {}, Origin.ASSISTANT)
    assert outcome.status == "error"
    assert "disabled" in outcome.message
    assert fake_http.requests == []


def test_home_migration_is_registered_once_even_when_polling_is_disabled(home_app: AppFactory) -> None:
    app = home_app(configuration())
    with app.ctx.db.session() as conn:
        versions = conn.execute("SELECT version FROM schema_migrations WHERE component = 'home'").fetchall()
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert [row["version"] for row in versions] == [1]
    assert {"home_snapshots", "home_poll_status"} <= names
    assert app.ctx.db.migrate() == []


def test_first_sample_is_a_silent_baseline_and_filters_unapproved_entities(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    outcome = app.call("home_poll", {}, Origin.ASSISTANT)
    assert outcome.status == "ok"
    assert outcome.data["baselined"] == 3
    assert outcome.data["changed"] == 0
    assert app.ctx.events.recent(source="home") == []
    status = app.call("home_poll_status", {}, Origin.ASSISTANT).data
    assert {item["entity_id"] for item in status["entities"]} == {
        "light.lounge",
        "sensor.temperature",
        "binary_sensor.leak",
    }
    assert status["stale"] is False
    assert status["history_complete"] is False
    assert "fixture-private-attribute" not in json.dumps(status)


def test_unchanged_samples_and_timestamp_only_updates_do_not_emit_events(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    assert app.call("home_poll", {}, Origin.ASSISTANT).status == "ok"
    changed = samples()
    changed[0]["last_changed"] = "2026-11-11T11:00:00+00:00"
    changed[0]["last_updated"] = "2026-11-11T11:00:00+00:00"
    add_states(fake_http, changed)
    outcome = app.call("home_poll", {}, Origin.ASSISTANT)
    assert outcome.data["changed"] == 0
    assert app.ctx.events.recent(source="home") == []


def test_state_and_allowed_attribute_changes_emit_bounded_durable_events(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    changed = [
        sample("light.lounge", state="on", brightness=102),
        sample("sensor.temperature"),
        sample("binary_sensor.leak"),
    ]
    add_states(fake_http, changed)
    outcome = app.call("home_poll", {}, Origin.ASSISTANT)
    assert outcome.data["changed"] == 1
    [event] = app.ctx.events.pending()
    assert event.source == "home"
    assert event.type == "home.state_changed"
    assert event.payload["entity_id"] == "light.lounge"
    assert event.payload["previous"]["state"] == "off"
    assert event.payload["current"]["state"] == "on"
    assert event.payload["current"]["attributes"]["brightness"] == 102
    assert event.payload["revision"] == 2
    assert event.payload["history_complete"] is False
    assert "access_token" not in json.dumps(event.payload)
    with app.ctx.db.session() as conn:
        row = conn.execute("SELECT dedupe_key FROM events WHERE id = ?", (event.id,)).fetchone()
    assert row["dedupe_key"].endswith(":light.lounge:2")


def test_unavailable_unknown_and_recovery_are_real_sampled_transitions(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    for state in ("off", "unavailable", "unknown", "on"):
        add_states(
            fake_http,
            [sample("light.lounge", state=state), sample("sensor.temperature"), sample("binary_sensor.leak")],
        )
        assert app.call("home_poll", {}, Origin.ASSISTANT).status == "ok"
    events = list(reversed(app.ctx.events.recent(source="home")))
    assert [event.payload["current"]["availability"] for event in events] == [
        "unavailable",
        "unknown",
        "available",
    ]
    assert [event.payload["revision"] for event in events] == [2, 3, 4]


def test_missing_entities_are_reported_without_claiming_they_are_off(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    add_states(fake_http, [sample("sensor.temperature"), sample("binary_sensor.leak")])
    assert app.call("home_poll", {}, Origin.ASSISTANT).data["changed"] == 1
    [event] = app.ctx.events.recent(source="home")
    assert event.payload["current"]["availability"] == "missing"
    assert event.payload["current"]["state"] is None


def test_a_missing_first_baseline_is_silent_and_later_arrival_is_observed(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http, [])
    assert app.call("home_poll", {}, Origin.ASSISTANT).data["baselined"] == 3
    assert app.ctx.events.recent(source="home") == []
    add_states(fake_http)
    assert app.call("home_poll", {}, Origin.ASSISTANT).data["changed"] == 3


def test_snapshot_event_and_health_writes_roll_back_together_on_enqueue_failure(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    before = app.call("home_poll_status", {}, Origin.ASSISTANT).data
    emit = app.ctx.events.emit
    calls: list[sqlite3.Connection] = []

    def fail_second(
        source: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int | None:
        assert connection is not None
        calls.append(connection)
        if len(calls) == 2:
            raise RuntimeError("synthetic event failure")
        return emit(source, event_type, payload, dedupe_key=dedupe_key, connection=connection)

    monkeypatch.setattr(app.ctx.events, "emit", fail_second)
    add_states(
        fake_http,
        [
            sample("light.lounge", state="on"),
            sample("sensor.temperature", state="22"),
            sample("binary_sensor.leak"),
        ],
    )
    with pytest.raises(RuntimeError, match="synthetic event failure"):
        app.ctx.require("home").poller.poll()
    assert calls[0] is calls[1]
    assert app.ctx.events.recent(source="home") == []
    assert app.call("home_poll_status", {}, Origin.ASSISTANT).data == before


def test_an_event_is_also_rolled_back_when_its_snapshot_write_fails(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    with app.ctx.db.session() as conn:
        conn.execute(
            "CREATE TRIGGER home_test_write_failure BEFORE UPDATE ON home_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'synthetic snapshot failure'); END"
        )
    add_states(
        fake_http,
        [
            sample("light.lounge", state="on"),
            sample("sensor.temperature"),
            sample("binary_sensor.leak"),
        ],
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic snapshot failure"):
        app.ctx.require("home").poller.poll()
    assert app.ctx.events.recent(source="home") == []
    snapshots = app.ctx.require("home").store.snapshots()
    assert next(item for item in snapshots if item["entity_id"] == "light.lounge")["state"] == "off"


def test_restart_reuses_baselines_and_does_not_duplicate_a_committed_change(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration(polling=True)
    app = home_app(config)
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    changed = [sample("light.lounge", state="on"), sample("sensor.temperature"), sample("binary_sensor.leak")]
    add_states(fake_http, changed)
    app.call("home_poll", {}, Origin.ASSISTANT)
    restarted = home_app(config)
    add_states(fake_http, changed)
    assert restarted.call("home_poll", {}, Origin.ASSISTANT).data["changed"] == 0
    assert len(restarted.ctx.events.recent(source="home")) == 1


def test_newly_allowlisted_entities_get_new_silent_baselines(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration(polling=True)
    app = home_app(config)
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    config["connectors"]["home"]["poll"]["entities"].append("sensor.humidity")
    restarted = home_app(config)
    add_states(fake_http)
    outcome = restarted.call("home_poll", {}, Origin.ASSISTANT)
    assert outcome.data["baselined"] == 1
    assert outcome.data["changed"] == 0


def test_removed_poll_targets_are_filtered_from_cached_reads(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration(polling=True)
    app = home_app(config)
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    config["connectors"]["home"]["poll"]["entities"] = ["sensor.temperature"]
    restarted = home_app(config)
    status = restarted.call("home_poll_status", {}, Origin.ASSISTANT).data
    assert [item["entity_id"] for item in status["entities"]] == ["sensor.temperature"]


def test_a_different_server_has_a_separate_baseline_namespace(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    config = configuration(polling=True)
    app = home_app(config)
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    config["connectors"]["home"]["base_url"] = "https://other.example.test"
    restarted = home_app(config)
    fake_http.add("GET", "https://other.example.test/api/states", body=json.dumps(samples()))
    assert restarted.call("home_poll", {}, Origin.ASSISTANT).data["baselined"] == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_provider_failures_preserve_snapshots_and_report_an_error_without_fake_changes(
    home_app: AppFactory, fake_http: FakeHttp, status: int
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    fake_http.add("GET", f"{HA}/api/states", status=status, body="synthetic-private-error-body")
    outcome = app.call("home_poll", {}, Origin.ASSISTANT)
    assert outcome.status == "error"
    health = app.call("home_poll_status", {}, Origin.ASSISTANT).data
    assert health["error"] == "poll failed; previous snapshots retained"
    assert health["succeeded_at"] is not None
    assert app.ctx.events.recent(source="home") == []
    assert "synthetic-private-error-body" not in json.dumps(health)


@pytest.mark.parametrize(
    "body",
    ["{", "{}", '[{"entity_id":"light.lounge","state":1,"attributes":{}}]', '[{"not_an_entity":true}]'],
)
def test_malformed_provider_data_is_an_error_not_an_empty_success(
    home_app: AppFactory, fake_http: FakeHttp, body: str
) -> None:
    app = home_app(configuration(polling=True))
    fake_http.add("GET", f"{HA}/api/states", body=body)
    assert app.call("home_poll", {}, Origin.ASSISTANT).status == "error"
    assert app.ctx.require("home").store.snapshots() == []


def test_duplicate_polled_entities_are_not_accepted_as_an_arbitrary_last_value(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http, [sample("light.lounge"), sample("light.lounge", state="on")])
    assert app.call("home_poll", {}, Origin.ASSISTANT).status == "error"


def test_polling_network_errors_do_not_persist_provider_exception_details(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    fake_http.add_error("GET", f"{HA}/api/states", "synthetic-home-token should not be retained")
    assert app.call("home_poll", {}, Origin.ASSISTANT).status == "error"
    health = app.call("home_poll_status", {}, Origin.ASSISTANT).data
    assert "synthetic-home-token" not in json.dumps(health)
    assert health["stale"] is True


def test_cached_staleness_uses_local_sample_age_not_last_changed(
    home_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = home_app(configuration(polling=True))
    add_states(fake_http)
    app.call("home_poll", {}, Origin.ASSISTANT)
    assert app.call("home_poll_status", {}, Origin.ASSISTANT).data["stale"] is False
    monkeypatch.setattr(poll, "now", lambda: MOMENT + timedelta(seconds=301))
    health = app.call("home_poll_status", {}, Origin.ASSISTANT).data
    assert health["stale"] is True
    assert all(item["stale"] for item in health["entities"])
    assert app.ctx.events.recent(source="home") == []
    assert len(fake_http.requests) == 1


def test_status_before_the_first_sample_is_explicitly_unobserved(home_app: AppFactory) -> None:
    app = home_app(configuration(polling=True))
    status = app.call("home_poll_status", {}, Origin.ASSISTANT).data
    assert status["succeeded_at"] is None
    assert status["stale"] is True
    assert all(item["availability"] == "not_observed" for item in status["entities"])


def test_older_concurrent_results_cannot_overwrite_a_newer_sample(home_app: AppFactory) -> None:
    app = home_app(configuration(polling=True))
    store = app.ctx.require("home").store
    later = "2026-11-11T12:00:02.000000+00:00"
    earlier = "2026-11-11T12:00:01.000000+00:00"
    store.success([observation("light.lounge", sample("light.lounge", state="on"), later)], later, later)
    result = store.success([observation("light.lounge", sample("light.lounge"), earlier)], earlier, earlier)
    assert result["ignored_older_sample"] is True
    assert store.snapshots()[0]["state"] == "on"
    assert app.ctx.events.recent(source="home") == []


def test_poll_tool_cannot_override_the_configured_entity_set(
    home_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = home_app(configuration(polling=True))
    outcome = app.call("home_poll", {"entity_id": "lock.front"}, Origin.ASSISTANT)
    assert outcome.status == "invalid"
    assert fake_http.requests == []

"""The webhook receiver with a real app: events, replay protection, auditing, handshakes, and the tool."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.cli import build_parser
from switchboard.connectors import webhooks
from switchboard.connectors.webhooks.hooks import load_settings
from switchboard.connectors.webhooks.receiver import WebhookReceiver
from switchboard.connectors.webhooks.verify import Reply, Request, sign
from switchboard.errors import ConfigError
from switchboard.events import Event
from switchboard.policy import Origin, Tier

AppFactory = Callable[..., App]

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "webhooks"
SECRET = "test-secret"
NOW = 1_800_000_000.0
HOOKS = {
    "ci": {"verify": "hmac-sha256", "secret": "env:SB_TEST_HOOK_SECRET"},
    "gh": {"verify": "github", "secret": "env:SB_TEST_HOOK_SECRET"},
    "backup": {"verify": "token-header", "header": "X-Backup-Token", "secret": "env:SB_TEST_HOOK_SECRET"},
    "outlook": {"verify": "msgraph", "secret": "env:SB_TEST_GRAPH_STATE"},
    "gcal": {"verify": "google-calendar", "secret": "env:SB_TEST_HOOK_SECRET"},
}


def webhooks_config(hooks: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "switchboard": {"connectors": ["webhooks"]},
        "connectors": {"webhooks": {"hooks": hooks, **extra}},
    }


@pytest.fixture
def app(make_app: AppFactory, monkeypatch: pytest.MonkeyPatch) -> App:
    monkeypatch.setenv("SB_TEST_HOOK_SECRET", SECRET)
    monkeypatch.setenv("SB_TEST_GRAPH_STATE", "test-client-state")
    return make_app(webhooks_config(HOOKS))


def receiver_for(app: App) -> WebhookReceiver:
    return WebhookReceiver(app.ctx, load_settings(app.ctx.connector_config("webhooks")), source="webhooks")


def deliver(app: App, hook: str, request: Request, now: float = NOW) -> Reply:
    receiver = receiver_for(app)
    return receiver.handle(receiver.settings.hooks[hook], request, now=now)


def webhook_events(app: App) -> list[Event]:
    return [event for event in app.ctx.events.recent(100) if event.type == "webhook.received"]


def rejections(app: App) -> list[dict[str, Any]]:
    return [record for record in app.ctx.audit.tail(100) if record["kind"] == "webhook.rejected"]


def signed(body: bytes, *, timestamp: int = int(NOW), extra: dict[str, str] | None = None) -> Request:
    headers = {"x-switchboard-signature": sign(SECRET, timestamp, body), **(extra or {})}
    return Request(headers=headers, query={}, body=body)


def test_a_verified_delivery_becomes_one_event(app: App) -> None:
    body = b'{"job":"nightly","status":"passed"}'
    request = signed(body, extra={"content-type": "application/json", "user-agent": "ci-runner/1.0"})

    assert deliver(app, "ci", request) == Reply(202, "accepted\n")

    [event] = webhook_events(app)
    assert event.source == "webhooks"
    assert event.payload == {
        "hook": "ci",
        "received_at": "2027-01-15T08:00:00+00:00",
        "content_type": "application/json",
        "headers": {"content-type": "application/json", "user-agent": "ci-runner/1.0"},
        "size": len(body),
        "json": {"job": "nightly", "status": "passed"},
    }


def test_a_replayed_signature_is_refused_even_after_a_restart(app: App, make_app: AppFactory) -> None:
    request = signed(b"{}")

    assert deliver(app, "ci", request).status == 202
    assert deliver(app, "ci", request, now=NOW + 5) == Reply(401, "verification failed\n")

    restarted = make_app(webhooks_config(HOOKS))
    assert deliver(restarted, "ci", request, now=NOW + 60).status == 401
    assert len(webhook_events(restarted)) == 1
    assert [record["reason"] for record in rejections(restarted)] == [
        "replayed delivery",
        "replayed delivery",
    ]


def test_after_the_window_a_replay_is_stale(app: App) -> None:
    request = signed(b"{}")

    assert deliver(app, "ci", request).status == 202
    assert deliver(app, "ci", request, now=NOW + 301).status == 401
    assert [record["reason"] for record in rejections(app)] == ["timestamp outside the tolerance window"]


def test_failed_verification_is_audited_without_the_body(app: App, data_dir: Path) -> None:
    body = b'{"note":"sb-test-private-body"}'
    forged = Request(
        headers={"x-switchboard-signature": sign("wrong-secret", int(NOW), body)}, query={}, body=body
    )

    assert deliver(app, "ci", forged) == Reply(401, "verification failed\n")

    assert webhook_events(app) == []
    [record] = rejections(app)
    assert (record["hook"], record["reason"], record["size"]) == ("ci", "signature mismatch", len(body))
    assert "sb-test-private-body" not in (data_dir / "audit.jsonl").read_text(encoding="utf-8")


def test_a_github_redelivery_is_a_duplicate_not_a_second_event(app: App) -> None:
    body = b'{"zen":"Keep it logically awesome.","hook_id":1}'
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={digest}",
        "x-github-event": "ping",
        "x-github-delivery": "d-1",
    }

    assert deliver(app, "gh", Request(headers=headers, query={}, body=body)) == Reply(202, "accepted\n")
    resent = Request(headers={**headers, "x-github-delivery": "d-2"}, query={}, body=body)
    assert deliver(app, "gh", resent, now=NOW + 1) == Reply(202, "duplicate\n")

    [event] = webhook_events(app)
    assert event.payload["headers"] == {"x-github-event": "ping", "x-github-delivery": "d-1"}


def test_graph_handshake_then_notifications_without_the_client_state(app: App) -> None:
    query = urllib.parse.parse_qs(
        "validationToken=Validation%3A%20Testing%20client%20application%20reachability"
    )
    handshake = Request(headers={"content-type": "text/plain; charset=utf-8"}, query=query, body=b"")

    assert deliver(app, "outlook", handshake) == Reply(
        200, "Validation: Testing client application reachability"
    )
    assert webhook_events(app) == []

    body = (FIXTURES / "msgraph_notification.json").read_bytes()
    notification = Request(headers={"content-type": "application/json"}, query={}, body=body)
    assert deliver(app, "outlook", notification) == Reply(202, "accepted\n")

    [event] = webhook_events(app)
    items = event.payload["json"]["value"]
    assert [item.get("changeType", item.get("lifecycleEvent")) for item in items] == [
        "created",
        "reauthorizationRequired",
    ]
    assert all("clientState" not in item for item in items)

    # A batch with a lifecycle item (no id) is never treated as a retry of an earlier one.
    assert deliver(app, "outlook", notification, now=NOW + 30) == Reply(202, "accepted\n")
    assert len(webhook_events(app)) == 2

    # A batch whose items all carry ids is deduplicated across Graph's retries.
    document = json.loads(body)
    document["value"] = document["value"][:1]
    with_ids = Request(
        headers={"content-type": "application/json"}, query={}, body=json.dumps(document).encode()
    )
    assert deliver(app, "outlook", with_ids, now=NOW + 40) == Reply(202, "accepted\n")
    assert deliver(app, "outlook", with_ids, now=NOW + 50) == Reply(202, "duplicate\n")
    assert len(webhook_events(app)) == 3
    assert "test-client-state" not in json.dumps(event.payload)


def test_google_sync_is_acknowledged_and_changes_are_deduplicated(app: App) -> None:
    headers = {
        "x-goog-channel-id": "sb-test-channel-1",
        "x-goog-channel-token": SECRET,
        "x-goog-resource-id": "sb-test-resource",
        "x-goog-resource-uri": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
    }
    sync = {**headers, "x-goog-resource-state": "sync", "x-goog-message-number": "1"}
    change = {**headers, "x-goog-resource-state": "exists", "x-goog-message-number": "10"}

    assert deliver(app, "gcal", Request(headers=sync, query={}, body=b"")) == Reply(200, "")
    assert webhook_events(app) == []
    assert deliver(app, "gcal", Request(headers=change, query={}, body=b"")).status == 202
    assert (
        deliver(app, "gcal", Request(headers=change, query={}, body=b""), now=NOW + 1).text == "duplicate\n"
    )

    [event] = webhook_events(app)
    assert event.payload["headers"]["x-goog-resource-state"] == "exists"
    assert "x-goog-channel-token" not in event.payload["headers"]
    assert event.payload["text"] == ""


def test_text_bodies_are_capped_and_the_token_header_is_not_kept(app: App) -> None:
    body = b"x" * 25_000
    headers = {"x-backup-token": SECRET, "content-type": "text/plain"}

    assert deliver(app, "backup", Request(headers=headers, query={}, body=body)).status == 202

    [event] = webhook_events(app)
    assert len(event.payload["text"]) == 20_000
    assert event.payload["size"] == 25_000
    assert event.payload["headers"] == {"content-type": "text/plain"}


def test_an_unresolvable_secret_answers_503_and_doctor_names_it(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SB_TEST_UNSET_SECRET", raising=False)
    app = make_app(webhooks_config({"ci": {"verify": "hmac-sha256", "secret": "env:SB_TEST_UNSET_SECRET"}}))

    assert deliver(app, "ci", signed(b"{}")).status == 503
    assert webhooks.doctor(app.ctx) == ["hook ci: environment variable 'SB_TEST_UNSET_SECRET' is not set"]
    assert webhook_events(app) == []


def test_webhooks_recent_lists_deliveries_without_bodies(app: App) -> None:
    first = b'{"job":"nightly"}'
    second = b"backup finished"
    deliver(app, "ci", signed(first))
    deliver(app, "backup", Request(headers={"x-backup-token": SECRET}, query={}, body=second), now=NOW + 1)

    tool = app.registry.get("webhooks_recent")
    assert tool is not None
    assert tool.tier is Tier.READ
    outcome = app.call("webhooks_recent", {"limit": 5}, Origin.ASSISTANT)

    assert outcome.status == "ok"
    deliveries = outcome.data["deliveries"]
    assert [(item["hook"], item["size"]) for item in deliveries] == [
        ("backup", len(second)),
        ("ci", len(first)),
    ]
    assert all(set(item) == {"event_id", "hook", "received_at", "size"} for item in deliveries)
    assert "backup finished" not in json.dumps(outcome.data)


@pytest.mark.parametrize(
    ("hooks", "extra", "message"),
    [
        ({"ci": {"verify": "basic", "secret": "env:X"}}, {}, "verify must be one of"),
        (
            {"ci": {"verify": "github", "secret": "plain-text-secret"}},
            {},
            "secret must be a secret reference",
        ),
        ({"ci": {"verify": "token-header", "secret": "env:X"}}, {}, "token-header hooks need header"),
        (
            {"ci": {"verify": "hmac-sha256", "secret": "env:X", "tolerance_seconds": 5}},
            {},
            "tolerance_seconds",
        ),
        ({"Bad Name": {"verify": "github", "secret": "env:X"}}, {}, "hook names use lowercase"),
        ({}, {"max_body_bytes": 10}, "max_body_bytes"),
    ],
)
def test_bad_hook_tables_fail_at_startup(
    make_app: AppFactory, hooks: dict[str, Any], extra: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        make_app(webhooks_config(hooks, **extra))


def test_doctor_without_hooks(make_app: AppFactory) -> None:
    app = make_app(webhooks_config({}))
    assert webhooks.doctor(app.ctx) == ["no hooks configured under [connectors.webhooks.hooks]"]


def test_serve_command_defaults_to_loopback() -> None:
    parser = build_parser([webhooks])

    args = parser.parse_args(["webhooks", "serve"])

    assert isinstance(args, argparse.Namespace)
    assert (args.host, args.port) == ("127.0.0.1", 8787)
    assert callable(args.app_handler)
    assert parser.parse_args(["webhooks", "serve", "--port", "9000"]).port == 9000

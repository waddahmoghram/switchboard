"""Bot input validation, durable offsets, retry pauses, and privacy, with fake transport only."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors.telegram.client import TelegramError
from switchboard.connectors.telegram.service import TelegramService, normalize_message
from switchboard.connectors.telegram.settings import load_settings
from switchboard.errors import ConfigError, ToolError, UncertainOutcomeError
from tests.conftest import AppFactory
from tests.fakes import FakeHttp
from tests.fixtures.telegram import BASE, BOT_ID, CHAT_ID, NOW, TOKEN, config, identity, respond, update


@pytest.fixture
def app(make_app: AppFactory, monkeypatch: pytest.MonkeyPatch) -> App:
    monkeypatch.setenv("SB_TEST_TELEGRAM_BOT", TOKEN)
    return make_app(config())


@pytest.mark.parametrize(
    "values",
    [
        {"allowed_user_ids": []},
        {"allowed_chat_ids": []},
        {"allowed_user_ids": [True]},
        {"allowed_chat_ids": [0]},
        {"allowed_user_ids": [-1]},
        {"allow_groups": "yes"},
        {"poll_interval_seconds": 1},
        {"long_poll_seconds": 60},
        {"unknown": True},
    ],
)
def test_invalid_settings_are_refused(values: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_settings(config(**values)["connectors"]["telegram"])


def test_unapproved_users_chats_bots_and_group_messages_are_not_imported() -> None:
    settings = load_settings(config()["connectors"]["telegram"])
    assert normalize_message(update(), settings) is not None
    for rejected in [
        update(user=222),
        update(chat=222),
        update(chat_type="group"),
        {"update_id": 1},
    ]:
        assert normalize_message(rejected, settings) is None
    bot = update()
    bot["message"]["from"]["is_bot"] = True
    assert normalize_message(bot, settings) is None


def test_explicit_groups_need_both_sender_and_chat_allowlists() -> None:
    settings = load_settings(config(allow_groups=True, allowed_chat_ids=[-100123])["connectors"]["telegram"])
    assert normalize_message(update(chat=-100123, chat_type="supergroup"), settings) is not None
    assert normalize_message(update(user=222, chat=-100123, chat_type="supergroup"), settings) is None


def test_registration_and_inbox_status_do_not_contact_telegram(app: App, fake_http: FakeHttp) -> None:
    service: TelegramService = app.ctx.require("telegram.inbox")
    assert service.status({})["bot_id"] is None
    assert fake_http.requests == []
    with pytest.raises(ToolError, match="start 'switchboard run'"):
        service.messages({})


def test_updates_are_durable_and_acknowledged_only_on_the_next_poll(app: App, fake_http: FakeHttp) -> None:
    identity(fake_http)
    respond(fake_http, "getUpdates", [update()])
    respond(fake_http, "getUpdates", [update()])
    service: TelegramService = app.ctx.require("telegram.inbox")
    service.clock = lambda: NOW

    assert service.poll()["inserted"] == 1
    assert service.poll()["inserted"] == 0

    polls = [request for request in fake_http.requests if request.url.endswith("/getUpdates")]
    assert "offset" not in json.loads(polls[0].body or b"{}")
    assert json.loads(polls[1].body or b"{}")["offset"] == 101
    assert len(service.messages({})["messages"]) == 1
    [event] = app.ctx.events.pending()
    assert event.type == "telegram.message"
    assert event.payload["untrusted_content"] is True
    assert all(not request.follow_redirects for request in fake_http.requests)


def test_invalid_update_batches_do_not_advance_the_cursor(app: App, fake_http: FakeHttp) -> None:
    identity(fake_http)
    respond(fake_http, "getUpdates", [update(), {"update_id": "bad"}])
    service: TelegramService = app.ctx.require("telegram.inbox")
    service.clock = lambda: NOW
    with pytest.raises(ToolError, match="cursor was not advanced"):
        service.poll()
    assert service.inbox.offset(BOT_ID, NOW) is None
    assert app.ctx.events.recent() == []
    assert service.status({})["poll"]["error"]


def test_a_long_idle_gap_resets_the_transport_offset_not_message_deduplication(
    app: App, fake_http: FakeHttp
) -> None:
    identity(fake_http)
    respond(fake_http, "getUpdates", [update(update_id=900)])
    respond(fake_http, "getUpdates", [update(update_id=20, message_id=11)])
    service: TelegramService = app.ctx.require("telegram.inbox")
    service.clock = lambda: NOW
    service.poll()
    service.clock = lambda: NOW + timedelta(days=7)
    assert service.poll()["inserted"] == 1
    requests = [request for request in fake_http.requests if request.url.endswith("/getUpdates")]
    assert "offset" not in json.loads(requests[1].body or b"{}")


def test_polling_records_ignored_updates_without_storing_foreign_messages(
    app: App, fake_http: FakeHttp
) -> None:
    identity(fake_http)
    respond(fake_http, "getUpdates", [update(user=222, text="private foreign text"), update()])
    service: TelegramService = app.ctx.require("telegram.inbox")
    report = service.poll()
    assert (report["received"], report["inserted"], report["ignored"]) == (2, 1, 1)
    assert all("private foreign text" not in json.dumps(event.payload) for event in app.ctx.events.recent())
    with app.ctx.db.session() as conn:
        rows = conn.execute("SELECT text FROM telegram_messages").fetchall()
    assert len(rows) == 1
    assert all("private foreign text" not in row["text"] for row in rows)


def test_conflicting_receivers_are_reported_without_clearing_a_webhook(app: App, fake_http: FakeHttp) -> None:
    identity(fake_http)
    fake_http.add("POST", BASE + "getUpdates", status=409, body='{"ok":false}')
    service: TelegramService = app.ctx.require("telegram.inbox")
    with pytest.raises(TelegramError, match="another poller or an active webhook"):
        service.poll()
    assert all("deleteWebhook" not in request.url for request in fake_http.requests)


def test_rate_limit_stops_repeat_requests_and_does_not_expose_tokens(app: App, fake_http: FakeHttp) -> None:
    identity(fake_http)
    fake_http.add(
        "POST",
        BASE + "getUpdates",
        status=429,
        body=json.dumps({"ok": False, "description": TOKEN, "parameters": {"retry_after": 60}}),
    )
    service: TelegramService = app.ctx.require("telegram.inbox")
    with pytest.raises(TelegramError, match="rate limited") as first:
        service.poll()
    assert TOKEN not in str(first.value)
    count = len(fake_http.requests)
    with pytest.raises(TelegramError, match="pause"):
        service.poll()
    assert len(fake_http.requests) == count


def test_paging_and_expiration_are_local_only(app: App, fake_http: FakeHttp) -> None:
    identity(fake_http)
    respond(fake_http, "getUpdates", [update(), update(update_id=101, message_id=11)])
    service: TelegramService = app.ctx.require("telegram.inbox")
    service.clock = lambda: NOW
    service.poll()
    first = service.messages({"limit": 1})
    assert first["truncated"]
    second = service.messages({"after_id": first["next_after_id"], "limit": 1})
    assert len(second["messages"]) == 1
    assert second["messages"][0]["id"] != first["messages"][0]["id"]
    service.inbox.prune(NOW + timedelta(days=15), 14)
    assert service.messages({})["messages"] == []
    assert len(fake_http.requests) == 2


def _received(app: App, fake_http: FakeHttp) -> tuple[TelegramService, dict[str, Any]]:
    identity(fake_http)
    respond(fake_http, "getUpdates", [update()])
    service: TelegramService = app.ctx.require("telegram.inbox")
    service.clock = lambda: NOW
    service.poll()
    [message] = service.messages({})["messages"]
    return service, message


@pytest.mark.parametrize("failure", ["transport", 500, "unreadable", "wrong_chat"])
def test_replies_that_may_have_been_delivered_raise_uncertain_outcomes(
    app: App, fake_http: FakeHttp, failure: str | int
) -> None:
    service, message = _received(app, fake_http)
    if failure == "transport":
        fake_http.add_error("POST", BASE + "sendMessage", "simulated timeout")
    elif failure == "unreadable":
        fake_http.add("POST", BASE + "sendMessage", body="not json")
    elif failure == "wrong_chat":
        respond(fake_http, "sendMessage", {"message_id": 900, "chat": {"id": 222}})
    else:
        fake_http.add("POST", BASE + "sendMessage", status=500, body='{"ok":false}')

    with pytest.raises(UncertainOutcomeError) as raised:
        service.reply({"id": message["id"], "chat_id": CHAT_ID, "text": "Hello."})

    assert TOKEN not in str(raised.value)
    assert service.read({"id": message["id"]})["reply_state"] == "unknown"


def test_a_refused_reply_is_a_definite_failure(app: App, fake_http: FakeHttp) -> None:
    service, message = _received(app, fake_http)
    fake_http.add("POST", BASE + "sendMessage", status=403, body='{"ok":false}')

    with pytest.raises(TelegramError, match="not permitted") as raised:
        service.reply({"id": message["id"], "chat_id": CHAT_ID, "text": "Hello."})

    assert not isinstance(raised.value, UncertainOutcomeError)
    assert service.read({"id": message["id"]})["reply_state"] == "failed"

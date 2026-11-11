"""Request shapes for the notify channels (ntfy, Telegram, email). Nothing here touches the network."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors.notify.service import make_notification
from tests.fakes import FakeHttp, FakeMailService

AppFactory = Callable[..., App]

NTFY_TOPIC = "sb-test-topic_42"
BOT = "123456:TEST-token_abc"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT}/sendMessage"
TELEGRAM_OK = '{"ok":true,"result":{"message_id":7}}'


def notify_config(channels: dict[str, Any]) -> dict[str, Any]:
    return {"switchboard": {"connectors": ["notify"]}, "connectors": {"notify": {"channels": channels}}}


def ntfy_channel(**extra: Any) -> dict[str, Any]:
    return {"type": "ntfy", "topic": "env:SB_TEST_NTFY_TOPIC", **extra}


def telegram_channel(chat_id: int | str = 987654321) -> dict[str, Any]:
    return {"type": "telegram", "bot_token": "env:SB_TEST_TG_TOKEN", "chat_id": chat_id}


@pytest.fixture(autouse=True)
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SB_TEST_NTFY_TOPIC", NTFY_TOPIC)
    monkeypatch.setenv("SB_TEST_NTFY_TOKEN", "tk_test-token")
    monkeypatch.setenv("SB_TEST_TG_TOKEN", BOT)


def test_ntfy_sends_the_message_as_body_and_metadata_as_headers(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(
        notify_config({"phone": ntfy_channel(token="env:SB_TEST_NTFY_TOKEN", tags=["robot", "warning"])})
    )
    fake_http.add("POST", f"https://ntfy.sh/{NTFY_TOPIC}", body='{"id":"sbtest1"}')

    sent = app.ctx.require("notify").send(
        "Card charge", "42.00 EUR at Example Shop", priority="high", url="https://example.test/tx/1"
    )

    assert sent == ["phone"]
    [request] = fake_http.requests
    assert request.method == "POST"
    assert request.headers == {
        "content-type": "text/plain; charset=utf-8",
        "title": "Card charge",
        "priority": "high",
        "tags": "robot,warning",
        "click": "https://example.test/tx/1",
        "authorization": "Bearer tk_test-token",
    }
    assert request.body == b"42.00 EUR at Example Shop"


def test_ntfy_minimal_request_to_your_own_server(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(notify_config({"phone": ntfy_channel(server="https://ntfy.example.test/")}))
    fake_http.add("POST", f"https://ntfy.example.test/{NTFY_TOPIC}")

    assert app.ctx.require("notify").send("Backup", "Done") == ["phone"]

    [request] = fake_http.requests
    assert request.headers == {
        "content-type": "text/plain; charset=utf-8",
        "title": "Backup",
        "priority": "default",
    }
    assert request.body == b"Done"


def test_ntfy_encodes_non_ascii_titles_as_rfc2047_words(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(notify_config({"phone": ntfy_channel()}))
    fake_http.add("POST", f"https://ntfy.sh/{NTFY_TOPIC}")

    app.ctx.require("notify").send("Caf\u00e9 \u2615", "Order ready")

    [request] = fake_http.requests
    encoded = base64.b64encode("Caf\u00e9 \u2615".encode()).decode("ascii")
    assert request.headers["title"] == f"=?UTF-8?B?{encoded}?="


def test_ntfy_body_stays_within_the_message_size_limit(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(notify_config({"phone": ntfy_channel()}))
    fake_http.add("POST", f"https://ntfy.sh/{NTFY_TOPIC}")

    app.ctx.require("notify").send("Long", "\u20ac" * 3000)  # 9,000 bytes of UTF-8

    [request] = fake_http.requests
    assert request.body is not None
    assert len(request.body) <= 4096
    text = request.body.decode("utf-8")
    assert text.endswith("\u2026")
    assert set(text[:-1]) == {"\u20ac"}


def test_ntfy_plain_http_only_with_allow_http(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(notify_config({"lan": ntfy_channel(server="http://ntfy.lan.test:8080", allow_http=True)}))
    fake_http.add("POST", f"http://ntfy.lan.test:8080/{NTFY_TOPIC}")

    assert app.ctx.require("notify").send("Local", "Plain HTTP on my own network") == ["lan"]


def test_telegram_send_message_payload(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(notify_config({"chat": telegram_channel()}))
    fake_http.add("POST", TELEGRAM_URL, body=TELEGRAM_OK)

    sent = app.ctx.require("notify").send(
        "Reminder", "Water the plants", priority="low", url="https://example.test/plants"
    )

    assert sent == ["chat"]
    [request] = fake_http.requests
    assert request.headers == {"content-type": "application/json"}
    assert json.loads(request.body or b"") == {
        "chat_id": 987654321,
        "text": "Reminder\n\nWater the plants\n\nhttps://example.test/plants",
        "disable_notification": True,
        "link_preview_options": {"is_disabled": True},
    }


@pytest.mark.parametrize(
    ("priority", "silent"),
    [("min", True), ("low", True), ("default", False), ("high", False), ("urgent", False)],
)
def test_telegram_quiet_priorities_send_silently(
    make_app: AppFactory, fake_http: FakeHttp, priority: str, silent: bool
) -> None:
    app = make_app(notify_config({"chat": telegram_channel(chat_id="@sb_test_channel")}))
    fake_http.add("POST", TELEGRAM_URL, body=TELEGRAM_OK)

    app.ctx.require("notify").send("Title", "Message", priority=priority)

    payload = json.loads(fake_http.requests[0].body or b"")
    assert payload["chat_id"] == "@sb_test_channel"
    assert payload["disable_notification"] is silent
    assert payload["text"] == "Title\n\nMessage"


def test_email_channel_mails_your_own_address_through_mail_sender(make_app: AppFactory) -> None:
    app = make_app(
        notify_config({"inbox": {"type": "email", "to": ["owner@example.test"], "account": "personal"}})
    )
    mail = FakeMailService()
    app.ctx.provide("mail.sender", mail)

    sent = app.ctx.require("notify").send(
        "Backup done", "All volumes verified.", url="https://example.test/report"
    )

    assert sent == ["inbox"]
    [(account, message)] = mail.sent
    assert account == "personal"
    assert message["To"] == "owner@example.test"
    assert message["Subject"] == "Backup done"
    assert message.get_content() == "All volumes verified.\n\nhttps://example.test/report\n"
    assert mail.drafts == []


def test_email_channel_without_the_mail_connector_fails_clearly(make_app: AppFactory) -> None:
    app = make_app(notify_config({"inbox": {"type": "email", "to": "owner@example.test"}}))
    service = app.ctx.require("notify")

    assert service.send("Hi", "Test") == []

    report = service.deliver(make_notification("Hi", "Test"))
    assert report.sent == []
    assert report.failed == {"inbox": "needs the mail.sender service; enable the mail connector"}

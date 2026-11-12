"""The notify service and the notify_self tool: isolation of failures, bounds, secrets, and config."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from switchboard import execution
from switchboard.app import App
from switchboard.connectors import notify
from switchboard.connectors.notify.service import make_notification
from switchboard.errors import ConfigError, HttpError, UncertainOutcomeError
from switchboard.httpclient import HttpResponse
from switchboard.policy import Origin, Tier
from tests.fakes import FakeHttp, FakeMailService, RecordedRequest

AppFactory = Callable[..., App]

NTFY_TOPIC = "sb-test-topic_42"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
BOT = "123456:TEST-secret_value"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT}/sendMessage"
TELEGRAM_OK = '{"ok":true,"result":{"message_id":7}}'
PHONE = {"type": "ntfy", "topic": "env:SB_TEST_NTFY_TOPIC"}
CHAT = {"type": "telegram", "bot_token": "env:SB_TEST_TG_TOKEN", "chat_id": "-100123"}
INBOX = {"type": "email", "to": "owner@example.test"}


def notify_config(channels: dict[str, Any]) -> dict[str, Any]:
    return {"switchboard": {"connectors": ["notify"]}, "connectors": {"notify": {"channels": channels}}}


@pytest.fixture(autouse=True)
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SB_TEST_NTFY_TOPIC", NTFY_TOPIC)
    monkeypatch.setenv("SB_TEST_TG_TOKEN", BOT)


def test_a_failing_channel_does_not_stop_the_others(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(notify_config({"phone": PHONE, "chat": CHAT, "inbox": INBOX}))
    mail = FakeMailService()
    app.ctx.provide("mail.sender", mail)
    fake_http.add("POST", NTFY_URL, status=403, body="forbidden")
    fake_http.add("POST", TELEGRAM_URL, body=TELEGRAM_OK)

    outcome = app.call(
        "notify_self",
        {"title": "Disk almost full", "message": "93% used on the backup disk"},
        Origin.ASSISTANT,
    )

    assert outcome.status == "ok"
    assert outcome.data == {"sent": ["chat", "inbox"], "failed": {"phone": "ntfy answered HTTP 403"}}
    assert outcome.action is not None and outcome.action["state"] == "succeeded"
    assert len(mail.sent) == 1
    assert [request.url for request in fake_http.requests] == [NTFY_URL, TELEGRAM_URL]


def test_a_server_error_leaves_that_channel_uncertain_and_blocks_an_identical_repeat(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(notify_config({"phone": PHONE, "chat": CHAT}))
    fake_http.add("POST", NTFY_URL, status=502, body="bad gateway")
    fake_http.add("POST", TELEGRAM_URL, body=TELEGRAM_OK)
    args = {"title": "Backup", "message": "The nightly backup finished"}

    outcome = app.call("notify_self", args, Origin.ASSISTANT)

    assert outcome.status == "ok"
    assert outcome.data == {
        "sent": ["chat"],
        "failed": {},
        "uncertain": {"phone": "ntfy answered HTTP 502; the note may have been delivered and was not resent"},
        "effect_unknown": True,
    }
    assert outcome.action is not None and outcome.action["state"] == "uncertain"

    repeat = app.call("notify_self", args, Origin.ASSISTANT)

    assert repeat.status == "denied"
    assert outcome.action["id"] in repeat.message
    assert len(fake_http.requests) == 2

    app.journal.reconcile(outcome.action["id"], applied=True, note="the phone showed the note")
    fake_http.add("POST", NTFY_URL)
    fake_http.add("POST", TELEGRAM_URL, body=TELEGRAM_OK)

    assert app.call("notify_self", args, Origin.ASSISTANT).status == "ok"


def test_when_no_channel_confirms_and_one_may_have_delivered_the_call_is_uncertain(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(notify_config({"phone": PHONE, "chat": CHAT}))
    fake_http.add("POST", NTFY_URL, status=400, body="bad request")
    fake_http.add("POST", TELEGRAM_URL, status=200, body="<html>captive portal</html>")

    outcome = app.call("notify_self", {"title": "t", "message": "m"}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert outcome.message.startswith(
        "no channel confirmed the notification (phone: ntfy answered HTTP 400; "
    )
    assert "chat: Telegram answered HTTP 200; the note may have been delivered" in outcome.message
    assert outcome.action is not None and outcome.action["state"] == "uncertain"


def test_definite_failures_on_every_channel_are_a_plain_error(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(notify_config({"phone": PHONE}))
    fake_http.add("POST", NTFY_URL, status=429, body="slow down")

    outcome = app.call("notify_self", {"title": "t", "message": "m"}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert outcome.message == "no channel accepted the notification (phone: ntfy answered HTTP 429)"
    assert outcome.action is not None and outcome.action["state"] == "failed"


def test_an_email_channel_passes_on_an_uncertain_send(make_app: AppFactory) -> None:
    class DroppingMail(FakeMailService):
        def send(self, message: EmailMessage, account: str | None = None) -> str:
            raise UncertainOutcomeError("the server may have accepted it; nothing was resent")

    app = make_app(notify_config({"inbox": INBOX}))
    app.ctx.provide("mail.sender", DroppingMail())

    report = app.ctx.require("notify").deliver(make_notification("t", "m"))

    assert report.sent == []
    assert report.uncertain == {
        "inbox": "the mail connector could not confirm sending: "
        "the server may have accepted it; nothing was resent"
    }


def test_channels_are_not_attempted_once_the_deadline_has_passed(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(notify_config({"phone": PHONE, "chat": CHAT}))
    clock = [100.0]
    monkeypatch.setattr(execution, "monotonic", lambda: clock[0])

    def slow(request: RecordedRequest) -> HttpResponse:
        clock[0] += 10
        return HttpResponse(status=200, url=request.url, headers={}, body=b"")

    fake_http.add_handler("POST", NTFY_URL, slow)

    with execution.deadline(5, "tool notify_self"):
        report = app.ctx.require("notify").deliver(make_notification("t", "m"))

    assert report.sent == ["phone"]
    assert report.failed == {"chat": "not attempted: the tool notify_self deadline passed"}
    assert report.uncertain == {}
    assert [request.url for request in fake_http.requests] == [NTFY_URL]


def test_unexpected_errors_are_reported_by_type_without_the_url(
    make_app: AppFactory, fake_http: FakeHttp, caplog: pytest.LogCaptureFixture
) -> None:
    app = make_app(notify_config({"phone": PHONE, "chat": CHAT}))
    # No ntfy route: FakeHttp raises an AssertionError that quotes the URL, topic included.
    fake_http.add("POST", TELEGRAM_URL, body=TELEGRAM_OK)
    caplog.set_level(logging.DEBUG)

    sent = app.ctx.require("notify").send("Test", "Body")

    assert sent == ["chat"]
    assert "notify channel phone outcome unknown: internal error (AssertionError)" in caplog.text
    assert NTFY_TOPIC not in caplog.text
    assert BOT not in caplog.text


def test_telegram_token_stays_out_of_errors_logs_and_audit(
    make_app: AppFactory, fake_http: FakeHttp, caplog: pytest.LogCaptureFixture, data_dir: Path
) -> None:
    app = make_app(notify_config({"chat": {**CHAT, "chat_id": 42}}))
    error = {"ok": False, "error_code": 404, "description": f"Not Found: bot{BOT}"}
    fake_http.add("POST", TELEGRAM_URL, status=404, body=json.dumps(error))
    caplog.set_level(logging.DEBUG)

    outcome = app.call("notify_self", {"title": "t", "message": "m"}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert "Telegram answered HTTP 404: Not Found: bot[redacted]" in outcome.message
    assert BOT not in outcome.message
    assert BOT not in caplog.text
    assert BOT not in (data_dir / "audit.jsonl").read_text(encoding="utf-8")


def test_telegram_network_errors_never_quote_the_url(
    make_app: AppFactory, fake_http: FakeHttp, caplog: pytest.LogCaptureFixture
) -> None:
    app = make_app(notify_config({"chat": CHAT}))

    def careless_transport(request: RecordedRequest) -> HttpResponse:
        raise HttpError(f"POST {request.url}: connection reset")

    fake_http.add_handler("POST", TELEGRAM_URL, careless_transport)
    caplog.set_level(logging.DEBUG)

    outcome = app.call("notify_self", {"title": "t", "message": "m"}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert "chat: could not reach api.telegram.org" in outcome.message
    assert outcome.action is not None and outcome.action["state"] == "uncertain"
    assert BOT not in outcome.message
    assert BOT not in caplog.text


def test_notify_self_is_a_draft_tool_that_runs_without_approval(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(notify_config({"phone": PHONE}))
    tool = app.registry.get("notify_self")
    assert tool is not None
    assert tool.tier is Tier.DRAFT
    fake_http.add("POST", NTFY_URL)

    outcome = app.call("notify_self", {"title": "Hello", "message": "From a test"}, Origin.ASSISTANT)

    assert outcome.status == "ok"
    assert outcome.data == {"sent": ["phone"], "failed": {}}


def test_notify_self_can_target_a_subset_of_channels(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(notify_config({"phone": PHONE, "chat": CHAT}))
    fake_http.add("POST", TELEGRAM_URL, body=TELEGRAM_OK)

    outcome = app.call(
        "notify_self", {"title": "t", "message": "m", "channels": ["chat", "chat"]}, Origin.ASSISTANT
    )

    assert outcome.data == {"sent": ["chat"], "failed": {}}
    assert [request.url for request in fake_http.requests] == [TELEGRAM_URL]


def test_notify_self_rejects_unknown_channel_names(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(notify_config({"phone": PHONE}))

    outcome = app.call("notify_self", {"title": "t", "message": "m", "channels": ["pager"]}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert outcome.message == "unknown channel(s): pager; configured: phone"
    assert fake_http.requests == []


@pytest.mark.parametrize(
    "args",
    [
        {"title": "x" * 121, "message": "m"},
        {"title": "t", "message": "m" * 2001},
        {"title": "t", "message": "m", "priority": "max"},
        {"title": "t", "message": "m", "url": "http://example.test/"},
        {"title": "t", "message": "m", "url": "https://example.test/a b"},
        {"title": "t", "message": "m", "to": "someone@example.test"},
        {"title": "t", "message": "m", "channels": []},
        {"title": "t"},
    ],
)
def test_notify_self_rejects_out_of_bounds_arguments(
    make_app: AppFactory, fake_http: FakeHttp, args: dict[str, Any]
) -> None:
    app = make_app(notify_config({"phone": PHONE}))

    outcome = app.call("notify_self", args, Origin.ASSISTANT)

    assert outcome.status == "invalid"
    assert fake_http.requests == []


def test_notify_self_rejects_a_link_with_a_trailing_newline(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(notify_config({"phone": PHONE}))

    # The core validator makes a pattern ending in "$" reach the very end, so the schema refuses this.
    outcome = app.call(
        "notify_self", {"title": "t", "message": "m", "url": "https://example.test/\n"}, Origin.ASSISTANT
    )

    assert outcome.status == "invalid"
    assert fake_http.requests == []
    assert fake_http.requests == []


def test_service_normalizes_input_from_other_connectors(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(notify_config({"phone": PHONE}))
    fake_http.add("POST", NTFY_URL)

    sent = app.ctx.require("notify").send(
        "Line one\nLine\ttwo", "Body", priority="loud", url="http://example.test/"
    )

    assert sent == ["phone"]
    [request] = fake_http.requests
    assert request.headers["title"] == "Line one Line two"
    assert request.headers["priority"] == "default"
    assert "click" not in request.headers


def test_a_topic_that_could_change_the_url_is_refused(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_NTFY_TOPIC", "topic/../admin")
    app = make_app(notify_config({"phone": PHONE}))

    report = app.ctx.require("notify").deliver(make_notification("t", "m"))

    assert report.failed == {"phone": "the topic must be 1-64 letters, digits, '-' or '_'"}
    assert fake_http.requests == []


def test_without_channels_the_tool_explains_what_is_missing(make_app: AppFactory) -> None:
    app = make_app({"switchboard": {"connectors": ["notify"]}})

    outcome = app.call("notify_self", {"title": "t", "message": "m"}, Origin.ASSISTANT)

    assert outcome.status == "error"
    assert outcome.message == "no notification channels are configured"
    assert notify.doctor(app.ctx) == ["no channels configured under [connectors.notify.channels]"]


@pytest.mark.parametrize(
    ("channel", "message"),
    [
        ({"type": "pager"}, "type must be ntfy, telegram, or email"),
        ({"type": "ntfy", "topic": "my-plain-topic"}, "topic must be a secret reference"),
        ({**PHONE, "server": "http://ntfy.example.test"}, "set allow_http = true"),
        ({**PHONE, "server": "https://user:pw@ntfy.example.test"}, "without credentials"),
        ({**PHONE, "tags": ["has space"]}, "tags must list"),
        ({**CHAT, "chat_id": "not a chat"}, "chat_id must be"),
        ({**CHAT, "bot_token": BOT}, "bot_token must be a secret reference"),
        ({"type": "email", "to": []}, "to must list"),
    ],
)
def test_bad_channel_tables_fail_at_startup(
    make_app: AppFactory, channel: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        make_app(notify_config({"bad": channel}))


def test_doctor_reports_secret_problems_without_values(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SB_TEST_UNSET_TOPIC", raising=False)
    monkeypatch.setenv("SB_TEST_BAD_TOPIC", "has/slash")
    app = make_app(
        notify_config(
            {
                "phone": {"type": "ntfy", "topic": "env:SB_TEST_UNSET_TOPIC"},
                "lan": {"type": "ntfy", "topic": "env:SB_TEST_BAD_TOPIC"},
                "inbox": INBOX,
            }
        )
    )

    problems = notify.doctor(app.ctx)

    assert problems == [
        "channel phone: topic: environment variable 'SB_TEST_UNSET_TOPIC' is not set",
        "channel lan: topic: the secret resolves but does not have the expected form",
        "channel inbox: needs the mail.sender service; enable the mail connector",
    ]
    assert not any("has/slash" in problem for problem in problems)

"""The mail connector inside a full app: tools through the policy gate, approvals, services, polling."""

from __future__ import annotations

import email
import email.policy
import json
import time
from email.message import EmailMessage
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors import mail
from switchboard.connectors.mail import imap as mail_imap
from switchboard.connectors.mail import smtp as mail_smtp
from switchboard.connectors.mail.oauth import TokenSet
from switchboard.policy import Origin
from tests.conftest import AppFactory
from tests.fakes import FakeHttp
from tests.fixtures.mail.fakes import FakeImapServer, FakeMailbox, FakeSmtp, load, simple_message

pytestmark = pytest.mark.integration

PASSWORD_ENV = "SWITCHBOARD_TEST_MAIL_PASSWORD"
PASSWORD = "app-password-for-tests"
SEND_ARGS: dict[str, Any] = {
    "account": "personal",
    "to": ["sam@example.org"],
    "cc": ["jordan@example.com"],
    "bcc": ["records@example.com"],
    "subject": "Lunch on Friday?",
    "body": "Does noon work?",
}


def config(**overrides: Any) -> dict[str, Any]:
    """App configuration with one Fastmail account; an override of None removes that key."""
    account: dict[str, Any] = {
        "provider": "fastmail",
        "address": "alex@example.com",
        "password": f"env:{PASSWORD_ENV}",
        "watch": ["INBOX"],
        "send_enabled": True,
    }
    account.update(overrides)
    return {
        "switchboard": {"connectors": ["mail"]},
        "connectors": {
            "mail": {"accounts": {"personal": {k: v for k, v in account.items() if v is not None}}}
        },
        "policy": {"tools": {"mail_send": {"allow_recipients": ["*@example.com", "*@example.org"]}}},
    }


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeImapServer:
    inbox = FakeMailbox("INBOX", uidvalidity=11)
    inbox.add(load("plain.eml"))
    inbox.add(load("multipart_attachment.eml"), flags=("\\Seen",))
    inbox.add(load("html_only.eml"))
    drafts = FakeMailbox("Drafts", attributes=("\\HasNoChildren", "\\Drafts"))
    fake = FakeImapServer([inbox, drafts])
    monkeypatch.setattr(mail_imap, "connect_imap", fake.connect)
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    return fake


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> FakeSmtp:
    fake = FakeSmtp()
    monkeypatch.setattr(mail_smtp, "connect_smtp", fake.connect)
    return fake


def ok(outcome: Any) -> Any:
    assert outcome.status == "ok", outcome.message
    return outcome.data


def test_accounts_tool_lists_accounts_without_secrets(make_app: AppFactory, server: FakeImapServer) -> None:
    app = make_app(config())
    data = ok(app.call("mail_accounts", {}, Origin.ASSISTANT))
    assert data == {
        "accounts": [
            {
                "name": "personal",
                "address": "alex@example.com",
                "provider": "fastmail",
                "auth": "password",
                "send_enabled": True,
                "watch": ["INBOX"],
            }
        ],
        "misconfigured": [],
    }
    assert server.connections == []


def test_mailboxes_tool_reports_special_use(make_app: AppFactory, server: FakeImapServer) -> None:
    app = make_app(config())
    data = ok(app.call("mail_mailboxes", {"account": "personal"}, Origin.ASSISTANT))
    assert data["mailboxes"] == [
        {"name": "INBOX", "special_use": [], "selectable": True},
        {"name": "Drafts", "special_use": ["\\Drafts"], "selectable": True},
    ]
    assert server.addresses == [("imap.fastmail.com", 993, 30.0)]
    assert server.connections[0].logins == [('"alex@example.com"', PASSWORD)]
    assert server.connections[0].logged_out


def test_search_returns_newest_first_and_only_examines(make_app: AppFactory, server: FakeImapServer) -> None:
    app = make_app(config())
    data = ok(app.call("mail_search", {"account": "personal", "limit": 2}, Origin.ASSISTANT))
    assert data["matched"] == 3
    assert [message["uid"] for message in data["messages"]] == [3, 2]
    newest, older = data["messages"]
    assert newest["subject"] == "Your order has shipped"
    assert newest["from"] == "Example Store <orders@shop.example.com>"
    assert newest["date"] == "2026-11-09T18:05:00+00:00"
    assert older["flags"] == ["\\Seen"]
    assert data["untrusted_content"] is True
    connection = server.connections[0]
    assert ("EXAMINE", '"INBOX"') in connection.calls
    assert not any(call[0] == "SELECT" for call in connection.calls)


def test_search_filters_on_the_server(make_app: AppFactory, server: FakeImapServer) -> None:
    app = make_app(config())
    data = ok(app.call("mail_search", {"account": "personal", "subject": "Invoice"}, Origin.ASSISTANT))
    assert [message["uid"] for message in data["messages"]] == [2]
    assert server.connections[0].searches == [(("SUBJECT", '"Invoice"'), None)]


def test_read_parses_without_marking_anything_seen(make_app: AppFactory, server: FakeImapServer) -> None:
    app = make_app(config())
    data = ok(app.call("mail_read", {"account": "personal", "uid": 2}, Origin.ASSISTANT))
    assert data["subject"] == "Invoice attached"
    assert data["from"] == "Jordan Example <jordan@example.com>"
    assert data["text"] == "Please find the invoice attached."
    assert data["attachments"] == [
        {"filename": "invoice-1001.pdf", "content_type": "application/pdf", "size": 45}
    ]
    assert (data["truncated"], data["partial"], data["untrusted_content"]) == (False, False, True)
    ok(app.call("mail_read", {"account": "personal", "uid": 1}, Origin.ASSISTANT))
    assert server.mailboxes["INBOX"].messages[1].flags == ()
    for connection in server.connections:
        assert connection.readonly is True
        fetches = [call for call in connection.calls if call[:2] == ("UID", "FETCH")]
        assert fetches
        assert all("BODY.PEEK[" in call[3] for call in fetches)


def test_reading_a_missing_uid_is_a_clean_error(make_app: AppFactory, server: FakeImapServer) -> None:
    app = make_app(config())
    outcome = app.call("mail_read", {"account": "personal", "uid": 99}, Origin.ASSISTANT)
    assert outcome.status == "error"
    assert "no message with UID 99" in outcome.message


def test_draft_threads_a_reply_and_appends_with_the_draft_flag(
    make_app: AppFactory, server: FakeImapServer
) -> None:
    app = make_app(config())
    arguments = {
        "account": "personal",
        "to": ["sam@example.org"],
        "subject": "Re: Quarterly planning notes",
        "body": "Thanks, looks good.",
        "in_reply_to_uid": 1,
    }
    data = ok(app.call("mail_draft", arguments, Origin.ASSISTANT))
    assert (data["status"], data["mailbox"], data["uid"]) == ("saved", "Drafts", 1)
    assert data["reference"] == f"personal:Drafts:{data['message_id']}"
    stored = server.mailboxes["Drafts"].messages[1]
    assert stored.flags == ("\\Draft",)
    draft = email.message_from_bytes(stored.raw, policy=email.policy.default)
    assert draft["In-Reply-To"] == "<plain-1@example.com>"
    assert draft["References"] == "<plain-1@example.com>"
    assert draft["To"] == "sam@example.org"
    assert draft["From"] == "alex@example.com"
    assert draft["Message-ID"] == data["message_id"]
    assert draft.get_content().replace("\r\n", "\n") == "Thanks, looks good.\n"


def test_send_waits_for_approval_then_sends_exactly_that_message(
    make_app: AppFactory, server: FakeImapServer, outbox: FakeSmtp
) -> None:
    app = make_app(config())
    pending = app.call("mail_send", SEND_ARGS, Origin.ASSISTANT)
    assert pending.status == "pending"
    assert pending.approval_id is not None
    assert outbox.sent == []
    request = app.approvals.get(pending.approval_id)
    assert request is not None
    assert request.summary == (
        "send 'Lunch on Friday?' from personal to sam@example.org, jordan@example.com, records@example.com"
    )
    data = ok(app.dispatcher.approve(pending.approval_id))
    assert outbox.connections == [("smtp.fastmail.com", 465, "ssl")]
    assert outbox.password == PASSWORD
    assert len(outbox.sent) == 1
    message, sender, envelope = outbox.sent[0]
    assert sender == "alex@example.com"
    assert envelope == ["sam@example.org", "jordan@example.com", "records@example.com"]
    assert message["To"] == "sam@example.org"
    assert message["Cc"] == "jordan@example.com"
    assert message["Bcc"] is None
    assert message["Subject"] == "Lunch on Friday?"
    assert message.get_content() == "Does noon work?\n"
    assert data["message_id"] == message["Message-ID"]
    assert data["recipients"] == envelope


def test_send_reports_recipients_the_server_refused(
    make_app: AppFactory, server: FakeImapServer, outbox: FakeSmtp
) -> None:
    outbox.refuse = {"jordan@example.com": (550, b"5.1.1 mailbox unavailable")}
    app = make_app(config())
    pending = app.call("mail_send", SEND_ARGS, Origin.ASSISTANT)
    assert pending.approval_id is not None

    data = ok(app.dispatcher.approve(pending.approval_id))

    assert data["status"] == "partially_sent"
    assert data["recipients"] == ["sam@example.org", "records@example.com"]
    assert data["refused"] == ["jordan@example.com"]
    assert len(outbox.sent) == 1


def test_recipients_outside_the_allowlist_are_denied(
    make_app: AppFactory, server: FakeImapServer, outbox: FakeSmtp
) -> None:
    app = make_app(config())
    outcome = app.call("mail_send", {**SEND_ARGS, "bcc": ["someone@elsewhere.invalid"]}, Origin.ASSISTANT)
    assert outcome.status == "denied"
    assert "someone@elsewhere.invalid" in outcome.message
    assert app.approvals.list("pending") == []
    assert outbox.sent == []


def test_send_is_refused_while_sending_is_off(
    make_app: AppFactory, server: FakeImapServer, outbox: FakeSmtp
) -> None:
    app = make_app(config(send_enabled=False))
    outcome = app.call("mail_send", SEND_ARGS, Origin.HUMAN)
    assert outcome.status == "error"
    assert "send_enabled" in outcome.message
    assert outbox.sent == []


def test_services_fill_in_headers_and_use_the_first_account(
    make_app: AppFactory, server: FakeImapServer, outbox: FakeSmtp
) -> None:
    app = make_app(config())
    message = EmailMessage()
    message["To"] = "sam@example.org"
    message["Subject"] = "Daily digest"
    message.set_content("Nothing urgent today.")
    message_id = app.ctx.require("mail.sender").send(message)
    sent, sender, envelope = outbox.sent[0]
    assert (sent["From"], sender, envelope) == ("alex@example.com", "alex@example.com", ["sam@example.org"])
    assert sent["Message-ID"] == message_id

    draft = EmailMessage()
    draft["To"] = "sam@example.org"
    draft["Subject"] = "Draft digest"
    draft.set_content("For review.")
    reference = app.ctx.require("mail.drafts").save_draft(draft, "personal")
    assert reference == f"personal:Drafts:{draft['Message-ID']}"
    assert server.mailboxes["Drafts"].messages[1].flags == ("\\Draft",)
    audit = [record for record in app.ctx.audit.tail(10) if record["kind"].startswith("mail.service_")]
    assert [record["kind"] for record in audit] == ["mail.service_send", "mail.service_draft"]
    assert audit[0]["recipients"] == ["sam@example.org"]
    assert audit[0]["message_id"] == message_id


def test_poll_job_emits_each_new_message_once(make_app: AppFactory, server: FakeImapServer) -> None:
    app = make_app(config())
    scheduler = app.ctx.scheduler
    scheduler.run_all()
    assert app.ctx.events.pending() == []
    server.mailboxes["INBOX"].add(simple_message(4))
    scheduler.run_all()
    scheduler.run_all()
    events = app.ctx.events.pending()
    assert [(event.type, event.payload["uid"], event.payload["subject"]) for event in events] == [
        ("mail.received", 4, "Message 4")
    ]
    app.ctx.db.kv_set("mail", "personal:INBOX", json.dumps({"uidvalidity": 11, "last_uid": 3}))
    scheduler.run_all()
    assert len(app.ctx.events.pending()) == 1
    assert poll_job(app).last_error is None


def test_one_failing_account_does_not_stop_the_others(
    make_app: AppFactory, server: FakeImapServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = config()
    data["connectors"]["mail"]["accounts"]["broken"] = {
        "provider": "custom",
        "address": "alex@example.org",
        "imap_host": "imap.broken.invalid",
        "password": f"env:{PASSWORD_ENV}",
        "watch": ["INBOX"],
    }

    def connect(host: str, port: int, *, timeout: float) -> Any:
        if host == "imap.broken.invalid":
            raise OSError("connection refused")
        return server.connect(host, port, timeout=timeout)

    monkeypatch.setattr(mail_imap, "connect_imap", connect)
    app = make_app(data)
    app.ctx.scheduler.run_all()
    server.mailboxes["INBOX"].add(simple_message(4))
    app.ctx.scheduler.run_all()
    assert [event.payload["account"] for event in app.ctx.events.pending()] == ["personal"]
    assert poll_job(app).last_error == "MailPollError"


def test_oauth_account_refreshes_its_token_and_signs_in_with_xoauth2(
    make_app: AppFactory, server: FakeImapServer, fake_http: FakeHttp
) -> None:
    app = make_app(config(provider="gmail", auth="oauth2", client_id="client.apps.example", password=None))
    store = app.ctx.require("mail.sender").tokens
    store.save("personal", TokenSet("old-access", "refresh-1", time.time() - 10, "https://mail.google.com/"))
    fake_http.add(
        "POST",
        "https://oauth2.googleapis.com/token",
        body=json.dumps({"access_token": "new-access", "expires_in": 3600}),
    )
    ok(app.call("mail_search", {"account": "personal"}, Origin.ASSISTANT))
    assert server.addresses[-1][:2] == ("imap.gmail.com", 993)
    assert server.connections[-1].authentications == [
        ("XOAUTH2", b"user=alex@example.com\x01auth=Bearer new-access\x01\x01")
    ]
    saved = store.load("personal")
    assert saved is not None
    assert (saved.access_token, saved.refresh_token) == ("new-access", "refresh-1")


def test_doctor_reports_unresolved_secrets_and_missing_sign_in(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    data = config()
    data["connectors"]["mail"]["accounts"]["hotmail"] = {
        "provider": "outlook",
        "address": "alex@example.org",
        "auth": "oauth2",
        "client_id": "00000000-0000-0000-0000-000000000000",
    }
    app = make_app(data)
    problems = mail.doctor(app.ctx)
    assert any(problem.startswith("account personal: password does not resolve") for problem in problems)
    assert any("oauth-login hotmail" in problem for problem in problems)
    assert not any(PASSWORD in problem for problem in problems)


def poll_job(app: App) -> Any:
    return next(job for job in app.ctx.scheduler.jobs() if job.name == "mail.poll")

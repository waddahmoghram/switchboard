"""Configuration parsing: presets, validation, and problems that never echo secret values."""

from __future__ import annotations

from typing import Any

from switchboard.connectors.mail.accounts import OAuthSettings, parse_settings


def settings_for(**account: Any) -> Any:
    return parse_settings({"accounts": {"personal": account}})


def test_gmail_preset_and_defaults() -> None:
    settings = settings_for(provider="gmail", address="alex@example.com", password="env:MAIL_APP_PASSWORD")
    account = settings.accounts["personal"]
    assert (account.imap_host, account.imap_port) == ("imap.gmail.com", 993)
    assert (account.smtp_host, account.smtp_port, account.smtp_security) == ("smtp.gmail.com", 465, "ssl")
    assert account.imap_username == account.smtp_username == "alex@example.com"
    assert account.auth == "password"
    assert account.send_enabled is False
    assert account.watch == ()
    assert (settings.poll_interval_seconds, settings.max_body_chars) == (300, 20_000)
    assert settings.problems == []


def test_outlook_needs_oauth2() -> None:
    settings = settings_for(provider="outlook", address="alex@example.org", password="env:MAIL_APP_PASSWORD")
    assert "personal" in settings.broken
    assert any('auth = "oauth2"' in problem for problem in settings.problems)


def test_outlook_oauth_uses_the_consumers_tenant_and_starttls() -> None:
    settings = settings_for(
        provider="outlook",
        address="alex@example.org",
        auth="oauth2",
        client_id="00000000-0000-0000-0000-000000000000",
    )
    account = settings.accounts["personal"]
    assert account.oauth == OAuthSettings("00000000-0000-0000-0000-000000000000", None, "consumers")
    assert (account.smtp_host, account.smtp_port, account.smtp_security) == (
        "smtp-mail.outlook.com",
        587,
        "starttls",
    )


def test_icloud_and_fastmail_presets() -> None:
    icloud = settings_for(provider="icloud", address="alex@example.com", password="env:X").accounts[
        "personal"
    ]
    assert (icloud.imap_host, icloud.smtp_host, icloud.smtp_port, icloud.smtp_security) == (
        "imap.mail.me.com",
        "smtp.mail.me.com",
        587,
        "starttls",
    )
    fastmail = settings_for(provider="fastmail", address="alex@example.com", password="env:X").accounts[
        "personal"
    ]
    assert (fastmail.imap_host, fastmail.smtp_host, fastmail.smtp_port) == (
        "imap.fastmail.com",
        "smtp.fastmail.com",
        465,
    )


def test_overridden_submission_port_switches_to_starttls() -> None:
    settings = settings_for(provider="fastmail", address="alex@example.com", password="env:X", smtp_port=587)
    assert settings.accounts["personal"].smtp_security == "starttls"


def test_separate_usernames() -> None:
    account = settings_for(
        provider="icloud",
        address="alex@example.com",
        password="env:X",
        username="alex",
        smtp_username="alex@example.com",
    ).accounts["personal"]
    assert (account.imap_username, account.smtp_username) == ("alex", "alex@example.com")


def test_problems_name_keys_but_never_echo_values() -> None:
    settings = settings_for(provider="custom", address="alex@example.com", password="hunter2-not-a-reference")
    assert "personal" in settings.broken
    text = " ".join(settings.problems)
    assert "password must be a secret reference" in text
    assert "imap_host is required" in text
    assert "hunter2" not in text


def test_trailing_newline_in_an_address_is_rejected() -> None:
    settings = settings_for(provider="gmail", address="alex@example.com\n", password="env:X")
    assert "personal" in settings.broken


def test_send_enabled_needs_an_smtp_host() -> None:
    settings = settings_for(
        provider="custom",
        address="alex@example.com",
        password="env:X",
        imap_host="imap.example.com",
        send_enabled=True,
    )
    assert any("smtp_host" in problem for problem in settings.problems)


def test_bad_names_unknown_keys_and_bad_numbers() -> None:
    settings = parse_settings(
        {
            "poll_interval_seconds": 10,
            "max_body_chars": "lots",
            "accounts": {
                "bad name": {"provider": "gmail"},
                "ok": {
                    "provider": "icloud",
                    "address": "alex@example.com",
                    "password": "env:X",
                    "send_enable": True,
                },
            },
        }
    )
    assert (settings.poll_interval_seconds, settings.max_body_chars) == (300, 20_000)
    assert "bad name" in settings.broken
    assert "ok" in settings.accounts
    assert any("send_enable" in problem for problem in settings.problems)
    assert sum("using" in problem for problem in settings.problems) == 2


def test_watch_must_be_a_list_of_mailbox_names() -> None:
    assert (
        "personal"
        in settings_for(provider="gmail", address="alex@example.com", password="env:X", watch="INBOX").broken
    )
    account = settings_for(
        provider="gmail", address="alex@example.com", password="env:X", watch=["INBOX", "Receipts", "INBOX"]
    ).accounts["personal"]
    assert account.watch == ("INBOX", "Receipts")

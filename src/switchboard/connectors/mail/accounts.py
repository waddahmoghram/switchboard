"""Read ``[connectors.mail]`` into typed settings without touching the network.

Parsing never raises: a broken account is listed by ``switchboard doctor`` and refused by the tools,
while the other accounts keep working. Secret *references* are checked for shape only; doctor resolves
them, and the tools resolve them lazily when they connect. Problems name keys, never values, so a
password pasted into the wrong place is not echoed back.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.connectors.mail.providers import PRESETS, ProviderPreset, smtp_security_for_port
from switchboard.secrets import is_reference

# Strict ASCII addresses without display names. ``(?!\n)$`` stops Python's ``$`` from accepting a
# trailing newline; the same pattern is used in tool schemas, which the core checks with re.search.
ADDRESS_PATTERN = r"^[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+(?!\n)$"
ADDRESS = re.compile(ADDRESS_PATTERN)
ACCOUNT_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}(?!\n)$"
ACCOUNT_NAME = re.compile(ACCOUNT_NAME_PATTERN)
HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
TENANT = re.compile(r"[A-Za-z0-9.-]{1,64}")
CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,200}")

DEFAULT_POLL_SECONDS = 300
MIN_POLL_SECONDS = 60
DEFAULT_MAX_BODY_CHARS = 20_000
MAX_MAILBOX_NAME = 200
MAX_WATCHED = 20

_SETTING_KEYS = frozenset({"poll_interval_seconds", "max_body_chars", "accounts"})
_ACCOUNT_KEYS = frozenset(
    {
        "provider",
        "address",
        "username",
        "smtp_username",
        "auth",
        "password",
        "client_id",
        "client_secret",
        "tenant",
        "imap_host",
        "imap_port",
        "smtp_host",
        "smtp_port",
        "smtp_security",
        "watch",
        "send_enabled",
        "drafts_mailbox",
    }
)


@dataclass(frozen=True)
class OAuthSettings:
    """The owner's own OAuth client. ``client_secret`` is a secret reference, never the value."""

    client_id: str
    client_secret: str | None
    tenant: str


@dataclass(frozen=True)
class Account:
    """One configured mailbox. ``password`` is a secret reference, never the value."""

    name: str
    provider: str
    address: str
    imap_username: str
    smtp_username: str
    auth: str
    password: str | None
    oauth: OAuthSettings | None
    imap_host: str
    imap_port: int
    smtp_host: str | None
    smtp_port: int
    smtp_security: str
    watch: tuple[str, ...]
    send_enabled: bool
    drafts_mailbox: str | None

    def describe(self) -> dict[str, Any]:
        """What ``mail_accounts`` shows: identity and capabilities, never credentials."""
        return {
            "name": self.name,
            "address": self.address,
            "provider": self.provider,
            "auth": self.auth,
            "send_enabled": self.send_enabled,
            "watch": list(self.watch),
        }


@dataclass(frozen=True)
class MailSettings:
    poll_interval_seconds: int
    max_body_chars: int
    accounts: dict[str, Account]
    broken: dict[str, list[str]]
    problems: list[str]

    def first_account(self) -> Account | None:
        """The first usable account in configuration order (the services' default)."""
        return next(iter(self.accounts.values()), None)


def mailbox_name_ok(value: object) -> bool:
    """Mailbox names are non-empty, bounded, and free of CR, LF, and NUL."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_MAILBOX_NAME
        and not any(character in value for character in "\r\n\x00")
    )


def parse_settings(raw: Mapping[str, Any]) -> MailSettings:
    problems: list[str] = [
        f"unknown setting {key!r} in [connectors.mail] is ignored"
        for key in sorted(set(map(str, raw)) - _SETTING_KEYS)
    ]
    poll = _bounded_int(
        raw, "poll_interval_seconds", DEFAULT_POLL_SECONDS, MIN_POLL_SECONDS, 86_400, problems
    )
    max_chars = _bounded_int(raw, "max_body_chars", DEFAULT_MAX_BODY_CHARS, 500, 200_000, problems)
    tables = raw.get("accounts", {})
    if not isinstance(tables, Mapping):
        problems.append("[connectors.mail.accounts] must be a table of account tables")
        tables = {}
    accounts: dict[str, Account] = {}
    broken: dict[str, list[str]] = {}
    for key, table in tables.items():
        name = str(key)
        account, issues = parse_account(name, table)
        problems.extend(f"account {name}: {issue}" for issue in issues)
        if account is None:
            broken[name] = issues
        else:
            accounts[name] = account
    return MailSettings(poll, max_chars, accounts, broken, problems)


def parse_account(name: str, table: object) -> tuple[Account | None, list[str]]:
    """Validate one ``[connectors.mail.accounts.<name>]`` table; None means the account is unusable."""
    if not ACCOUNT_NAME.match(name):
        return None, ["account names may use only letters, digits, '-' and '_' (at most 64 characters)"]
    if not isinstance(table, Mapping):
        return None, ["the account must be a table"]
    read = _Reader(table)
    notes = [f"unknown key {key!r} is ignored" for key in sorted(map(str, table)) if key not in _ACCOUNT_KEYS]
    preset = _preset(read)
    address = read.text("address", required=True, pattern=ADDRESS)
    auth = read.choice("auth", ("password", "oauth2"), "password")
    if preset is not None and auth not in preset.auth_methods:
        read.fail(_auth_problem(preset))
    password = read.reference("password")
    if auth == "password" and password is None:
        read.fail('auth = "password" needs password = a secret reference to an app password')
    oauth = _oauth_settings(read) if auth == "oauth2" else None
    imap_host = read.text("imap_host", pattern=HOST) or (preset.imap_host if preset else None)
    smtp_host = read.text("smtp_host", pattern=HOST) or (preset.smtp_host if preset else None)
    imap_port = read.port("imap_port", preset.imap_port if preset else 993)
    smtp_port = read.port("smtp_port", preset.smtp_port if preset else 465)
    smtp_security = read.choice("smtp_security", ("ssl", "starttls"), smtp_security_for_port(smtp_port))
    send_enabled = read.flag("send_enabled")
    if preset is not None and imap_host is None:
        read.fail(f"imap_host is required for provider {preset.name!r}")
    if send_enabled and preset is not None and smtp_host is None:
        read.fail(f"send_enabled needs smtp_host for provider {preset.name!r}")
    username = read.text("username", max_length=320)
    smtp_username = read.text("smtp_username", max_length=320)
    watch = read.mailboxes("watch")
    drafts_mailbox = read.mailbox("drafts_mailbox")
    if read.problems or preset is None or address is None or imap_host is None:
        return None, read.problems + notes
    account = Account(
        name=name,
        provider=preset.name,
        address=address,
        imap_username=username or address,
        smtp_username=smtp_username or username or address,
        auth=auth,
        password=password,
        oauth=oauth,
        imap_host=imap_host,
        imap_port=imap_port,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_security=smtp_security,
        watch=watch,
        send_enabled=send_enabled,
        drafts_mailbox=drafts_mailbox,
    )
    return account, notes


def _bounded_int(
    raw: Mapping[str, Any], key: str, default: int, low: int, high: int, problems: list[str]
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        problems.append(f"{key} must be a whole number from {low} to {high}; using {default}")
        return default
    return value


def _preset(read: _Reader) -> ProviderPreset | None:
    name = read.text("provider", required=True, max_length=32)
    if name is None:
        return None
    preset = PRESETS.get(name)
    if preset is None:
        read.fail(f"provider must be one of: {', '.join(PRESETS)}")
    return preset


def _auth_problem(preset: ProviderPreset) -> str:
    if preset.name == "outlook":
        return (
            'Outlook.com accepts only auth = "oauth2": Microsoft ended basic authentication for '
            "Outlook.com on September 16, 2024"
        )
    return f'provider {preset.name!r} supports only auth = "password" (an app password) in this version'


def _oauth_settings(read: _Reader) -> OAuthSettings | None:
    client_id = read.text("client_id", required=True, pattern=CLIENT_ID, max_length=200)
    client_secret = read.reference("client_secret")
    tenant = read.text("tenant", pattern=TENANT, max_length=64) or "consumers"
    return None if client_id is None else OAuthSettings(client_id, client_secret, tenant)


class _Reader:
    """Typed reads from one account table that record problems instead of raising."""

    def __init__(self, table: Mapping[str, Any]) -> None:
        self.table = table
        self.problems: list[str] = []

    def fail(self, problem: str) -> None:
        self.problems.append(problem)

    def text(
        self,
        key: str,
        *,
        required: bool = False,
        pattern: re.Pattern[str] | None = None,
        max_length: int = 254,
    ) -> str | None:
        value = self.table.get(key)
        if value is None:
            if required:
                self.fail(f"{key} is required")
            return None
        if not isinstance(value, str) or not 0 < len(value) <= max_length:
            self.fail(f"{key} must be a non-empty string of at most {max_length} characters")
            return None
        if pattern is not None and not pattern.fullmatch(value):
            self.fail(f"{key} is not in the expected format")
            return None
        return value

    def choice(self, key: str, options: tuple[str, ...], default: str) -> str:
        value = self.table.get(key, default)
        if value not in options:
            self.fail(f"{key} must be one of: {', '.join(options)}")
            return default
        return str(value)

    def port(self, key: str, default: int) -> int:
        value = self.table.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            self.fail(f"{key} must be a port number from 1 to 65535")
            return default
        return value

    def flag(self, key: str) -> bool:
        value = self.table.get(key, False)
        if not isinstance(value, bool):
            self.fail(f"{key} must be true or false")
            return False
        return value

    def reference(self, key: str) -> str | None:
        value = self.table.get(key)
        if value is None:
            return None
        if not is_reference(value):
            self.fail(f"{key} must be a secret reference such as env:NAME, file:/path, or cmd:program")
            return None
        return str(value)

    def mailbox(self, key: str) -> str | None:
        value = self.table.get(key)
        if value is None:
            return None
        if not mailbox_name_ok(value):
            self.fail(f"{key} must be a mailbox name of at most {MAX_MAILBOX_NAME} characters")
            return None
        return str(value)

    def mailboxes(self, key: str) -> tuple[str, ...]:
        value = self.table.get(key, [])
        if not isinstance(value, list) or len(value) > MAX_WATCHED or not all(map(mailbox_name_ok, value)):
            self.fail(f"{key} must be a list of at most {MAX_WATCHED} mailbox names")
            return ()
        return tuple(dict.fromkeys(str(item) for item in value))

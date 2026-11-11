"""The mail connector's runtime: accounts, credentials, connections, services, and the poll job."""

from __future__ import annotations

import contextlib
import imaplib
import logging
import smtplib
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.message import EmailMessage

from switchboard.connectors.mail import imap, smtp
from switchboard.connectors.mail.accounts import Account, MailSettings
from switchboard.connectors.mail.compose import (
    choose_drafts_mailbox,
    complete_headers,
    envelope_recipients,
    to_bytes,
)
from switchboard.connectors.mail.oauth import (
    GOOGLE,
    OAuthEndpoints,
    OAuthError,
    TokenStore,
    microsoft,
    refresh_tokens,
)
from switchboard.connectors.mail.poll import poll_mailbox
from switchboard.connectors.mail.providers import PRESETS
from switchboard.context import AppContext
from switchboard.errors import SwitchboardError, ToolError

NAME = "mail"
TIMEOUT_SECONDS = 30.0

log = logging.getLogger(__name__)


class MailPollError(SwitchboardError):
    """At least one watched mailbox could not be polled; the others were still processed."""


def describe_error(exc: BaseException) -> str:
    """Exception type plus a short message. Protocol errors carry server text, never credentials."""
    text = " ".join(str(exc).split())[:200]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


@contextlib.contextmanager
def protocol_errors(account: str) -> Iterator[None]:
    """Report network, protocol, and credential failures as :class:`ToolError` for the dispatcher."""
    try:
        yield
    except ToolError:
        raise
    except (imaplib.IMAP4.error, smtplib.SMTPException, OSError, SwitchboardError) as exc:
        raise ToolError(f"{account}: {describe_error(exc)}") from exc


def endpoints_for(account: Account) -> OAuthEndpoints:
    flow = PRESETS[account.provider].oauth
    if flow == "google":
        return GOOGLE
    if flow == "microsoft" and account.oauth is not None:
        return microsoft(account.oauth.tenant)
    raise ToolError(f"provider {account.provider!r} has no OAuth sign-in in this version")


def draft_reference(account: str, mailbox: str, message_id: str) -> str:
    return f"{account}:{mailbox}:{message_id}"


def store_draft(
    session: imap.ImapSession, account: Account, message: EmailMessage, now: datetime
) -> tuple[str, int | None]:
    """APPEND ``message`` to the Drafts mailbox with the ``\\Draft`` flag; returns the mailbox and UID."""
    mailboxes = [] if account.drafts_mailbox else session.list_mailboxes()
    mailbox = choose_drafts_mailbox(mailboxes, account.drafts_mailbox)
    return mailbox, session.append(mailbox, to_bytes(message), flags="\\Draft", when=now)


class MailService:
    """Backs the tools and is provided as both ``mail.drafts`` and ``mail.sender``.

    ``clock`` and ``timeout`` are attributes so tests can pin time and keep failures fast.
    """

    def __init__(self, ctx: AppContext, settings: MailSettings) -> None:
        self.ctx = ctx
        self.settings = settings
        self.tokens = TokenStore(ctx.data_dir)
        self.clock: Callable[[], float] = time.time
        self.timeout = TIMEOUT_SECONDS

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def account(self, name: str | None) -> Account:
        """The named account, or the first usable one in configuration order when ``name`` is None."""
        if name is None:
            first = self.settings.first_account()
            if first is None:
                raise ToolError("no usable mail account is configured; run 'switchboard doctor'")
            return first
        account = self.settings.accounts.get(name)
        if account is not None:
            return account
        if name in self.settings.broken:
            raise ToolError(f"mail account {name!r} is misconfigured; run 'switchboard doctor'")
        known = ", ".join(self.settings.accounts) or "none"
        raise ToolError(f"unknown mail account {name!r}; configured: {known}")

    def credentials(self, account: Account, protocol: str) -> imap.Credentials:
        """Resolve the secret now, at connection time, never earlier."""
        username = account.imap_username if protocol == "imap" else account.smtp_username
        if account.auth == "oauth2":
            return imap.Credentials(username, access_token=self.access_token(account))
        if account.password is None:
            raise ToolError(f"account {account.name} has no password reference")
        return imap.Credentials(username, password=self.ctx.secret(account.password))

    def access_token(self, account: Account) -> str:
        """A valid access token, refreshed and saved when it is about to expire."""
        if account.oauth is None:
            raise ToolError(f"account {account.name} has no OAuth client settings")
        stored = self.tokens.load(account.name)
        if stored is None:
            raise OAuthError(f"not signed in yet; run 'switchboard mail oauth-login {account.name}'")
        now = self.clock()
        if not stored.needs_refresh(now):
            return stored.access_token
        secret = self.ctx.secret(account.oauth.client_secret) if account.oauth.client_secret else None
        fresh = refresh_tokens(
            self.ctx.http, endpoints_for(account), account.oauth.client_id, secret, stored, now=now
        )
        self.tokens.save(account.name, fresh)
        return fresh.access_token

    @contextlib.contextmanager
    def imap_session(self, account: Account) -> Iterator[imap.ImapSession]:
        credentials = self.credentials(account, "imap")
        with imap.open_session(
            account.imap_host, account.imap_port, credentials, timeout=self.timeout
        ) as session:
            yield session

    def deliver(self, account: Account, message: EmailMessage, recipients: list[str]) -> list[str]:
        """Send through the account's SMTP server; returns refused recipients. Needs ``send_enabled``."""
        if not account.send_enabled:
            raise ToolError(f"sending is turned off for account {account.name}; set send_enabled = true")
        if account.smtp_host is None:
            raise ToolError(f"account {account.name} has no smtp_host")
        return smtp.send(
            account.smtp_host,
            account.smtp_port,
            account.smtp_security,
            self.credentials(account, "smtp"),
            message,
            sender=account.address,
            recipients=recipients,
            timeout=self.timeout,
        )

    # ---- Services for other connectors (docs/CONNECTOR_GUIDE.md) ----

    def save_draft(self, message: EmailMessage, account: str | None = None) -> str:
        """Store ``message`` as a draft; returns ``<account>:<mailbox>:<Message-ID>``."""
        target = self.account(account)
        now = self.now()
        complete_headers(message, sender=target.address, now=now)
        with protocol_errors(target.name), self.imap_session(target) as session:
            mailbox, _uid = store_draft(session, target, message, now)
        message_id = str(message["Message-ID"])
        self.ctx.audit.append(
            "mail.service_draft", account=target.name, mailbox=mailbox, message_id=message_id
        )
        return draft_reference(target.name, mailbox, message_id)

    def send(self, message: EmailMessage, account: str | None = None) -> str:
        """Send ``message`` to its To, Cc, and Bcc addresses; returns the Message-ID.

        Callers are other connectors, so the policy gate does not apply here; ``send_enabled`` does, and
        every send is written to the audit log because the dispatcher does not see it.
        """
        target = self.account(account)
        complete_headers(message, sender=target.address, now=self.now())
        recipients = envelope_recipients(message)
        with protocol_errors(target.name):
            refused = self.deliver(target, message, recipients)
        message_id = str(message["Message-ID"])
        self.ctx.audit.append(
            "mail.service_send",
            account=target.name,
            recipients=[address for address in recipients if address not in refused],
            refused=refused,
            subject=str(message.get("Subject", "")),
            message_id=message_id,
        )
        return message_id

    # ---- The mail.poll job ----

    def poll(self) -> None:
        """Poll every watched mailbox. A failing account or mailbox is logged and skipped, then reported."""
        failures: list[str] = []
        for account in self.settings.accounts.values():
            if account.watch:
                failures.extend(self._poll_account(account))
        if failures:
            raise MailPollError("mail polling failed for " + ", ".join(failures))

    def _poll_account(self, account: Account) -> list[str]:
        failed: list[str] = []
        try:
            with self.imap_session(account) as session:
                for mailbox in account.watch:
                    try:
                        poll_mailbox(
                            session,
                            self.ctx.db,
                            self.ctx.events,
                            account=account.name,
                            mailbox=mailbox,
                            max_chars=self.settings.max_body_chars,
                        )
                    except Exception as exc:
                        log.warning(
                            "mail poll of %s/%s failed: %s", account.name, mailbox, describe_error(exc)
                        )
                        failed.append(f"{account.name}/{mailbox}")
        except Exception as exc:
            log.warning("mail poll of %s failed: %s", account.name, describe_error(exc))
            failed.append(account.name)
        return failed

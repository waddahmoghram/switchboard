"""``switchboard mail …`` commands the owner runs: OAuth sign-in and a connection check."""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from typing import TYPE_CHECKING

from switchboard.connectors.mail.compose import choose_drafts_mailbox
from switchboard.connectors.mail.oauth import TokenSet, google_login, poll_device_token, request_device_code
from switchboard.connectors.mail.service import MailService, endpoints_for, protocol_errors
from switchboard.errors import ConfigError, ToolError

if TYPE_CHECKING:
    from switchboard.app import App


def add_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    mail = subparsers.add_parser("mail", help="mail connector: OAuth sign-in and connection check")
    actions = mail.add_subparsers(dest="mail_command", metavar="<action>", required=True)
    login = actions.add_parser("oauth-login", help="sign in with OAuth2 and store a refresh token")
    login.add_argument("account")
    login.add_argument(
        "--no-browser", action="store_true", help="print the sign-in URL instead of opening a browser"
    )
    login.set_defaults(app_handler=cmd_oauth_login)
    check = actions.add_parser("check", help="connect over IMAP and print how many mailboxes there are")
    check.add_argument("account")
    check.set_defaults(app_handler=cmd_check)


def _service(app: App) -> MailService:
    service = app.ctx.service("mail.sender")
    if not isinstance(service, MailService):
        raise ConfigError("the mail connector is not enabled")
    return service


def cmd_oauth_login(args: argparse.Namespace, app: App) -> int:
    """Run the provider's sign-in flow and store the tokens; prints no token values."""
    service = _service(app)
    account = service.account(args.account)
    if account.oauth is None:
        print(
            f'account {account.name} uses auth = "{account.auth}"; set auth = "oauth2" first', file=sys.stderr
        )
        return 1
    endpoints = endpoints_for(account)
    client_id = account.oauth.client_id
    tokens: TokenSet
    if endpoints.device_url is not None:
        device = request_device_code(app.ctx.http, endpoints, client_id)
        print(device.message, flush=True)
        tokens = poll_device_token(
            app.ctx.http, endpoints, client_id, device, sleep=time.sleep, clock=time.time
        )
    else:
        secret = app.ctx.secret(account.oauth.client_secret) if account.oauth.client_secret else None
        tokens = google_login(
            app.ctx.http,
            client_id,
            secret,
            login_hint=account.address,
            open_browser=(lambda _url: False) if args.no_browser else webbrowser.open,
            announce=lambda text: print(text, flush=True),
        )
    path = service.tokens.save(account.name, tokens)
    print(f"signed in as {account.address}; tokens saved to {path} (mode 600)")
    if not tokens.refresh_token:
        print(
            "warning: no refresh token was issued, so you must sign in again within the hour", file=sys.stderr
        )
    return 0


def cmd_check(args: argparse.Namespace, app: App) -> int:
    """Sign in over IMAP, list mailboxes, and report the count and the Drafts mailbox."""
    service = _service(app)
    account = service.account(args.account)
    with protocol_errors(account.name), service.imap_session(account) as session:
        mailboxes = session.list_mailboxes()
    try:
        drafts = choose_drafts_mailbox(mailboxes, account.drafts_mailbox)
    except ToolError:
        drafts = "not found; set drafts_mailbox"
    print(f"{account.name}: signed in to {account.imap_host}:{account.imap_port}; {len(mailboxes)} mailboxes")
    print(f"drafts mailbox: {drafts}")
    return 0

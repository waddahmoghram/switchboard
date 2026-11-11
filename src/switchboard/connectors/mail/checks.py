"""``switchboard doctor`` checks for the mail connector. Nothing here connects to a mail server."""

from __future__ import annotations

from switchboard.connectors.mail.accounts import Account, parse_settings
from switchboard.connectors.mail.oauth import OAuthError, TokenStore
from switchboard.connectors.mail.service import NAME
from switchboard.context import AppContext
from switchboard.errors import SwitchboardError


def doctor(ctx: AppContext) -> list[str]:
    """Configuration problems, unresolvable secret references, and missing OAuth tokens (never values)."""
    settings = parse_settings(ctx.connector_config(NAME))
    problems = list(settings.problems)
    if not settings.accounts and not settings.broken:
        problems.append("no accounts are configured under [connectors.mail.accounts]")
    store = TokenStore(ctx.data_dir)
    for account in settings.accounts.values():
        problems.extend(
            f"account {account.name}: {problem}" for problem in account_problems(ctx, account, store)
        )
    return problems


def account_problems(ctx: AppContext, account: Account, store: TokenStore) -> list[str]:
    problems: list[str] = []
    references = [("password", account.password)]
    if account.oauth is not None:
        references.append(("client_secret", account.oauth.client_secret))
    for key, reference in references:
        if reference is None:
            continue
        try:
            ctx.secret(reference)
        except SwitchboardError as exc:
            problems.append(f"{key} does not resolve: {exc}")
    if account.auth == "oauth2":
        problems.extend(_token_problems(account, store))
    return problems


def _token_problems(account: Account, store: TokenStore) -> list[str]:
    try:
        tokens = store.load(account.name)
    except OAuthError as exc:
        return [str(exc)]
    if tokens is None:
        return [f"not signed in yet; run 'switchboard mail oauth-login {account.name}'"]
    if not tokens.refresh_token:
        return [f"the stored token has no refresh token; run 'switchboard mail oauth-login {account.name}'"]
    return []

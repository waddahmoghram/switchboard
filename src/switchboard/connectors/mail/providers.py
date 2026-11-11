"""Server presets for well-known mail providers.

Every value was checked against the provider's own documentation on 2026-11-11 (links are in
``docs/connectors/mail.md``). Settings in an account table override the preset.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    """Default servers and the sign-in methods a provider accepts.

    ``oauth`` names the sign-in flow that ``switchboard mail oauth-login`` runs: ``"google"`` (installed
    app with a loopback redirect) or ``"microsoft"`` (device authorization grant).
    """

    name: str
    imap_host: str | None
    imap_port: int
    smtp_host: str | None
    smtp_port: int
    auth_methods: tuple[str, ...]
    oauth: str | None


PRESETS: dict[str, ProviderPreset] = {
    "gmail": ProviderPreset(
        "gmail", "imap.gmail.com", 993, "smtp.gmail.com", 465, ("password", "oauth2"), "google"
    ),
    "outlook": ProviderPreset(
        "outlook", "outlook.office365.com", 993, "smtp-mail.outlook.com", 587, ("oauth2",), "microsoft"
    ),
    "icloud": ProviderPreset("icloud", "imap.mail.me.com", 993, "smtp.mail.me.com", 587, ("password",), None),
    "fastmail": ProviderPreset(
        "fastmail", "imap.fastmail.com", 993, "smtp.fastmail.com", 465, ("password",), None
    ),
    "custom": ProviderPreset("custom", None, 993, None, 465, ("password",), None),
}


def smtp_security_for_port(port: int) -> str:
    """Port 465 is implicit TLS (RFC 8314); other submission ports (587, 25, …) upgrade with STARTTLS."""
    return "ssl" if port == 465 else "starttls"

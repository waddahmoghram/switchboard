"""The ``notify`` service: deliver one short note to every configured channel, or to a chosen few.

Other connectors look it up with ``ctx.service("notify")`` and call
``send(title, message, *, priority="default", url=None)``; it returns the names of the channels that
accepted the note. One failing channel never stops the others. Channels whose outcome is unknown are
logged and left out of that list; they are never retried automatically.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from switchboard import execution
from switchboard.connectors.notify.channels import Channel, ChannelError, ChannelUncertainError, Notification
from switchboard.context import AppContext
from switchboard.util import truncate

log = logging.getLogger(__name__)

PRIORITIES = ("min", "low", "default", "high", "urgent")
MAX_TITLE = 120
MAX_MESSAGE = 4000
_HTTPS_URL = re.compile(r"https://[!-~]{1,2040}")


def is_https_url(value: object) -> bool:
    """True for one https:// address of printable ASCII without spaces (safe in a header or a chat)."""
    return isinstance(value, str) and _HTTPS_URL.fullmatch(value) is not None


def one_line(text: str) -> str:
    """Turn control characters into spaces and collapse runs of whitespace."""
    visible = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)
    return " ".join(visible.split())


def make_notification(
    title: str, message: str, *, priority: str = "default", url: str | None = None
) -> Notification:
    """Normalize caller input. Unknown priorities become ``default``; links that are not https are dropped."""
    if priority not in PRIORITIES:
        log.warning("notify: unknown priority %r; using default", str(priority)[:20])
        priority = "default"
    if url is not None and not is_https_url(url):
        log.warning("notify: dropped a link that is not a plain https:// address")
        url = None
    clean_title = truncate(one_line(_valid_unicode(str(title))), MAX_TITLE) or "Switchboard"
    clean_message = truncate(_valid_unicode(str(message)), MAX_MESSAGE)
    return Notification(title=clean_title, message=clean_message, priority=priority, url=url)


@dataclass
class DeliveryReport:
    """Which channels accepted a note, which refused it, and which may have delivered it anyway.

    ``uncertain`` and ``effect_unknown`` appear in :meth:`to_dict` only when some outcome is unknown, so
    the action journal records the call as uncertain and an identical repeat waits for the operator.
    """

    sent: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    uncertain: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        report: dict[str, Any] = {"sent": list(self.sent), "failed": dict(self.failed)}
        if self.uncertain:
            report["uncertain"] = dict(self.uncertain)
            report["effect_unknown"] = True
        return report


class NotifyService:
    """Provided as the ``notify`` service; also backs the ``notify_self`` tool."""

    def __init__(self, ctx: AppContext, channels: Mapping[str, Channel]) -> None:
        self._ctx = ctx
        self.channels: dict[str, Channel] = dict(channels)

    def send(
        self, title: str, message: str, *, priority: str = "default", url: str | None = None
    ) -> list[str]:
        """Deliver to every channel; return the names of those that accepted the note."""
        note = make_notification(title, message, priority=priority, url=url)
        return self.deliver(note).sent

    def deliver(self, note: Notification, names: Sequence[str] | None = None) -> DeliveryReport:
        """Deliver to ``names`` in that order (default: every channel, in configuration order).

        Each channel's failure is recorded in the report; it never stops the remaining channels. Once the
        current execution deadline has passed, the remaining channels are not attempted.
        """
        chosen = list(self.channels) if names is None else list(dict.fromkeys(names))
        report = DeliveryReport()
        for name in chosen:
            channel = self.channels.get(name)
            if channel is None:
                report.failed[name] = "no such channel"
                continue
            limit = execution.current()
            if limit is not None and limit.expired():
                report.failed[name] = f"not attempted: the {limit.label} deadline passed"
                continue
            try:
                channel.send(note, self._ctx)
            except ChannelUncertainError as exc:
                report.uncertain[name] = str(exc)
            except ChannelError as exc:
                report.failed[name] = str(exc)
            except Exception as exc:
                # Only the type: an unexpected error's text could quote a URL that carries a secret. It may
                # have happened after the note was handed over, so the outcome is unknown.
                report.uncertain[name] = f"internal error ({type(exc).__name__})"
            else:
                report.sent.append(name)
        for name, reason in report.failed.items():
            log.warning("notify channel %s failed: %s", name, reason)
        for name, reason in report.uncertain.items():
            log.warning("notify channel %s outcome unknown: %s", name, reason)
        return report


def _valid_unicode(text: str) -> str:
    """Replace lone surrogates (possible in decoded JSON) so every channel can encode the text."""
    return text.encode("utf-8", errors="replace").decode("utf-8")

"""robots.txt and Retry-After handling for polite page fetches.

robots.txt follows RFC 9309. Rules are evaluated here rather than by :mod:`urllib.robotparser`, whose
answers differ between Python versions (before 3.14 it used the first matching rule and read ``*`` and
``$`` literally):

* 2xx: obey the groups for Switchboard's product token (``switchboard``, compared without regard to
  case), else the ``*`` groups. Of the rules matching the path and query, the longest wins and ``Allow``
  wins a tie; ``*`` matches any run of characters and a final ``$`` anchors the end. ``/robots.txt`` itself
  is always allowed.
* 401 or 403: treated as "disallow everything" (as the standard library's own ``read()`` does).
* Other 4xx: "unavailable", which RFC 9309 says allows access.
* 429 and 503: a rate limit; nothing is fetched and Retry-After is honored.
* Other 5xx, network failures, oversized files, and redirects that leave the allowed domains:
  "unreachable", which RFC 9309 says means disallow.

Each answer is cached per site in the key-value store: rules and 4xx answers for 24 hours (RFC 9309 asks
crawlers not to use a cached copy for longer), failures until they may be retried (Retry-After, else one
hour). Every watch on a site shares the entry, so a struggling site is asked once rather than once per
watch, and a rate limit on any product page pauses the whole site.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit, urlunsplit

from switchboard.connectors.shopping.fetch import get_vetted
from switchboard.connectors.shopping.settings import NAME, Settings
from switchboard.db import Database
from switchboard.errors import HttpError
from switchboard.httpclient import DEFAULT_USER_AGENT, HttpTransport
from switchboard.util import iso

ROBOTS_CACHE = timedelta(hours=24)
ROBOTS_RETRY = timedelta(hours=1)
ROBOTS_MAX_BYTES = 512_000  # RFC 9309 asks parsers to handle at least 500 KiB
MAX_RETRY_AFTER = timedelta(days=30)
MAX_PATTERN_CHARS = 2_000
_RULES, _ALLOW_ALL, _DISALLOW_ALL = "rules", "allow_all", "disallow_all"
_FAILURES = {"unreachable": "robots_unreachable", "rate_limited": "rate_limited"}
_TOKEN = re.compile(r"[A-Za-z_-]+")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")


@dataclass(frozen=True)
class RobotsVerdict:
    allowed: bool
    outcome: str  # "allowed", "robots_disallowed", "robots_unreachable", or "rate_limited"
    retry_at: datetime | None = None


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str  # normalized with :func:`normalize_path`


@dataclass(frozen=True)
class RobotsPolicy:
    """One cached answer for a site: rules, everything allowed or disallowed, or a failure to wait out."""

    kind: str  # _RULES, _ALLOW_ALL, _DISALLOW_ALL, or a key of _FAILURES
    fetched_at: datetime
    text: str = ""
    retry_at: datetime | None = None

    def verdict(self, url: str) -> RobotsVerdict:
        if self.kind in _FAILURES:
            return RobotsVerdict(False, _FAILURES[self.kind], self.retry_at)
        if self.allows(url):
            return RobotsVerdict(True, "allowed")
        return RobotsVerdict(False, "robots_disallowed")

    def allows(self, url: str) -> bool:
        if self.kind == _ALLOW_ALL:
            return True
        if self.kind != _RULES:
            return False
        return is_allowed(rules_for(self.text, product_token(DEFAULT_USER_AGENT)), url)


def product_token(user_agent: str) -> str:
    """``switchboard/0.1 (...)`` and ``Switchboard`` both give ``switchboard``; ``*`` stays ``*``."""
    if user_agent.strip().startswith("*"):
        return "*"
    match = _TOKEN.match(user_agent.strip())
    return match.group(0).lower() if match else ""


def rules_for(text: str, token: str) -> list[Rule]:
    """The rules for ``token``: its groups combined, else the ``*`` groups combined, else none.

    A group is one or more ``User-agent`` lines followed by rules; blank lines and other fields
    (``Sitemap``, ``Crawl-delay``) neither start nor end one. Rules before the first group are ignored.
    """
    own: list[Rule] = []
    anyone: list[Rule] = []
    agents: set[str] = set()
    named = False
    after_rules = False
    for line in text.splitlines():
        field, _, value = line.split("#", 1)[0].partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if after_rules:
                agents, after_rules = set(), False
            agents.add(product_token(value))
            named = named or token in agents
        elif field in ("allow", "disallow"):
            after_rules = True
            if value and len(value) <= MAX_PATTERN_CHARS:
                rule = Rule(field == "allow", normalize_path(value))
                if token in agents:
                    own.append(rule)
                if "*" in agents:
                    anyone.append(rule)
    return own if named else anyone


def is_allowed(rules: Sequence[Rule], url: str) -> bool:
    """The most specific matching rule decides (most characters, ``Allow`` on a tie); none means allowed."""
    parts = urlsplit(url)
    path = normalize_path((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
    if path == "/robots.txt":
        return True
    best: Rule | None = None
    for rule in rules:
        if not matches(rule.pattern, path):
            continue
        if best is None or (len(rule.pattern), rule.allow) > (len(best.pattern), best.allow):
            best = rule
    return best is None or best.allow


def normalize_path(text: str) -> str:
    """Compare rules and URLs octet for octet (RFC 9309 section 2.2.2).

    Escapes of unreserved characters are decoded (``%7E`` is ``~``), other escapes are written in upper
    case (``%2f`` is ``%2F``), and non-ASCII characters, spaces, and controls are percent-encoded as UTF-8.
    """

    def unescape(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else "%" + match.group(1).upper()

    decoded = _ESCAPE.sub(unescape, text)
    return "".join(
        char if char.isascii() and char.isprintable() and char != " " else quote(char, safe="")
        for char in decoded
    )


def matches(pattern: str, path: str) -> bool:
    """Does a rule match? ``*`` is any run of characters, a final ``$`` anchors the end, else a prefix.

    With ``*`` as the only wildcard, taking each literal piece at its leftmost possible position never
    rules out a match, so one forward scan decides. A regular expression built from a hostile pattern
    could instead backtrack for exponential time.
    """
    anchored = pattern.endswith("$")
    first, *rest = (pattern[:-1] if anchored else pattern).split("*")
    if not path.startswith(first):
        return False
    if not rest:
        return not anchored or path == first
    position = len(first)
    *middle, last = rest
    for piece in middle:
        found = path.find(piece, position)
        if found < 0:
            return False
        position = found + len(piece)
    if anchored:
        return len(path) - len(last) >= position and path.endswith(last)
    return path.find(last, position) >= 0


def robots_url(page_url: str) -> str:
    parts = urlsplit(page_url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def robots_verdict(
    http: HttpTransport, db: Database, page_url: str, now: datetime, settings: Settings
) -> RobotsVerdict:
    """May Switchboard fetch ``page_url`` now? Uses the site's cached answer while it is fresh."""
    location = robots_url(page_url)
    policy = load_cached(db, location, now)
    if policy is None:
        policy = fetch_robots(http, location, now, settings)
        db.kv_set(NAME, _cache_key(location), _dump(policy))
    return policy.verdict(page_url)


def pause_site(db: Database, page_url: str, now: datetime, retry_at: datetime | None) -> datetime:
    """Record that the site rate-limited a page, so every watch on it waits; returns the retry time."""
    policy = RobotsPolicy("rate_limited", now, retry_at=_retry_time(retry_at, now))
    db.kv_set(NAME, _cache_key(robots_url(page_url)), _dump(policy))
    return _retry_time(retry_at, now)


def fetch_robots(http: HttpTransport, location: str, now: datetime, settings: Settings) -> RobotsPolicy:
    """The site's answer, to be cached: rules, a blanket allow or disallow, or a failure with a retry time."""
    headers = {"Accept": "text/plain"}
    try:
        response = get_vetted(http, location, settings, headers=headers, max_bytes=ROBOTS_MAX_BYTES)
    except HttpError:
        return RobotsPolicy("unreachable", now, retry_at=now + ROBOTS_RETRY)
    if response.ok:
        return RobotsPolicy(_RULES, now, response.text())
    if response.status in (429, 503):
        retry_at = parse_retry_after(response.headers.get("retry-after"), now)
        return RobotsPolicy("rate_limited", now, retry_at=_retry_time(retry_at, now))
    if response.status in (401, 403):
        return RobotsPolicy(_DISALLOW_ALL, now)
    if 400 <= response.status < 500:
        return RobotsPolicy(_ALLOW_ALL, now)
    return RobotsPolicy("unreachable", now, retry_at=now + ROBOTS_RETRY)


def load_cached(db: Database, location: str, now: datetime) -> RobotsPolicy | None:
    """The cached answer for ``location`` while it is fresh; None when absent, stale, or unreadable."""
    raw = db.kv_get(NAME, _cache_key(location))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        retry_at = data.get("retry_at")
        policy = RobotsPolicy(
            str(data["kind"]),
            datetime.fromisoformat(data["fetched_at"]),
            str(data.get("text", "")),
            datetime.fromisoformat(retry_at) if retry_at else None,
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if policy.fetched_at.tzinfo is None or not timedelta(0) <= now - policy.fetched_at:
        return None
    if policy.kind in _FAILURES:
        fresh = policy.retry_at is not None and policy.retry_at.tzinfo is not None and now < policy.retry_at
        return policy if fresh else None
    return policy if now - policy.fetched_at < ROBOTS_CACHE else None


def _retry_time(retry_at: datetime | None, now: datetime) -> datetime:
    """A Retry-After moment still ahead, else one hour from now."""
    return retry_at if retry_at is not None and retry_at > now else now + ROBOTS_RETRY


def _cache_key(location: str) -> str:
    return "robots:" + location


def _dump(policy: RobotsPolicy) -> str:
    data = {"kind": policy.kind, "fetched_at": iso(policy.fetched_at), "text": policy.text}
    if policy.retry_at is not None:
        data["retry_at"] = iso(policy.retry_at)
    return json.dumps(data)


def parse_retry_after(value: str | None, now: datetime) -> datetime | None:
    """When a Retry-After header (delay in seconds or an HTTP date) allows the next request.

    Returns None for a missing or unreadable value. Dates in the past mean "now"; anything beyond
    30 days is capped, so a hostile header cannot silence a watch forever.
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        seconds = int(text) if len(text) <= 9 else int(MAX_RETRY_AFTER.total_seconds())
        return now + min(timedelta(seconds=seconds), MAX_RETRY_AFTER)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return min(max(moment, now), now + MAX_RETRY_AFTER)

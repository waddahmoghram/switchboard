"""Read and check the ``[connectors.shopping]`` table.

Parsing never raises. A broken value falls back to a safe default and is reported as a problem, so
``switchboard doctor`` still starts and can say what to fix. A merchant rule with a regex that does
not compile is skipped entirely rather than half applied.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.connectors.shopping.money import normalize_currency

NAME = "shopping"

DEFAULT_CHECK_INTERVAL = 21_600
DEFAULT_MIN_FETCH_INTERVAL = 21_600
MIN_CHECK_INTERVAL = 300
MIN_FETCH_INTERVAL = 3_600
MAX_REGEX_LENGTH = 500

_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SUBJECT_KEYS = ("order_subject_regex", "shipped_subject_regex", "delivered_subject_regex")
_REGEX_KEYS = (*_SUBJECT_KEYS, "order_id_regex", "total_regex")
_RULE_KEYS = frozenset({"name", "from_contains", "currency", *_REGEX_KEYS})
_TOP_KEYS = frozenset(
    {
        "price_check_interval_seconds",
        "min_fetch_interval_seconds",
        "allowed_domains",
        "default_currency",
        "merchant_rules",
    }
)


def is_domain(text: str) -> bool:
    """True for a lowercase DNS name with at least one dot; IP addresses and bare names fail."""
    return len(text) <= 253 and _DOMAIN.fullmatch(text) is not None


@dataclass(frozen=True)
class MerchantRule:
    """One merchant's email conventions. Patterns are compiled case-insensitively."""

    name: str
    from_contains: str
    order_subject: re.Pattern[str] | None = None
    shipped_subject: re.Pattern[str] | None = None
    delivered_subject: re.Pattern[str] | None = None
    order_id: re.Pattern[str] | None = None
    total: re.Pattern[str] | None = None
    currency: str | None = None

    def matches_sender(self, sender: str) -> bool:
        return self.from_contains in sender.lower()


@dataclass(frozen=True)
class Settings:
    price_check_interval_seconds: int = DEFAULT_CHECK_INTERVAL
    min_fetch_interval_seconds: int = DEFAULT_MIN_FETCH_INTERVAL
    allowed_domains: tuple[str, ...] | None = None
    default_currency: str | None = None
    merchant_rules: tuple[MerchantRule, ...] = ()

    def allows_host(self, host: str) -> bool:
        """True when no allowlist is set, or ``host`` is a listed domain or one of its subdomains."""
        if self.allowed_domains is None:
            return True
        name = host.lower().rstrip(".")
        return any(name == domain or name.endswith("." + domain) for domain in self.allowed_domains)


def parse_settings(raw: Mapping[str, Any]) -> tuple[Settings, list[str]]:
    """Return usable settings plus human-readable problems. Never raises, never echoes secrets."""
    problems = [f"unknown setting {key!r}" for key in sorted(set(raw) - _TOP_KEYS)]
    settings = Settings(
        price_check_interval_seconds=_interval(
            raw, "price_check_interval_seconds", DEFAULT_CHECK_INTERVAL, MIN_CHECK_INTERVAL, problems
        ),
        min_fetch_interval_seconds=_interval(
            raw, "min_fetch_interval_seconds", DEFAULT_MIN_FETCH_INTERVAL, MIN_FETCH_INTERVAL, problems
        ),
        allowed_domains=_domains(raw.get("allowed_domains"), problems),
        default_currency=_currency(raw.get("default_currency"), "default_currency", problems),
        merchant_rules=_rules(raw.get("merchant_rules"), problems),
    )
    return settings, problems


def _interval(raw: Mapping[str, Any], key: str, default: int, floor: int, problems: list[str]) -> int:
    value: object = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"{key} must be a whole number of seconds; using {default}")
        return default
    if value < floor:
        problems.append(f"{key} = {value} is below the polite minimum of {floor}; using {floor}")
        return floor
    return value


def _domains(value: Any, problems: list[str]) -> tuple[str, ...] | None:
    """None means "any domain". A malformed list blocks every watch until it is fixed."""
    if value is None:
        return None
    if not isinstance(value, list):
        problems.append(
            "allowed_domains must be a list of host names; no watched page is checked until fixed"
        )
        return ()
    domains: list[str] = []
    for item in value:
        domain = item.strip().lower().strip(".") if isinstance(item, str) else ""
        if is_domain(domain):
            domains.append(domain)
        else:
            problems.append(f"allowed_domains entry {item!r} is not a host name such as 'shop.example.com'")
    if not domains:
        problems.append("allowed_domains is empty, so no watched page can be checked")
    return tuple(domains)


def _currency(value: Any, label: str, problems: list[str]) -> str | None:
    if value is None:
        return None
    code = normalize_currency(value)
    if code is None:
        problems.append(f"{label} must be a three-letter ISO 4217 code such as 'USD'")
    return code


def _rules(value: Any, problems: list[str]) -> tuple[MerchantRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        problems.append("merchant_rules must be an array of tables: [[connectors.shopping.merchant_rules]]")
        return ()
    rules: list[MerchantRule] = []
    for index, item in enumerate(value):
        rule = _rule(item, f"merchant_rules[{index}]", problems)
        if rule is not None:
            rules.append(rule)
    return tuple(rules)


def _rule(item: Any, label: str, problems: list[str]) -> MerchantRule | None:
    if not isinstance(item, Mapping):
        problems.append(f"{label} must be a table")
        return None
    unknown = sorted(str(key) for key in set(item) - _RULE_KEYS)
    if unknown:
        problems.append(f"{label} has unknown keys: {', '.join(unknown)}")
    name = _short_text(item.get("name"), 100)
    sender = _short_text(item.get("from_contains"), 200)
    if name is None or sender is None:
        problems.append(f"{label} needs non-empty 'name' and 'from_contains' strings; rule skipped")
        return None
    label = f"{label} ({name})"
    patterns = _compile_patterns(item, label, problems)
    if patterns is None:
        return None
    if not any(patterns[key] is not None for key in _SUBJECT_KEYS):
        problems.append(f"{label} needs at least one of {', '.join(_SUBJECT_KEYS)}; rule skipped")
        return None
    return MerchantRule(
        name=name,
        from_contains=sender.lower(),
        order_subject=patterns["order_subject_regex"],
        shipped_subject=patterns["shipped_subject_regex"],
        delivered_subject=patterns["delivered_subject_regex"],
        order_id=patterns["order_id_regex"],
        total=patterns["total_regex"],
        currency=_currency(item.get("currency"), f"{label} currency", problems),
    )


def _compile_patterns(
    item: Mapping[str, Any], label: str, problems: list[str]
) -> dict[str, re.Pattern[str] | None] | None:
    """Compile every regex of a rule; None (and problems) when any of them is unusable."""
    compiled: dict[str, re.Pattern[str] | None] = {}
    usable = True
    for key in _REGEX_KEYS:
        source = item.get(key)
        compiled[key] = None
        if source is None:
            continue
        if not isinstance(source, str) or not source or len(source) > MAX_REGEX_LENGTH:
            problems.append(
                f"{label}: {key} must be a non-empty string of at most {MAX_REGEX_LENGTH} characters"
            )
            usable = False
            continue
        try:
            compiled[key] = re.compile(source, re.IGNORECASE)
        except re.error as exc:
            problems.append(f"{label}: {key} does not compile ({exc}); rule skipped")
            usable = False
    return compiled if usable else None


def _short_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if 0 < len(text) <= limit else None

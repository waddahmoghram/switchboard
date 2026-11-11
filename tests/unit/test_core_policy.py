"""Policy gate: tier defaults, overrides, constraints, and origins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from switchboard.errors import ConfigError
from switchboard.policy import Decision, Origin, Policy, Tier
from switchboard.tools import Tool


def _tool(
    name: str = "demo_tool",
    tier: Tier = Tier.ACT,
    recipients: Callable[[Mapping[str, Any]], list[str]] | None = None,
    amount: Callable[[Mapping[str, Any]], Decimal] | None = None,
) -> Tool:
    return Tool(
        name, "demo", tier, {"type": "object"}, lambda args: args, recipients=recipients, amount=amount
    )


def _policy(tmp_path: Path, settings: dict[str, Any] | None = None, recent: int = 0) -> Policy:
    return Policy(
        settings or {}, kill_switch=tmp_path / "KILL_SWITCH", recent_calls=lambda _tool, _seconds: recent
    )


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (Tier.READ, Decision.ALLOW),
        (Tier.DRAFT, Decision.ALLOW),
        (Tier.ACT, Decision.ASK),
        (Tier.SPEND, Decision.DENY),
    ],
)
def test_tier_defaults(tmp_path: Path, tier: Tier, expected: Decision) -> None:
    assert _policy(tmp_path).evaluate(_tool(tier=tier), {}, Origin.ASSISTANT).decision is expected


def test_kill_switch_denies_everything_except_reads(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.kill_switch.touch()
    assert policy.evaluate(_tool(tier=Tier.DRAFT), {}, Origin.HUMAN).decision is Decision.DENY
    assert policy.evaluate(_tool(tier=Tier.READ), {}, Origin.ASSISTANT).decision is Decision.ALLOW


def test_human_invocation_turns_ask_into_allow_but_never_overrides_deny(tmp_path: Path) -> None:
    assert _policy(tmp_path).evaluate(_tool(), {}, Origin.HUMAN).decision is Decision.ALLOW
    denied = _policy(tmp_path, {"tools": {"demo_tool": {"decision": "deny"}}})
    assert denied.evaluate(_tool(), {}, Origin.HUMAN).decision is Decision.DENY


def test_rule_origin_still_asks(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    assert policy.evaluate(_tool(), {}, Origin.RULE).decision is Decision.ASK


def test_exact_rule_beats_glob(tmp_path: Path) -> None:
    policy = _policy(
        tmp_path, {"tools": {"demo_*": {"decision": "deny"}, "demo_tool": {"decision": "allow"}}}
    )
    assert policy.evaluate(_tool(), {}, Origin.ASSISTANT).decision is Decision.ALLOW
    assert policy.evaluate(_tool(name="demo_other"), {}, Origin.ASSISTANT).decision is Decision.DENY


def test_recipient_allowlist(tmp_path: Path) -> None:
    tool = _tool(recipients=lambda args: list(args["to"]))
    policy = _policy(tmp_path, {"tools": {"demo_tool": {"allow_recipients": ["*@example.com"]}}})
    assert policy.evaluate(tool, {"to": ["a@example.com"]}, Origin.ASSISTANT).decision is Decision.ASK
    verdict = policy.evaluate(tool, {"to": ["a@example.com", "b@example.org"]}, Origin.ASSISTANT)
    assert verdict.decision is Decision.DENY
    assert "b@example.org" in verdict.reason


def test_amount_cap(tmp_path: Path) -> None:
    tool = _tool(tier=Tier.SPEND, amount=lambda args: Decimal(str(args["amount"])))
    policy = _policy(tmp_path, {"tools": {"demo_tool": {"decision": "allow", "max_amount": 25}}})
    assert policy.evaluate(tool, {"amount": "20.00"}, Origin.ASSISTANT).decision is Decision.ALLOW
    assert policy.evaluate(tool, {"amount": "25.01"}, Origin.ASSISTANT).decision is Decision.DENY


def test_spend_allow_without_a_cap_still_asks(tmp_path: Path) -> None:
    tool = _tool(tier=Tier.SPEND, amount=lambda _args: Decimal("1"))
    policy = _policy(tmp_path, {"tools": {"demo_tool": {"decision": "allow"}}})
    assert policy.evaluate(tool, {}, Origin.ASSISTANT).decision is Decision.ASK


def test_rate_limit(tmp_path: Path) -> None:
    policy = _policy(tmp_path, {"tools": {"demo_tool": {"max_per_hour": 3}}}, recent=3)
    verdict = policy.evaluate(_tool(), {}, Origin.ASSISTANT)
    assert verdict.decision is Decision.DENY
    assert "rate limit" in verdict.reason


def test_approved_requests_still_respect_deny_rules_and_the_kill_switch(tmp_path: Path) -> None:
    assert _policy(tmp_path).evaluate(_tool(), {}, Origin.APPROVAL).decision is Decision.ALLOW
    denied = _policy(tmp_path, {"tools": {"demo_tool": {"decision": "deny"}}})
    assert denied.evaluate(_tool(), {}, Origin.APPROVAL).decision is Decision.DENY
    policy = _policy(tmp_path)
    policy.kill_switch.touch()
    assert policy.evaluate(_tool(), {}, Origin.APPROVAL).decision is Decision.DENY


def test_invalid_decision_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _policy(tmp_path, {"tools": {"demo_tool": {"decision": "maybe"}}})


def test_nominal_decision_ignores_arguments(tmp_path: Path) -> None:
    assert _policy(tmp_path).nominal(_tool()) is Decision.ASK
    assert (
        _policy(tmp_path, {"tools": {"demo_tool": {"decision": "allow"}}}).nominal(_tool()) is Decision.ALLOW
    )


@pytest.mark.parametrize(
    "rule",
    [
        "deny",
        {"decison": "deny"},
        {"allowed_recipients": ["*@example.com"]},
        {"allow_recipients": "*@example.com"},
        {"max_per_hour": -1},
        {"max_per_hour": True},
        {"max_amount": "lots"},
    ],
)
def test_malformed_or_misspelled_rules_are_refused(tmp_path: Path, rule: Any) -> None:
    with pytest.raises(ConfigError):
        _policy(tmp_path, {"tools": {"demo_tool": rule}})

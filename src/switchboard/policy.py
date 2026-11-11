"""The policy gate: decide allow, ask, or deny for every tool call.

Defaults follow the access ladder from the connectors deep dive:

========  =======================================  ==========
Tier      Meaning                                  Default
========  =======================================  ==========
read      Look at data; no outside effect          allow
draft     Prepare something you finish yourself    allow
act       Send, create, or change outside data     ask
spend     Move or commit money                     deny
========  =======================================  ==========

Per-tool rules in ``[policy.tools.<name>]`` (exact names or shell-style globs) can override the
decision and add constraints:

``decision``          ``"allow"``, ``"ask"``, or ``"deny"``.
``allow_recipients``  Glob patterns every recipient must match (for tools that declare recipients).
``max_amount``        Upper bound for tools that declare an amount.
``max_per_hour``      Successful and in-flight calls per rolling hour, including approvals.

A ``KILL_SWITCH`` file in the data directory denies every non-read call. Human invocations from the
command line turn ``ask`` into ``allow``, because typing the command is the approval. A ``spend``
tool is never allowed without a ``max_amount``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from switchboard.errors import ConfigError
from switchboard.util import glob_any

if TYPE_CHECKING:
    from switchboard.tools import Tool


class Tier(StrEnum):
    READ = "read"
    DRAFT = "draft"
    ACT = "act"
    SPEND = "spend"


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class Origin(StrEnum):
    ASSISTANT = "assistant"  # an AI client through MCP
    RULE = "rule"  # an automation rule reacting to an event
    HUMAN = "human"  # you, typing a command
    APPROVAL = "approval"  # executing a request you approved


DEFAULT_BY_TIER: dict[Tier, Decision] = {
    Tier.READ: Decision.ALLOW,
    Tier.DRAFT: Decision.ALLOW,
    Tier.ACT: Decision.ASK,
    Tier.SPEND: Decision.DENY,
}


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str


class Policy:
    def __init__(
        self,
        settings: Mapping[str, Any],
        *,
        kill_switch: Path,
        recent_calls: Callable[[str, int], int],
    ) -> None:
        tools = settings.get("tools", {})
        if not isinstance(tools, Mapping):
            raise ConfigError("[policy.tools] must be a table of per-tool rules")
        self._rules: dict[str, Mapping[str, Any]] = {
            str(name): _checked_rule(str(name), rule) for name, rule in tools.items()
        }
        self.kill_switch = kill_switch
        self._recent_calls = recent_calls

    def rule_for(self, tool_name: str) -> Mapping[str, Any]:
        """Exact match first, then the longest matching glob."""
        if tool_name in self._rules:
            return self._rules[tool_name]
        globs = sorted(
            (pattern for pattern in self._rules if any(char in pattern for char in "*?[")),
            key=len,
            reverse=True,
        )
        for pattern in globs:
            if glob_any(tool_name, [pattern]):
                return self._rules[pattern]
        return {}

    def nominal(self, tool: Tool) -> Decision:
        """The decision before argument-specific constraints, used for listings."""
        if tool.tier is not Tier.READ and self.kill_switch.exists():
            return Decision.DENY
        configured = self.rule_for(tool.name).get("decision")
        return Decision(configured) if configured else DEFAULT_BY_TIER[tool.tier]

    def evaluate(self, tool: Tool, args: Mapping[str, Any], origin: Origin) -> Verdict:
        if tool.tier is not Tier.READ and self.kill_switch.exists():
            return Verdict(Decision.DENY, "the kill switch is on")
        rule = self.rule_for(tool.name)

        if tool.recipients is not None and "allow_recipients" in rule:
            allowed = [str(pattern) for pattern in rule["allow_recipients"]]
            blocked = [address for address in tool.recipients(args) if not glob_any(address, allowed)]
            if blocked:
                return Verdict(Decision.DENY, f"recipient not on the allowlist: {', '.join(blocked)}")

        cap = _as_decimal(rule.get("max_amount"), f"[policy.tools.{tool.name}] max_amount")
        if tool.amount is not None and cap is not None:
            amount = tool.amount(args)
            if amount > cap:
                return Verdict(Decision.DENY, f"amount {amount} exceeds the cap of {cap}")

        limit = rule.get("max_per_hour")
        if limit is not None and self._recent_calls(tool.name, 3600) >= int(limit):
            return Verdict(Decision.DENY, f"rate limit of {int(limit)} calls per hour reached")

        configured = rule.get("decision")
        if configured == Decision.DENY.value:
            return Verdict(Decision.DENY, "a configured rule denies this tool")
        if origin is Origin.APPROVAL:
            return Verdict(Decision.ALLOW, "approved by a human")

        decision = Decision(configured) if configured else DEFAULT_BY_TIER[tool.tier]
        reason = "configured rule" if configured else f"default for the {tool.tier.value} tier"

        if tool.tier is Tier.SPEND and decision is Decision.ALLOW and (cap is None or tool.amount is None):
            return Verdict(
                Decision.ASK, "spend tools need an amount extractor and max_amount to run unattended"
            )
        if origin is Origin.HUMAN and decision is Decision.ASK:
            return Verdict(Decision.ALLOW, "invoked directly by a human")
        return Verdict(decision, reason)


RULE_KEYS = frozenset({"decision", "allow_recipients", "max_amount", "max_per_hour"})


def _checked_rule(name: str, rule: Any) -> Mapping[str, Any]:
    """Refuse malformed or misspelled rules: silently ignoring one would fall back to a looser default."""
    label = f"[policy.tools.{name}]"
    if not isinstance(rule, Mapping):
        raise ConfigError(f'{label} must be a table, for example {{ decision = "deny" }}')
    unknown = sorted(set(rule) - RULE_KEYS)
    if unknown:
        expected = ", ".join(sorted(RULE_KEYS))
        raise ConfigError(f"{label}: unknown setting(s) {', '.join(unknown)}; expected {expected}")
    if "decision" in rule and rule["decision"] not in {decision.value for decision in Decision}:
        raise ConfigError(f"{label} decision must be allow, ask, or deny")
    recipients = rule.get("allow_recipients")
    if recipients is not None and (
        not isinstance(recipients, list) or not all(isinstance(item, str) for item in recipients)
    ):
        raise ConfigError(f"{label} allow_recipients must be a list of address patterns")
    limit = rule.get("max_per_hour")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ConfigError(f"{label} max_per_hour must be a whole number of zero or more")
    _as_decimal(rule.get("max_amount"), f"{label} max_amount")
    return rule


def _as_decimal(value: Any, label: str) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"{label} must be a number") from exc
    if not amount.is_finite() or amount < 0:
        raise ConfigError(f"{label} must be a finite, non-negative number")
    return amount

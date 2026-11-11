"""Tool definitions, the registry, and the dispatcher that sends every call through the policy gate.

Every call, whatever its origin (assistant, rule, human, approval), takes the same path:

1. look up the tool, 2. validate arguments against its JSON Schema, 3. ask the policy,
4. write an audit record, 5. allow, queue for approval, or deny, 6. audit the outcome.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from switchboard.approvals import ApprovalStore
from switchboard.audit import AuditLog, redact
from switchboard.db import Database
from switchboard.errors import ToolError
from switchboard.jsonschema import validate
from switchboard.policy import Decision, Origin, Policy, Tier
from switchboard.util import canonical_json, glob_any, iso_before, now_iso, printable, truncate

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
Handler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class Tool:
    """One capability exposed to assistants, rules, and the command line.

    ``recipients`` and ``amount`` let the policy enforce allowlists and caps. ``summarize`` writes the
    one-line description shown when you are asked to approve the call.
    """

    name: str
    description: str
    tier: Tier
    input_schema: Mapping[str, Any]
    handler: Handler
    title: str | None = None
    summarize: Callable[[Mapping[str, Any]], str] | None = None
    recipients: Callable[[Mapping[str, Any]], list[str]] | None = None
    amount: Callable[[Mapping[str, Any]], Decimal] | None = None
    connector: str = "core"

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"tool name {self.name!r} must be lowercase letters, digits, and underscores")
        if self.input_schema.get("type") != "object":
            raise ValueError(f"tool {self.name}: input_schema must describe an object")

    def annotations(self) -> dict[str, Any]:
        """MCP tool annotations: hints for clients, never a security boundary."""
        return {
            "title": self.title or self.name,
            "readOnlyHint": self.tier is Tier.READ,
            "destructiveHint": self.tier in {Tier.ACT, Tier.SPEND},
            "idempotentHint": self.tier is Tier.READ,
            "openWorldHint": self.connector != "core",
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return [self._tools[name] for name in sorted(self._tools)]

    def exposed(self, patterns: Sequence[str] | None) -> list[Tool]:
        if patterns is None:
            return self.all()
        return [tool for tool in self.all() if glob_any(tool.name, patterns)]


Status = Literal["ok", "pending", "denied", "error", "invalid"]


@dataclass(frozen=True)
class CallOutcome:
    status: Status
    tool: str
    data: Any = None
    message: str = ""
    approval_id: str | None = None

    @property
    def is_error(self) -> bool:
        return self.status in {"denied", "error", "invalid"}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status, "tool": self.tool}
        if self.message:
            result["message"] = self.message
        if self.approval_id:
            result["approval_id"] = self.approval_id
        if self.data is not None:
            result["data"] = self.data
        return result


def count_recent_calls(db: Database, tool: str, seconds: int) -> int:
    with db.session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tool_calls WHERE tool = ? AND outcome = 'ok' AND created_at >= ?",
            (tool, iso_before(seconds)),
        ).fetchone()
    return int(row["n"])


class Dispatcher:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: Policy,
        approvals: ApprovalStore,
        audit: AuditLog,
        db: Database,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.db = db

    def call(self, name: str, args: Mapping[str, Any] | None, origin: Origin) -> CallOutcome:
        arguments = dict(args or {})
        tool = self.registry.get(name)
        if tool is None:
            self.audit.append("tool.unknown", tool=name, origin=origin.value)
            return CallOutcome("invalid", name, message=f"unknown tool {name!r}")
        problems = validate(arguments, tool.input_schema)
        if problems:
            self.audit.append("tool.invalid", tool=name, origin=origin.value, problems=problems)
            return CallOutcome("invalid", name, message="; ".join(problems))

        verdict = self.policy.evaluate(tool, arguments, origin)
        self.audit.append(
            "tool.request",
            tool=name,
            origin=origin.value,
            decision=verdict.decision.value,
            reason=verdict.reason,
            args=arguments,
        )
        if verdict.decision is Decision.DENY:
            self._record(name, origin, verdict.decision, "denied")
            return CallOutcome("denied", name, message=verdict.reason)
        if verdict.decision is Decision.ASK:
            raw_summary = tool.summarize(arguments) if tool.summarize else _default_summary(tool, arguments)
            summary = printable(truncate(raw_summary, 500))
            request = self.approvals.create(tool=name, args=arguments, summary=summary, origin=origin.value)
            self.audit.append("approval.created", approval=request.id, tool=name, summary=summary)
            self._record(name, origin, verdict.decision, "pending")
            return CallOutcome(
                "pending",
                name,
                approval_id=request.id,
                message=(
                    f"Waiting for approval {request.id}: {summary}. Ask the user to run "
                    f"'switchboard approvals approve {request.id}', then check approvals_status."
                ),
            )
        return self._execute(tool, arguments, origin, verdict.decision)

    def approve(self, approval_id: str) -> CallOutcome:
        self.approvals.expire()
        request = self.approvals.get(approval_id)
        if request is None:
            return CallOutcome("invalid", "approvals", message=f"no approval with id {approval_id!r}")
        if not self.approvals.claim(approval_id):
            current = self.approvals.get(approval_id)
            state = current.status if current else "missing"
            return CallOutcome(
                "error", request.tool, message=f"approval {approval_id} is {state}, not pending"
            )
        self.audit.append("approval.approved", approval=approval_id, tool=request.tool)

        tool = self.registry.get(request.tool)
        if tool is None:
            self.approvals.finish(approval_id, ok=False, result=None, error="tool is no longer registered")
            return CallOutcome("error", request.tool, message="tool is no longer registered")
        verdict = self.policy.evaluate(tool, request.args, Origin.APPROVAL)
        if verdict.decision is not Decision.ALLOW:
            self.approvals.finish(approval_id, ok=False, result=None, error=verdict.reason)
            self.audit.append("approval.blocked", approval=approval_id, reason=verdict.reason)
            return CallOutcome("denied", request.tool, message=verdict.reason)

        outcome = self._execute(tool, request.args, Origin.APPROVAL, verdict.decision)
        self.approvals.finish(
            approval_id,
            ok=outcome.status == "ok",
            result=outcome.data,
            error=None if outcome.status == "ok" else outcome.message,
        )
        return outcome

    def deny(self, approval_id: str) -> bool:
        denied = self.approvals.deny(approval_id)
        if denied:
            self.audit.append("approval.denied", approval=approval_id)
        return denied

    def _execute(self, tool: Tool, args: dict[str, Any], origin: Origin, decision: Decision) -> CallOutcome:
        try:
            data = tool.handler(dict(args))
        except ToolError as exc:
            self.audit.append("tool.error", tool=tool.name, origin=origin.value, error=str(exc))
            self._record(tool.name, origin, decision, "error")
            return CallOutcome("error", tool.name, message=str(exc))
        except Exception as exc:
            log.exception("tool %s failed", tool.name)
            self.audit.append("tool.error", tool=tool.name, origin=origin.value, error=type(exc).__name__)
            self._record(tool.name, origin, decision, "error")
            return CallOutcome(
                "error", tool.name, message=f"internal error ({type(exc).__name__}); see the log"
            )
        self.audit.append("tool.done", tool=tool.name, origin=origin.value)
        self._record(tool.name, origin, decision, "ok")
        return CallOutcome("ok", tool.name, data=data)

    def _record(self, name: str, origin: Origin, decision: Decision, outcome: str) -> None:
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO tool_calls (tool, origin, decision, outcome, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, origin.value, decision.value, outcome, now_iso()),
            )


def _default_summary(tool: Tool, args: Mapping[str, Any]) -> str:
    return f"{tool.name} {truncate(canonical_json(redact(dict(args))), 240)}"

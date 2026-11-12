"""Tool definitions, the registry, and the dispatcher that sends every call through the policy gate.

Every call, whatever its origin (assistant, rule, human, approval), takes the same path:

1. look up the tool, 2. validate arguments against its JSON Schema, 3. ask the policy,
4. write an audit record, 5. allow, queue for approval, or deny, 6. audit the outcome.

Execution holds the tool's connector lane, reserves the hourly budget, records every non-read attempt in
the action journal before its handler runs (refusing an identical attempt that is still in flight or has an
unknown outcome), and runs the handler under the ``tool_timeout_seconds`` deadline. See
:mod:`switchboard.actions` and :mod:`switchboard.execution`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from switchboard import execution
from switchboard.actions import ActionBlockedError, ActionJournal, ActionRecord, Finish
from switchboard.approvals import ApprovalStore
from switchboard.audit import AuditLog, redact
from switchboard.db import Database, transaction
from switchboard.errors import ToolError, UncertainOutcomeError
from switchboard.execution import Lanes
from switchboard.jsonschema import check_schema, validate
from switchboard.policy import Decision, Origin, Policy, Tier
from switchboard.util import canonical_json, glob_any, iso_before, now_iso, printable, truncate

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
Handler = Callable[[dict[str, Any]], Any]
DEFAULT_TOOL_TIMEOUT_SECONDS = 120


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
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"tool name {self.name!r} must be lowercase letters, digits, and underscores")
        check_schema(self.input_schema)
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
    """What a call returned. ``action`` (``{"id", "state"}``) is present only for executed non-read calls."""

    status: Status
    tool: str
    data: Any = None
    message: str = ""
    approval_id: str | None = None
    action: Mapping[str, str] | None = None

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
        if self.action is not None:
            result["action"] = dict(self.action)
        return result


def count_recent_calls(db: Database, tool: str, seconds: int) -> int:
    with db.session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tool_calls "
            "WHERE tool = ? AND outcome IN ('ok', 'running') AND created_at >= ?",
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
        journal: ActionJournal | None = None,
        lanes: Lanes | None = None,
        tool_timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> None:
        if not 1 <= tool_timeout_seconds <= 86_400:
            raise ValueError("tool_timeout_seconds must be between 1 and 86400")
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.db = db
        self.journal = journal if journal is not None else ActionJournal(db)
        self.lanes = lanes if lanes is not None else Lanes()
        self.tool_timeout_seconds = tool_timeout_seconds
        # Rewrites operator command hints in outcome messages before they are returned or stored with an
        # approval. The broker replaces it so hints name the commands its operator account can run.
        self.command_hints: Callable[[str], str] = _unchanged

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
                message=self.command_hints(
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
        problems = validate(request.args, tool.input_schema)
        if problems:
            message = "stored arguments no longer match the current tool: " + "; ".join(problems)
            self.approvals.finish(approval_id, ok=False, result=None, error=message)
            self.audit.append("approval.invalid", approval=approval_id, tool=request.tool, problems=problems)
            return CallOutcome("invalid", request.tool, message=message)
        verdict = self.policy.evaluate(tool, request.args, Origin.APPROVAL)
        if verdict.decision is not Decision.ALLOW:
            self.approvals.finish(approval_id, ok=False, result=None, error=verdict.reason)
            self.audit.append("approval.blocked", approval=approval_id, reason=verdict.reason)
            return CallOutcome("denied", request.tool, message=verdict.reason)

        outcome = self._execute(
            tool, request.args, Origin.APPROVAL, verdict.decision, approval_id=approval_id
        )
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

    def _execute(
        self,
        tool: Tool,
        args: dict[str, Any],
        origin: Origin,
        decision: Decision,
        *,
        approval_id: str | None = None,
    ) -> CallOutcome:
        with self.lanes.hold(tool.connector, timeout=self.tool_timeout_seconds) as held:
            if not held:
                reason = f"the {tool.connector} connector stayed busy, so nothing was run; try again later"
                self.audit.append("tool.error", tool=tool.name, origin=origin.value, error=reason)
                self._record(tool.name, origin, decision, "error")
                return CallOutcome("error", tool.name, message=reason)
            return self._execute_in_lane(tool, args, origin, decision, approval_id)

    def _execute_in_lane(
        self, tool: Tool, args: dict[str, Any], origin: Origin, decision: Decision, approval_id: str | None
    ) -> CallOutcome:
        call_id = self._reserve(tool, origin, decision)
        if call_id is None:
            reason = "hourly rate limit reached before execution"
            self.audit.append("tool.denied", tool=tool.name, origin=origin.value, reason=reason)
            self._record(tool.name, origin, Decision.DENY, "denied")
            return CallOutcome("denied", tool.name, message=reason)
        action: ActionRecord | None = None
        if tool.tier is not Tier.READ:
            try:
                action = self.journal.begin(
                    tool=tool.name,
                    args=args,
                    origin=origin.value,
                    approval_id=approval_id,
                    timeout_seconds=self.tool_timeout_seconds,
                )
            except ActionBlockedError as exc:
                self.audit.append(
                    "tool.denied",
                    tool=tool.name,
                    origin=origin.value,
                    reason="an identical earlier request is open",
                    action=exc.action_id,
                    action_state=exc.state,
                )
                self._record(tool.name, origin, decision, "denied", call_id)
                return CallOutcome("denied", tool.name, message=self.command_hints(str(exc)))
            except BaseException:
                self._record(tool.name, origin, decision, "error", call_id)
                raise
        with execution.deadline(self.tool_timeout_seconds, f"tool {tool.name}"):
            try:
                data = tool.handler(dict(args))
            except UncertainOutcomeError as exc:
                return self._failed(tool, origin, decision, call_id, action, "uncertain", str(exc), str(exc))
            except ToolError as exc:
                return self._failed(tool, origin, decision, call_id, action, "failed", str(exc), str(exc))
            except Exception as exc:
                log.exception("tool %s failed", tool.name)
                message = f"internal error ({type(exc).__name__}); see the log"
                return self._failed(
                    tool, origin, decision, call_id, action, "uncertain", message, type(exc).__name__
                )
        unknown = isinstance(data, Mapping) and data.get("effect_unknown") is True
        recorded = self._finish(
            action, "uncertain" if unknown else "succeeded", "the handler reported an unknown effect"
        )
        self.audit.append("tool.done", tool=tool.name, origin=origin.value, **_action_fields(recorded))
        self._record(tool.name, origin, decision, "ok", call_id)
        note = self.command_hints(_unknown_note(recorded))
        return CallOutcome("ok", tool.name, data=data, message=note, action=recorded)

    def _failed(
        self,
        tool: Tool,
        origin: Origin,
        decision: Decision,
        call_id: int,
        action: ActionRecord | None,
        state: Finish,
        message: str,
        audit_error: str,
    ) -> CallOutcome:
        recorded = self._finish(action, state, message)
        self.audit.append(
            "tool.error", tool=tool.name, origin=origin.value, error=audit_error, **_action_fields(recorded)
        )
        self._record(tool.name, origin, decision, "error", call_id)
        note = self.command_hints(_unknown_note(recorded))
        return CallOutcome(
            "error", tool.name, message=f"{message}; {note}" if note else message, action=recorded
        )

    def _finish(self, action: ActionRecord | None, state: Finish, error: str) -> dict[str, str] | None:
        """Record this attempt's result; the stored state wins (an operator may already have decided)."""
        if action is None:
            return None
        try:
            stored = self.journal.finish(action.id, state, error=None if state == "succeeded" else error)
        except Exception:
            log.exception(
                "could not record the result of action %s; a sweep will mark it uncertain", action.id
            )
            return {"id": action.id, "state": action.state}
        return {"id": action.id, "state": action.state if stored is None else stored.state}

    def _reserve(self, tool: Tool, origin: Origin, decision: Decision) -> int | None:
        with self.db.session() as conn, transaction(conn):
            limit = self.policy.rule_for(tool.name).get("max_per_hour")
            if limit is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM tool_calls "
                    "WHERE tool = ? AND outcome IN ('ok', 'running') AND created_at >= ?",
                    (tool.name, iso_before(3600)),
                ).fetchone()
                if int(row["n"]) >= limit:
                    return None
            cursor = conn.execute(
                "INSERT INTO tool_calls (tool, origin, decision, outcome, created_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (tool.name, origin.value, decision.value, now_iso()),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("could not reserve tool execution")
            return cursor.lastrowid

    def _record(
        self, name: str, origin: Origin, decision: Decision, outcome: str, call_id: int | None = None
    ) -> None:
        with self.db.session() as conn:
            if call_id is not None:
                conn.execute("UPDATE tool_calls SET outcome = ? WHERE id = ?", (outcome, call_id))
            else:
                conn.execute(
                    "INSERT INTO tool_calls (tool, origin, decision, outcome, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, origin.value, decision.value, outcome, now_iso()),
                )


def _default_summary(tool: Tool, args: Mapping[str, Any]) -> str:
    return f"{tool.name} {truncate(canonical_json(redact(dict(args))), 240)}"


def _action_fields(action: Mapping[str, str] | None) -> dict[str, str]:
    return {} if action is None else {"action": action["id"], "action_state": action["state"]}


def _unchanged(text: str) -> str:
    return text


def _unknown_note(action: Mapping[str, str] | None) -> str:
    """Guidance for an attempt whose effect may or may not have happened; empty otherwise."""
    if action is None or action["state"] not in {"uncertain", "in_flight"}:
        return ""
    return (
        f"the outcome is unknown (action {action['id']}); an identical request is refused until the user "
        f"checks what happened and runs 'switchboard actions resolve {action['id']}'"
    )

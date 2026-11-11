"""Assemble the application: configuration, storage, connectors, tools, policy, and rules."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from switchboard.approvals import ApprovalStore
from switchboard.audit import AuditLog
from switchboard.config import Config, ensure_private_dir
from switchboard.context import AppContext
from switchboard.db import CORE_MIGRATIONS, Database
from switchboard.errors import ConfigError
from switchboard.events import EventBus
from switchboard.httpclient import HttpClient, HttpTransport
from switchboard.policy import Origin, Policy, Tier
from switchboard.rules import RuleEngine, load_rules
from switchboard.scheduler import Scheduler
from switchboard.tools import CallOutcome, Dispatcher, Tool, ToolRegistry, count_recent_calls

KNOWN_CONNECTORS: dict[str, str] = {
    "mail": "switchboard.connectors.mail",
    "calendar": "switchboard.connectors.calendar",
    "contacts": "switchboard.connectors.contacts",
    "finance": "switchboard.connectors.finance",
    "shopping": "switchboard.connectors.shopping",
    "notify": "switchboard.connectors.notify",
    "webhooks": "switchboard.connectors.webhooks",
    "home": "switchboard.connectors.home",
}


@dataclass
class App:
    ctx: AppContext
    registry: ToolRegistry
    policy: Policy
    approvals: ApprovalStore
    dispatcher: Dispatcher
    rules: RuleEngine
    connectors: dict[str, ModuleType]

    def call(self, tool: str, args: dict[str, Any] | None, origin: Origin) -> CallOutcome:
        return self.dispatcher.call(tool, args, origin)

    def process_events(self) -> int:
        return self.ctx.events.process(self.rules)

    def tick(self) -> None:
        """One iteration of the background runner: expire approvals, poll due jobs, handle events."""
        self.approvals.expire()
        self.ctx.scheduler.run_due()
        self.process_events()


def load_connectors(names: list[str]) -> dict[str, ModuleType]:
    modules: dict[str, ModuleType] = {}
    for name in names:
        path = KNOWN_CONNECTORS.get(name)
        if path is None:
            raise ConfigError(f"unknown connector {name!r}; known: {', '.join(sorted(KNOWN_CONNECTORS))}")
        module = importlib.import_module(path)
        if getattr(module, "NAME", None) != name or not callable(getattr(module, "register", None)):
            raise ConfigError(f"{path} does not follow the connector contract (NAME and register)")
        modules[name] = module
    return modules


def build_app(config: Config, *, http: HttpTransport | None = None) -> App:
    ensure_private_dir(config.data_dir)
    db = Database(config.data_dir / "switchboard.sqlite3")
    db.add_migrations("core", CORE_MIGRATIONS)
    modules = load_connectors(config.enabled_connectors())
    for name, module in modules.items():
        db.add_migrations(name, getattr(module, "MIGRATIONS", []))
    db.migrate()

    audit = AuditLog(config.data_dir / "audit.jsonl")
    events = EventBus(db)
    ctx = AppContext(
        config=config,
        db=db,
        audit=audit,
        events=events,
        http=http if http is not None else HttpClient(),
        scheduler=Scheduler(),
    )
    settings = config.section("policy")
    policy = Policy(
        settings,
        kill_switch=config.data_dir / "KILL_SWITCH",
        recent_calls=lambda tool, seconds: count_recent_calls(db, tool, seconds),
    )
    approvals = ApprovalStore(db, ttl_seconds=int(settings.get("approval_ttl_minutes", 60)) * 60)
    registry = ToolRegistry()
    dispatcher = Dispatcher(registry=registry, policy=policy, approvals=approvals, audit=audit, db=db)
    rules = RuleEngine(load_rules(config.data.get("rules")), dispatcher, audit)
    app = App(ctx, registry, policy, approvals, dispatcher, rules, modules)

    _register_core_tools(app)
    for module in modules.values():
        module.register(ctx, registry)

    retention_days = int(config.section("switchboard").get("event_retention_days", 14))

    def prune_events() -> None:
        events.prune(retention_days)

    ctx.scheduler.add("core.prune_events", 6 * 3600, prune_events, delay=300)
    return app


def _register_core_tools(app: App) -> None:
    empty = {"type": "object", "properties": {}, "additionalProperties": False}

    def status(_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "connectors": sorted(app.connectors),
            "kill_switch": app.policy.kill_switch.exists(),
            "pending_approvals": len(app.approvals.list("pending")),
            "audit_head": app.ctx.audit.head(),
            "tools": len(app.registry.all()),
        }

    def pending(_args: dict[str, Any]) -> list[dict[str, Any]]:
        app.approvals.expire()
        return [
            {"id": req.id, "tool": req.tool, "summary": req.summary, "expires_at": req.expires_at}
            for req in app.approvals.list("pending")
        ]

    def approval_status(args: dict[str, Any]) -> dict[str, Any]:
        request = app.approvals.get(args["id"])
        if request is None:
            return {"id": args["id"], "status": "unknown"}
        return {
            "id": request.id,
            "tool": request.tool,
            "status": request.status,
            "summary": request.summary,
            "result": request.result,
            "error": request.error,
        }

    app.registry.add(
        Tool(
            "switchboard_status",
            "Show enabled connectors, kill-switch state, and pending approvals.",
            Tier.READ,
            empty,
            status,
            title="Switchboard status",
        )
    )
    app.registry.add(
        Tool(
            "approvals_pending",
            "List actions waiting for the user's approval.",
            Tier.READ,
            empty,
            pending,
            title="Pending approvals",
        )
    )
    app.registry.add(
        Tool(
            "approvals_status",
            "Check whether the user approved, denied, or let an approval expire, and read its result.",
            Tier.READ,
            {
                "type": "object",
                "properties": {"id": {"type": "string", "pattern": "^[0-9a-f]{8}$"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            approval_status,
            title="Approval status",
        )
    )

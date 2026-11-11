"""What every connector receives when it registers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from switchboard import secrets
from switchboard.audit import AuditLog
from switchboard.config import Config
from switchboard.db import Database
from switchboard.errors import ConfigError
from switchboard.events import EventBus
from switchboard.httpclient import HttpTransport
from switchboard.scheduler import Scheduler


@dataclass
class AppContext:
    """Shared services. Connectors read their own table with :meth:`connector_config`.

    Cross-connector features go through named *services* rather than imports, so connectors stay
    independent. Well-known names are listed in ``docs/CONNECTOR_GUIDE.md``.
    """

    config: Config
    db: Database
    audit: AuditLog
    events: EventBus
    http: HttpTransport
    scheduler: Scheduler
    services: dict[str, Any] = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        return self.config.data_dir

    def connector_config(self, name: str) -> dict[str, Any]:
        return self.config.connector(name)

    def secret(self, reference: str) -> str:
        return secrets.resolve(reference)

    def provide(self, name: str, service: Any) -> None:
        if name in self.services:
            raise ConfigError(f"service {name!r} is provided twice")
        self.services[name] = service

    def service(self, name: str) -> Any | None:
        return self.services.get(name)

    def require(self, name: str) -> Any:
        service = self.services.get(name)
        if service is None:
            raise ConfigError(
                f"this feature needs the {name!r} service; enable the connector that provides it"
            )
        return service

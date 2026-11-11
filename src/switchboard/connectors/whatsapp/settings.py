"""Settings for offline personal chat-export imports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from switchboard.errors import ConfigError

NAME = "whatsapp"
DATE_ORDERS = ("MDY", "DMY", "YMD")


@dataclass(frozen=True)
class Settings:
    import_dir: Path | None
    date_order: str
    timezone: str | None = None


def load_settings(raw: dict[str, Any]) -> Settings:
    """Validate explicit parsing context without opening files or contacting an account."""
    if set(raw) - {"import_dir", "date_order", "timezone"}:
        raise ConfigError("[connectors.whatsapp] contains unsupported settings; live access is not supported")
    date_order = raw.get("date_order")
    if not isinstance(date_order, str) or date_order not in DATE_ORDERS:
        raise ConfigError("[connectors.whatsapp] date_order is required and must be MDY, DMY, or YMD")
    directory = raw.get("import_dir")
    import_dir = None
    if directory is not None:
        if (
            not isinstance(directory, str)
            or not directory.strip()
            or len(directory) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in directory)
        ):
            raise ConfigError("[connectors.whatsapp] import_dir must be an absolute private folder path")
        try:
            import_dir = Path(directory).expanduser()
        except RuntimeError as exc:
            raise ConfigError(
                "[connectors.whatsapp] import_dir cannot resolve its home-folder prefix"
            ) from exc
        if not import_dir.is_absolute():
            raise ConfigError("[connectors.whatsapp] import_dir must be an absolute private folder path")
    timezone = raw.get("timezone")
    if timezone is not None and (
        not isinstance(timezone, str)
        or not timezone.strip()
        or len(timezone) > 100
        or any(ord(char) < 32 or ord(char) == 127 for char in timezone)
    ):
        raise ConfigError(
            "[connectors.whatsapp] timezone must be a nonempty context label of at most 100 characters"
        )
    return Settings(import_dir, date_order, timezone)

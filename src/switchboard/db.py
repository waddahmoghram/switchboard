"""SQLite storage with per-component migrations.

Each component (the core and every connector) declares ``MIGRATIONS`` as a list of
``(version, statements)`` pairs, where ``statements`` is a sequence of single SQL statements.
Versions apply in order inside one transaction each, so a failed migration leaves no partial schema.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from switchboard.errors import ConfigError
from switchboard.util import now_iso

Migration = tuple[int, Sequence[str]]

CORE_MIGRATIONS: list[Migration] = [
    (
        1,
        (
            """
            CREATE TABLE approvals (
                id TEXT PRIMARY KEY,
                tool TEXT NOT NULL,
                args_json TEXT NOT NULL,
                summary TEXT NOT NULL,
                origin TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'denied', 'expired', 'executed', 'failed')
                ),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                decided_at TEXT,
                result_json TEXT,
                error TEXT
            )
            """,
            "CREATE INDEX approvals_status ON approvals(status, created_at)",
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                dedupe_key TEXT UNIQUE,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                error TEXT
            )
            """,
            "CREATE INDEX events_pending ON events(processed_at, id)",
            """
            CREATE TABLE tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool TEXT NOT NULL,
                origin TEXT NOT NULL,
                decision TEXT NOT NULL,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX tool_calls_recent ON tool_calls(tool, created_at)",
            """
            CREATE TABLE kv (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, key)
            )
            """,
        ),
    ),
]


class Database:
    """A file-backed SQLite database shared by the MCP server, the runner, and the command line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._migrations: dict[str, list[Migration]] = {}

    def connect(self) -> sqlite3.Connection:
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            raise ConfigError(f"cannot open the database: {exc.strerror}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ConfigError("the database must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """A connection that is always closed, for short read or autocommit work."""
        with closing(self.connect()) as conn:
            yield conn

    def add_migrations(self, component: str, migrations: Sequence[Migration]) -> None:
        if component in self._migrations:
            raise ValueError(f"migrations for {component!r} were already registered")
        versions = [version for version, _ in migrations]
        if versions != sorted(set(versions)):
            raise ValueError(f"{component}: migration versions must be unique and increasing")
        self._migrations[component] = list(migrations)

    def migrate(self) -> list[str]:
        """Apply pending migrations; return ``component:version`` labels that ran."""
        applied_now: list[str] = []
        with self.session() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "component TEXT NOT NULL, version INTEGER NOT NULL, applied_at TEXT NOT NULL, "
                "PRIMARY KEY (component, version))"
            )
            done = {
                (row["component"], row["version"])
                for row in conn.execute("SELECT component, version FROM schema_migrations")
            }
            for component in sorted(self._migrations):
                for version, statements in self._migrations[component]:
                    if (component, version) in done:
                        continue
                    with transaction(conn):
                        already = conn.execute(
                            "SELECT 1 FROM schema_migrations WHERE component = ? AND version = ?",
                            (component, version),
                        ).fetchone()
                        if already is not None:  # another process applied it after we looked
                            continue
                        for statement in statements:
                            conn.execute(statement)
                        conn.execute(
                            "INSERT INTO schema_migrations (component, version, applied_at) VALUES (?, ?, ?)",
                            (component, version, now_iso()),
                        )
                    applied_now.append(f"{component}:{version}")
        return applied_now

    def kv_get(self, namespace: str, key: str) -> str | None:
        with self.session() as conn:
            row = conn.execute(
                "SELECT value FROM kv WHERE namespace = ? AND key = ?", (namespace, key)
            ).fetchone()
        return None if row is None else str(row["value"])

    def kv_set(self, namespace: str, key: str, value: str) -> None:
        with self.session() as conn:
            conn.execute(
                "INSERT INTO kv (namespace, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (namespace, key, value, now_iso()),
            )

    def kv_setdefault(self, namespace: str, key: str, value: str) -> str:
        """Store ``value`` only if the key is absent; return whatever is stored (race-free)."""
        with self.session() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO kv (namespace, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (namespace, key, value, now_iso()),
            )
            row = conn.execute(
                "SELECT value FROM kv WHERE namespace = ? AND key = ?", (namespace, key)
            ).fetchone()
        return str(row["value"])


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in an immediate write transaction on an autocommit connection."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

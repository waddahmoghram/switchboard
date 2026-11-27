"""The one place that imports ``fcntl``, so Switchboard refuses to load on a platform without it.

The audit log's hash chain, its rotation, backups, and the runner lock all depend on ``flock``. Carrying on
without it would let two writers read the same last record and fork the chain, so importing this module,
which the audit log and everything that writes state import, fails before any state is written.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError as exc:  # pragma: no cover - exercised in a subprocess that hides fcntl
    raise ImportError(
        "Switchboard needs POSIX file locking (the fcntl module), which Linux and macOS provide; "
        "this platform is not supported"
    ) from exc

__all__ = ["fcntl"]

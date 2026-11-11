"""Resolve secret references without ever storing secrets in the configuration.

Supported reference forms:

``env:NAME``
    The value of an environment variable.
``file:/path/to/secret``
    The contents of a regular file owned by you with no group or other permissions (``chmod 600``).
    Symbolic links are refused, so a planted link cannot redirect the read.
``cmd:program arg ...``
    The standard output of a command run *without* a shell, such as ``cmd:pass show mail/app``,
    ``cmd:secret-tool lookup service switchboard``, or a password manager's command-line tool.

One trailing newline is removed. Empty values are errors. Error messages never include the value.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

from switchboard.errors import SecretError

_MAX_SECRET_BYTES = 64 * 1024


def resolve(reference: str, *, timeout: float = 20.0) -> str:
    if not isinstance(reference, str) or ":" not in reference:
        raise SecretError("secret references must look like env:NAME, file:/path, or cmd:program args")
    scheme, _, rest = reference.partition(":")
    if scheme == "env":
        value = os.environ.get(rest)
        if value is None:
            raise SecretError(f"environment variable {rest!r} is not set")
    elif scheme == "file":
        value = _read_private_file(Path(os.path.expanduser(rest)))
    elif scheme == "cmd":
        value = _run_command(rest, timeout)
    else:
        raise SecretError(f"unknown secret scheme {scheme!r}; use env:, file:, or cmd:")
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value:
        raise SecretError(f"secret reference {scheme}:… resolved to an empty value")
    return value


def _read_private_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise SecretError(f"secret file {path} does not exist") from exc
    except OSError as exc:
        raise SecretError(
            f"cannot open secret file {path} (symbolic links are refused): {exc.strerror}"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretError(f"secret file {path} is not a regular file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise SecretError(f"secret file {path} is not owned by the current user")
        if info.st_mode & 0o077:
            raise SecretError(f"secret file {path} is readable by others; run: chmod 600 {path}")
        if info.st_size > _MAX_SECRET_BYTES:
            raise SecretError(f"secret file {path} is unexpectedly large")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretError(f"secret file {path} is not UTF-8 text") from exc


def _run_command(command: str, timeout: float) -> str:
    argv = shlex.split(command)
    if not argv:
        raise SecretError("cmd: secret reference has no command")
    try:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise SecretError(f"secret command {argv[0]!r} was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise SecretError(f"secret command {argv[0]!r} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise SecretError(f"secret command {argv[0]!r} could not start: {exc.strerror}") from exc
    if completed.returncode != 0:
        raise SecretError(f"secret command {argv[0]!r} exited with status {completed.returncode}")
    return completed.stdout


def is_reference(value: object) -> bool:
    """True when ``value`` looks like a supported secret reference."""
    return isinstance(value, str) and value.split(":", 1)[0] in {"env", "file", "cmd"} and ":" in value

"""Credential-owning broker: an assistant socket and an operator socket for separate accounts.

The broker runs under a dedicated service account that owns the configuration, secrets, database, and audit
log. It serves two Unix domain sockets:

* the **client socket** speaks newline-delimited Model Context Protocol (MCP) JSON-RPC. It lists and calls
  only the tools matching ``[mcp] expose``, always with the ``assistant`` origin;
* the **operator socket** speaks the shared operator protocol from ``contracts/README.md`` ("Operator socket
  protocol"): ``status``, ``approvals``, ``approval``, ``approve``, ``deny``, ``actions``, ``reconcile``, and
  ``kill_switch``, one JSON object per line each way. No operator operation is reachable from the client
  socket.

Every accepted connection is checked twice: the socket file's group and mode limit who can connect, and the
peer's numeric user identifier, read from the kernel (``SO_PEERCRED`` on Linux, ``LOCAL_PEERCRED`` on macOS),
must be listed for that socket. Every call into the application runs under one lock, because connectors are
not thread-safe. Running the assistant and the operator under one account restores the old boundary: that
account can reach both sockets. See ``docs/ISOLATION.md``.

``run_shim`` relays an assistant's standard input and output to the client socket. It needs no
configuration, data directory, or secrets.
"""

from __future__ import annotations

import json
import logging
import os
import re
import selectors
import signal
import socket
import stat
import struct
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol, TypeVar

from switchboard import __version__
from switchboard.actions import STATES, ActionJournal, ActionRecord, ActionStateError, normalize_note
from switchboard.audit import AuditLog
from switchboard.config import Config
from switchboard.errors import ConfigError, SwitchboardError
from switchboard.mcp_server import DEFAULT_INSTRUCTIONS, INVALID_REQUEST, McpServer, serve_stdio
from switchboard.policy import Origin
from switchboard.tools import CallOutcome, Tool
from switchboard.util import json_loads

if TYPE_CHECKING:
    from switchboard.app import App

log = logging.getLogger(__name__)

MAX_LINE_BYTES = 1024 * 1024
DEFAULT_MAX_CONNECTIONS = 16
SOCKET_MODE = 0o660
MAX_SOCKET_PATH_BYTES = 100
DEFAULT_OPERATOR_SOCKET = Path("/run/switchboard/operator.sock")
OPERATOR_SOCKET_ENV = "SWITCHBOARD_OPERATOR_SOCKET"
CLIENT = "client"
OPERATOR = "operator"
RESOLUTIONS = ("applied", "not_applied")

# Linux <sys/socket.h> struct ucred: pid_t pid, uid_t uid, gid_t gid.
_UCRED = struct.Struct("=iII")
# macOS <sys/un.h> SOL_LOCAL and LOCAL_PEERCRED; <sys/ucred.h> struct xucred is cr_version (u_int),
# cr_uid (uid_t), cr_ngroups (short, then padding), and cr_groups[NGROUPS] with NGROUPS = 16.
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001
_XUCRED_SIZE = 76
_XUCRED_VERSION = 0
_APPROVAL_ID = re.compile(r"[0-9a-f]{8}")
_ACTION_ID = re.compile(r"[0-9a-f]{32}")
_MAX_REQUEST_ID_BYTES = 128

T = TypeVar("T")


class BrokerError(SwitchboardError):
    """The broker cannot start or serve safely."""


class PeerCredentialError(BrokerError):
    """The kernel did not provide a usable identity for a connected peer."""


class FrameTooLargeError(BrokerError):
    """A line exceeded the size limit; the stream cannot be resynchronized, so the connection ends."""


class BrokerClosedError(BrokerError):
    """The broker is shutting down and accepts no further operations."""


class OperatorError(SwitchboardError):
    """An operator request was refused or failed; ``code`` is the machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- peer credentials -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PeerCredentials:
    """The connecting process as the kernel recorded it at connect time; macOS provides no process id."""

    uid: int
    gid: int | None = None
    pid: int | None = None


class SupportsGetsockopt(Protocol):
    def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes: ...


PeerLookup = Callable[[socket.socket], PeerCredentials]


def parse_ucred(raw: bytes) -> PeerCredentials:
    """Decode Linux ``SO_PEERCRED`` output."""
    if len(raw) != _UCRED.size:
        raise PeerCredentialError(f"SO_PEERCRED returned {len(raw)} bytes, expected {_UCRED.size}")
    pid, uid, gid = _UCRED.unpack(raw)
    return PeerCredentials(uid=uid, gid=gid, pid=pid if pid > 0 else None)


def parse_xucred(raw: bytes) -> PeerCredentials:
    """Decode macOS ``LOCAL_PEERCRED`` output (``struct xucred``)."""
    if len(raw) < 12:
        raise PeerCredentialError("LOCAL_PEERCRED returned a truncated structure")
    version, uid = struct.unpack_from("=II", raw, 0)
    if version != _XUCRED_VERSION:
        raise PeerCredentialError(f"LOCAL_PEERCRED returned unknown structure version {version}")
    (groups,) = struct.unpack_from("=h", raw, 8)
    gid = struct.unpack_from("=I", raw, 12)[0] if groups > 0 and len(raw) >= 16 else None
    return PeerCredentials(uid=uid, gid=gid)


def peer_credentials(sock: SupportsGetsockopt, *, platform: str | None = None) -> PeerCredentials:
    """The peer's identity from the kernel; other platforms are refused rather than trusted."""
    system = sys.platform if platform is None else platform
    if system.startswith("linux"):
        option = getattr(socket, "SO_PEERCRED", None)
        if not isinstance(option, int):
            raise PeerCredentialError("this Python build does not expose SO_PEERCRED")
        return parse_ucred(sock.getsockopt(socket.SOL_SOCKET, option, _UCRED.size))
    if system == "darwin":
        return parse_xucred(sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE))
    raise PeerCredentialError(f"peer credentials are not implemented on {system}; the broker refuses to run")


def check_peer_credential_support(platform: str | None = None) -> None:
    """Refuse to start on a platform where connecting identities cannot be read."""
    system = sys.platform if platform is None else platform
    if system.startswith("linux") and isinstance(getattr(socket, "SO_PEERCRED", None), int):
        return
    if system == "darwin":
        return
    raise BrokerError(
        f"the broker needs kernel peer credentials (Linux SO_PEERCRED or macOS LOCAL_PEERCRED); "
        f"{system} is not supported"
    )


def authorized(peer: PeerCredentials, allowed: frozenset[int]) -> bool:
    return peer.uid in allowed


# ---- framing ----------------------------------------------------------------------------------------


class SupportsReadline(Protocol):
    def readline(self, size: int, /) -> bytes: ...


def read_frames(reader: SupportsReadline, limit: int = MAX_LINE_BYTES) -> Iterator[bytes]:
    """Yield newline-delimited frames without the newline; raise once a frame exceeds ``limit`` bytes.

    A final frame without a newline is still yielded at end of input.
    """
    while True:
        line = reader.readline(limit + 1)
        if not line:
            return
        if line.endswith(b"\n"):
            line = line[:-1]
        elif len(line) > limit:
            raise FrameTooLargeError(f"a line exceeded {limit} bytes")
        yield line


def encode_frame(payload: Any) -> bytes:
    """One ASCII-only JSON line; a lone surrogate cannot break the stream."""
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str).encode("ascii") + b"\n"


def write_frame(writer: IO[bytes], payload: Any) -> None:
    writer.write(encode_frame(payload))
    writer.flush()


# ---- the application gateway ------------------------------------------------------------------------

# Behind a broker, the operator account cannot run the direct commands (they read the configuration), so
# hints in instructions and results point at the ``operator`` client instead.
_OPERATOR_HINTS = (
    ("'switchboard approvals approve ", "'switchboard operator approvals approve "),
    ("'switchboard actions resolve ", "'switchboard operator actions resolve "),
)


def operator_hints(text: str) -> str:
    """Rewrite quoted direct-command hints to their ``switchboard operator`` equivalents."""
    for direct, brokered in _OPERATOR_HINTS:
        text = text.replace(direct, brokered)
    return text


class Gateway:
    """The only path from socket threads into the application: one operation at a time."""

    def __init__(self, app: App) -> None:
        self.app = app
        self._lock = threading.Lock()
        self._closed = False
        self._exposure = app.ctx.config.mcp_exposure()
        self.instructions = operator_hints(
            str(app.ctx.config.section("mcp").get("instructions", DEFAULT_INSTRUCTIONS))
        )
        # At the source, so messages stored with an approval and read back later carry the same hints.
        app.dispatcher.command_hints = operator_hints

    def run(self, operation: Callable[[], T]) -> T:
        with self._lock:
            if self._closed:
                raise BrokerClosedError("the broker is shutting down")
            return operation()

    def drain(self, timeout: float) -> bool:
        """Refuse new operations, then wait up to ``timeout`` seconds for the running one to finish."""
        self._closed = True
        if not self._lock.acquire(timeout=max(timeout, 0.0)):
            return False
        self._lock.release()
        return True

    def exposed_tools(self) -> list[Tool]:
        return self.app.registry.exposed(self._exposure)

    def call_tool(self, name: str, args: dict[str, Any], origin: Origin) -> CallOutcome:
        """A client-socket tool call. The socket belongs to the assistant, whatever origin is passed."""
        del origin

        def call() -> CallOutcome:
            if name not in {tool.name for tool in self.exposed_tools()}:
                return CallOutcome("invalid", name, message=f"tool {name!r} is not exposed")
            return self.app.dispatcher.call(name, args, Origin.ASSISTANT)

        return self.run(call)

    def mcp_server(self) -> McpServer:
        """A fresh protocol session (each connection negotiates its own protocol version)."""
        return McpServer(tools=self.exposed_tools, call=self.call_tool, instructions=self.instructions)


def serve_client_connection(
    gateway: Gateway, reader: SupportsReadline, writer: IO[bytes], peer: PeerCredentials
) -> None:
    """Serve one assistant session until end of input; an oversized line ends the session."""
    log.info("client session opened by uid %d", peer.uid)
    try:
        serve_stdio(gateway.mcp_server(), read_frames(reader), writer)
    except FrameTooLargeError:
        with suppress(OSError):
            write_frame(
                writer,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": INVALID_REQUEST,
                        "message": f"a message exceeded {MAX_LINE_BYTES} bytes; closing the session",
                    },
                },
            )
    log.info("client session closed for uid %d", peer.uid)


# ---- the operator interface -------------------------------------------------------------------------

# Fields each operation accepts besides "op" and "request_id": (allowed, required). This is the shared
# protocol in contracts/README.md, "Operator socket protocol"; the native broker accepts the same requests.
OPERATIONS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "status": (frozenset(), frozenset()),
    "approvals": (frozenset(), frozenset()),
    "approval": (frozenset({"id"}), frozenset({"id"})),
    "approve": (frozenset({"id"}), frozenset({"id"})),
    "deny": (frozenset({"id"}), frozenset({"id"})),
    "actions": (frozenset({"state", "limit", "id"}), frozenset()),
    "reconcile": (frozenset({"id", "resolution", "note"}), frozenset({"id", "resolution", "note"})),
    "kill_switch": (frozenset({"enable"}), frozenset()),
}
MAX_OP_CHARS = 32
MAX_ACTIONS = 100
MAX_PENDING_LISTED = 100
KILL_SWITCH_SCOPE = "denies new non-read calls; reads, polling and already submitted requests continue"


def outcome(status: str, tool: str, message: str, **extra: Any) -> dict[str, Any]:
    """The result of an operation that ran but refused or reported, shaped like a tool outcome."""
    return {"status": status, "tool": tool, "message": message, **extra}


def operator_failure(op: Any, tag: Any, code: str, message: str) -> dict[str, Any]:
    """A request that could not be processed as written; nothing was changed."""
    return {"ok": False, "op": op, "request_id": tag, "error": {"code": code, "message": message}}


def _valid_tag(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and len(value.encode("utf-8", "surrogatepass")) <= _MAX_REQUEST_ID_BYTES


def operation_name(request: Any) -> str | None:
    """The ``op`` to echo: text of at most 32 characters, otherwise null."""
    op = request.get("op") if isinstance(request, dict) else None
    return op if isinstance(op, str) and len(op) <= MAX_OP_CHARS else None


def request_tag(request: Any) -> str | int | None:
    """The optional ``request_id`` to echo when it is an integer or text of at most 128 bytes."""
    tag = request.get("request_id") if isinstance(request, dict) else None
    return tag if _valid_tag(tag) else None


def check_request(request: Any) -> tuple[str, dict[str, Any]]:
    """The operation and its fields; anything the protocol does not list is refused."""
    if not isinstance(request, dict):
        raise OperatorError("invalid", "operator requests must be JSON objects")
    op = request.get("op")
    if not isinstance(op, str) or not 1 <= len(op) <= MAX_OP_CHARS:
        raise OperatorError("invalid", "op must name an operation")
    if "request_id" in request and not _valid_tag(request["request_id"]):
        raise OperatorError("invalid", "request_id must be an integer or text of at most 128 bytes")
    spec = OPERATIONS.get(op)
    if spec is None:
        raise OperatorError("invalid", f"unsupported operation {op!r}")
    allowed, required = spec
    unknown = sorted(str(key) for key in set(request) - allowed - {"op", "request_id"})
    if unknown:
        raise OperatorError("invalid", f"{op} does not accept: {', '.join(unknown)}")
    missing = sorted(required - set(request))
    if missing:
        raise OperatorError("invalid", f"{op} needs: {', '.join(missing)}")
    return op, {key: value for key, value in request.items() if key in allowed}


def _field_text(fields: dict[str, Any], key: str) -> str:
    value = fields[key]
    if not isinstance(value, str):
        raise OperatorError("invalid", f"{key} must be text")
    return value


def _action_view(record: ActionRecord) -> dict[str, Any]:
    """One journal entry with its stored arguments; the owner is an internal process token."""
    view = record.to_dict()
    del view["owner"]
    return view


OperatorHandler = Callable[[dict[str, Any], PeerCredentials], Any]


class OperatorApi:
    """Answer operator requests: ``{"op", "request_id"?, ...fields}`` in, one response object out.

    ``ok: false`` means the request could not be processed as written. ``ok: true`` means the operation
    ran; its ``result`` can still report a refusal through ``status`` (``invalid``, ``error``, or
    ``denied``), as tool outcomes do. State-changing requests write an audit record naming the peer's user
    and process identifiers.
    """

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self._handlers: dict[str, OperatorHandler] = {
            "status": self._status,
            "approvals": self._approvals,
            "approval": self._approval,
            "approve": self._approve,
            "deny": self._deny,
            "actions": self._actions,
            "reconcile": self._reconcile,
            "kill_switch": self._kill_switch,
        }

    def handle(self, request: Any, peer: PeerCredentials) -> dict[str, Any]:
        op = operation_name(request)
        tag = request_tag(request)
        try:
            name, fields = check_request(request)
            result = self.gateway.run(partial(self._handlers[name], fields, peer))
        except OperatorError as exc:
            return operator_failure(op, tag, exc.code, exc.message)
        except BrokerClosedError as exc:
            return operator_failure(op, tag, "unavailable", str(exc))
        except SwitchboardError as exc:
            return operator_failure(op, tag, "error", str(exc))
        except Exception:
            log.exception("operator request %r from uid %d failed", op, peer.uid)
            return operator_failure(
                op, tag, "internal", "internal error; inspect state before retrying external effects"
            )
        return {"ok": True, "op": op, "request_id": tag, "result": result}

    @property
    def _app(self) -> App:
        return self.gateway.app

    def _audit(self, kind: str, peer: PeerCredentials, **fields: Any) -> None:
        self._app.ctx.audit.append(
            kind, via="operator_socket", peer_uid=peer.uid, peer_pid=peer.pid, **fields
        )

    def _status(self, fields: dict[str, Any], peer: PeerCredentials) -> dict[str, Any]:
        app = self._app
        app.approvals.expire()
        with app.ctx.db.session() as conn:
            pending = conn.execute("SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'").fetchone()
        counts = ActionJournal(app.ctx.db).counts()
        return {
            "status": "ok",
            "tool": "switchboard_status",
            "data": {
                "backend": "python",
                "version": __version__,
                "connectors": sorted(app.connectors),
                "kill_switch": app.policy.kill_switch.exists(),
                "pending_approvals": int(pending["n"]),
                "open_actions": {"in_flight": counts["in_flight"], "uncertain": counts["uncertain"]},
                "audit_head": app.ctx.audit.head(),
                "same_user_sandbox": False,
                "peer_uid": peer.uid,
            },
        }

    def _approvals(self, fields: dict[str, Any], peer: PeerCredentials) -> dict[str, Any]:
        self._app.approvals.expire()
        pending = self._app.approvals.list("pending", limit=MAX_PENDING_LISTED)
        rows = [
            {
                "id": request.id,
                "tool": request.tool,
                "summary": request.summary,
                "expires_at": request.expires_at,
            }
            for request in sorted(pending, key=lambda request: (request.created_at, request.id))
        ]
        return {"status": "ok", "tool": "approvals_pending", "data": rows}

    def _approval(self, fields: dict[str, Any], peer: PeerCredentials) -> dict[str, Any]:
        approval_id = _field_text(fields, "id")
        if not _APPROVAL_ID.fullmatch(approval_id):
            raise OperatorError("invalid", "approval identifiers are 8 lowercase hexadecimal characters")
        self._app.approvals.expire()
        request = self._app.approvals.get(approval_id)
        if request is None:
            raise OperatorError("invalid", f"no approval {approval_id}")
        return request.to_dict()

    def _approve(self, fields: dict[str, Any], peer: PeerCredentials) -> dict[str, Any]:
        approval_id = _field_text(fields, "id")
        if not _APPROVAL_ID.fullmatch(approval_id):
            return outcome(
                "invalid", "approvals", "approval identifiers are 8 lowercase hexadecimal characters"
            )
        request = self._app.approvals.get(approval_id)
        if request is None:
            return outcome("invalid", "approvals", f"no approval with id {approval_id!r}")
        self._audit("operator.approve", peer, approval=approval_id, tool=request.tool)
        return self._app.dispatcher.approve(approval_id).to_dict()

    def _deny(self, fields: dict[str, Any], peer: PeerCredentials) -> dict[str, Any]:
        approval_id = _field_text(fields, "id")
        if not _APPROVAL_ID.fullmatch(approval_id):
            return outcome(
                "invalid", "approvals", "approval identifiers are 8 lowercase hexadecimal characters"
            )
        self._app.approvals.expire()
        request = self._app.approvals.get(approval_id)
        refused = outcome("error", "approvals", f"approval {approval_id} is missing or not pending")
        if request is None or request.status != "pending":
            return refused
        self._audit("operator.deny", peer, approval=approval_id, tool=request.tool)
        if not self._app.dispatcher.deny(approval_id):
            return refused
        return {"id": approval_id, "status": "denied"}

    def _actions(self, fields: dict[str, Any], peer: PeerCredentials) -> dict[str, Any]:
        journal = ActionJournal(self._app.ctx.db)
        if "id" in fields:
            action_id = _field_text(fields, "id")
            if len(fields) != 1:
                return outcome("invalid", "actions", "id is used alone, without state or limit")
            if not _ACTION_ID.fullmatch(action_id):
                return outcome(
                    "invalid", "actions", "action identifiers are 32 lowercase hexadecimal characters"
                )
            record = journal.get(action_id)
            if record is None:
                return outcome("invalid", "actions", f"no action {action_id}")
            return {"status": "ok", "tool": "actions", "action": _action_view(record)}
        state = fields.get("state")
        if state is not None and not isinstance(state, str):
            raise OperatorError("invalid", "state must be text")
        limit = fields.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise OperatorError("invalid", "limit must be a whole number")
        if state is not None and state not in STATES:
            return outcome("invalid", "actions", f"state must be one of {', '.join(STATES)}")
        if not 1 <= limit <= MAX_ACTIONS:
            return outcome("invalid", "actions", f"limit must be from 1 to {MAX_ACTIONS}")
        records = journal.recent(state=state, limit=limit)
        return {"status": "ok", "tool": "actions", "actions": [record.public() for record in records]}

    def _reconcile(self, fields: dict[str, Any], peer: PeerCredentials) -> dict[str, Any]:
        action_id = _field_text(fields, "id")
        resolution = _field_text(fields, "resolution")
        note = _field_text(fields, "note")
        if not _ACTION_ID.fullmatch(action_id):
            return outcome(
                "invalid", "reconcile", "action identifiers are 32 lowercase hexadecimal characters"
            )
        if resolution not in RESOLUTIONS:
            return outcome("invalid", "reconcile", "resolution must be applied or not_applied")
        try:
            cleaned = normalize_note(note)
        except ActionStateError as exc:
            return outcome("invalid", "reconcile", str(exc))
        journal = ActionJournal(self._app.ctx.db)
        if journal.get(action_id) is None:
            return outcome("invalid", "reconcile", f"no action {action_id}")
        self._audit("operator.reconcile", peer, action=action_id, resolution=resolution)
        try:
            record = journal.reconcile(action_id, applied=resolution == "applied", note=cleaned)
        except ActionStateError as exc:
            return outcome("error", "reconcile", str(exc))
        self._audit(
            "action.reconciled",
            peer,
            action=record.id,
            tool=record.tool,
            state=record.state,
            note=record.resolution_note,
        )
        return outcome(
            "ok",
            "reconcile",
            "recorded the operator's decision; nothing was repeated, reversed or verified",
            action=_action_view(record),
        )

    def _kill_switch(self, fields: dict[str, Any], peer: PeerCredentials) -> dict[str, Any]:
        switch = self._app.policy.kill_switch
        changed = False
        if "enable" in fields:
            enable = fields["enable"]
            if not isinstance(enable, bool):
                raise OperatorError("invalid", "enable must be true or false")
            if enable and not switch.exists():
                switch.touch(mode=0o600, exist_ok=True)
                changed = True
            elif not enable and switch.exists():
                switch.unlink(missing_ok=True)
                changed = True
            self._audit("kill_switch.on" if enable else "kill_switch.off", peer, changed=changed)
        return {
            "status": "ok",
            "tool": "kill_switch",
            "kill_switch": switch.exists(),
            "changed": changed,
            "scope": KILL_SWITCH_SCOPE,
        }


def serve_operator_connection(
    api: OperatorApi, reader: SupportsReadline, writer: IO[bytes], peer: PeerCredentials
) -> None:
    """Answer operator requests line by line until end of input; an oversized line ends the connection."""
    try:
        for frame in read_frames(reader):
            text = frame.strip()
            if not text:
                continue
            try:
                request = json_loads(text)
            except (ValueError, RecursionError):
                response = operator_failure(None, None, "invalid", "operator request is not valid JSON")
            else:
                response = api.handle(request, peer)
            write_frame(writer, response)
    except FrameTooLargeError:
        with suppress(OSError):
            write_frame(
                writer,
                operator_failure(
                    None, None, "limit", f"an operator request exceeded {MAX_LINE_BYTES} bytes; closing"
                ),
            )


# ---- settings and socket files ----------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerSettings:
    client_socket: Path
    operator_socket: Path
    client_uids: frozenset[int]
    operator_uids: frozenset[int]
    client_group: int | None = None
    operator_group: int | None = None
    max_connections: int = DEFAULT_MAX_CONNECTIONS


def broker_settings(
    config: Config, *, client_group: int | None = None, operator_group: int | None = None
) -> BrokerSettings:
    """Complete ``[broker]`` settings; every key is required to serve."""
    table = config.broker_settings()
    required = ("client_socket", "operator_socket", "client_uids", "operator_uids")
    missing = [key for key in required if key not in table]
    if missing:
        raise ConfigError(f"[broker] needs {', '.join(missing)}; see docs/ISOLATION.md")
    client = _socket_path(table["client_socket"], "client_socket")
    operator = _socket_path(table["operator_socket"], "operator_socket")
    if client == operator:
        raise ConfigError("[broker] client_socket and operator_socket must be different paths")
    return BrokerSettings(
        client_socket=client,
        operator_socket=operator,
        client_uids=frozenset(table["client_uids"]),
        operator_uids=frozenset(table["operator_uids"]),
        client_group=client_group,
        operator_group=operator_group,
    )


def _socket_path(value: str, key: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"[broker] {key} must be an absolute path")
    if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
        raise ConfigError(f"[broker] {key} must be at most {MAX_SOCKET_PATH_BYTES} bytes long")
    return path


def separation_problems(settings: BrokerSettings, *, service_uid: int) -> list[str]:
    """Configurations that give no approval separation; the broker refuses them unless told otherwise."""
    problems: list[str] = []
    shared = sorted(settings.client_uids & settings.operator_uids)
    if shared:
        problems.append(
            f"user identifier(s) {', '.join(map(str, shared))} may use both sockets, "
            "so that account can approve its own requests"
        )
    if service_uid in settings.client_uids:
        problems.append(
            f"the broker's own account (uid {service_uid}) is a client, so an assistant there could read "
            "the credentials and database directly"
        )
    return problems


def check_socket_directory(directory: Path, *, service_uid: int) -> None:
    """The socket directory must be real, owned by the service account or root, not writable by others."""
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        raise BrokerError(
            f"socket directory {directory} does not exist; create it (with systemd: RuntimeDirectory=)"
        ) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BrokerError(f"socket directory {directory} must be a real directory, not a link or file")
    if info.st_uid not in {service_uid, 0}:
        raise BrokerError(f"socket directory {directory} must belong to the broker's account or root")
    if info.st_mode & 0o022:
        raise BrokerError(f"socket directory {directory} must not be writable by group or others")


def socket_in_use(path: Path, timeout: float = 1.0) -> bool:
    """True when something accepts connections at ``path``, or when that cannot be ruled out."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(os.fspath(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True
    finally:
        probe.close()
    return True


def clear_stale_socket(path: Path, *, service_uid: int, in_use: Callable[[Path], bool]) -> None:
    """Remove a leftover socket owned by the service account; refuse links, files, and live sockets."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise BrokerError(f"{path} is a symbolic link; refusing to replace it")
    if not stat.S_ISSOCK(info.st_mode):
        raise BrokerError(f"{path} exists and is not a socket; refusing to replace it")
    if info.st_uid != service_uid:
        raise BrokerError(f"{path} belongs to another account; refusing to replace it")
    if in_use(path):
        raise BrokerError(f"{path} is accepting connections; is another broker running?")
    os.unlink(path)
    log.info("removed stale socket %s", path)


def remove_socket(path: Path, identity: tuple[int, int]) -> None:
    """Unlink ``path`` only if it is still the socket this process created."""
    with suppress(OSError):
        info = os.lstat(path)
        if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            os.unlink(path)


def bind_socket(
    path: Path,
    *,
    service_uid: int,
    group: int | None,
    in_use: Callable[[Path], bool] = socket_in_use,
    backlog: int = 16,
) -> tuple[socket.socket, tuple[int, int]]:
    """Create a listening socket with mode 660, optionally owned by ``group``; never wider in between."""
    check_socket_directory(path.parent, service_uid=service_uid)
    clear_stale_socket(path, service_uid=service_uid, in_use=in_use)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity: tuple[int, int] | None = None
    try:
        previous = os.umask(0o177)
        try:
            listener.bind(os.fspath(path))
        finally:
            os.umask(previous)
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != service_uid:
            raise BrokerError(f"{path} changed while it was being created")
        identity = (info.st_dev, info.st_ino)
        if group is not None:
            try:
                os.chown(path, -1, group, follow_symlinks=False)
            except PermissionError as exc:
                raise BrokerError(
                    f"cannot give {path} to group {group}; add the broker's account to that group"
                ) from exc
        os.chmod(path, SOCKET_MODE)
        listener.listen(backlog)
        listener.setblocking(False)
        return listener, identity
    except BaseException:
        listener.close()
        if identity is not None:
            remove_socket(path, identity)
        raise


# ---- the server -------------------------------------------------------------------------------------


@dataclass
class _Listener:
    kind: str
    path: Path
    sock: socket.socket
    allowed: frozenset[int]
    identity: tuple[int, int]


class BrokerServer:
    """Accept on both sockets, check each peer, and serve each connection on its own thread."""

    def __init__(
        self,
        gateway: Gateway,
        settings: BrokerSettings,
        *,
        peer_lookup: PeerLookup = peer_credentials,
        service_uid: int | None = None,
        in_use: Callable[[Path], bool] = socket_in_use,
        grace_seconds: float = 30.0,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.operator_api = OperatorApi(gateway)
        self._peer_lookup = peer_lookup
        self._service_uid = os.getuid() if service_uid is None else service_uid
        self._in_use = in_use
        self._grace = grace_seconds
        self._stop = threading.Event()
        self._listeners: list[_Listener] = []
        self._connections: dict[str, set[socket.socket]] = {CLIENT: set(), OPERATOR: set()}
        self._threads: list[threading.Thread] = []
        self._guard = threading.Lock()
        self._refusals = 0

    def start(self) -> None:
        if self._listeners:
            raise BrokerError("the broker is already listening")
        sockets = (
            (CLIENT, self.settings.client_socket, self.settings.client_uids, self.settings.client_group),
            (
                OPERATOR,
                self.settings.operator_socket,
                self.settings.operator_uids,
                self.settings.operator_group,
            ),
        )
        try:
            for kind, path, allowed, group in sockets:
                sock, identity = bind_socket(
                    path, service_uid=self._service_uid, group=group, in_use=self._in_use
                )
                self._listeners.append(_Listener(kind, path, sock, allowed, identity))
        except BaseException:
            self._close_listeners()
            raise

    def request_stop(self) -> None:
        self._stop.set()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        if not self._listeners:
            raise BrokerError("start the broker before serving")
        with selectors.DefaultSelector() as selector:
            for listener in self._listeners:
                selector.register(listener.sock, selectors.EVENT_READ, listener)
            while not self._stop.is_set():
                for key, _events in selector.select(poll_interval):
                    self._accept(key.data)

    def close(self) -> None:
        """Stop accepting, let the running operation finish (bounded), then end every connection."""
        self._stop.set()
        self._close_listeners()
        if not self.gateway.drain(self._grace):
            log.warning("an operation was still running after %.0f seconds; stopping without it", self._grace)
        with self._guard:
            connections = [conn for group in self._connections.values() for conn in group]
            threads = list(self._threads)
        for conn in connections:
            with suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
        for thread in threads:
            thread.join(timeout=5.0)

    def active_connections(self, kind: str) -> int:
        with self._guard:
            return len(self._connections[kind])

    def _close_listeners(self) -> None:
        for listener in self._listeners:
            with suppress(OSError):
                listener.sock.close()
            remove_socket(listener.path, listener.identity)
        self._listeners.clear()

    def _accept(self, listener: _Listener) -> None:
        try:
            conn, _address = listener.sock.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            log.warning("accept on the %s socket failed: %s", listener.kind, exc)
            return
        conn.setblocking(True)
        try:
            peer = self._peer_lookup(conn)
        except (OSError, PeerCredentialError) as exc:
            log.warning("refused a %s connection without usable peer credentials: %s", listener.kind, exc)
            _refuse(conn, listener.kind, "forbidden", "peer credentials are unavailable")
            return
        if not authorized(peer, listener.allowed):
            self._refusals += 1
            # Report the first refusals, then only powers of two, so a flood cannot fill the logs.
            if self._refusals <= 10 or self._refusals & (self._refusals - 1) == 0:
                log.warning(
                    "refused a %s socket connection from uid %d (%d refusals so far)",
                    listener.kind,
                    peer.uid,
                    self._refusals,
                )
                if listener.kind == OPERATOR:
                    _audit_quietly(
                        self.gateway.app.ctx.audit,
                        "broker.refused",
                        socket=OPERATOR,
                        peer_uid=peer.uid,
                        peer_pid=peer.pid,
                        refusals=self._refusals,
                    )
            _refuse(conn, listener.kind, "forbidden", f"this account may not use the {listener.kind} socket")
            return
        with self._guard:
            active = self._connections[listener.kind]
            busy = len(active) >= self.settings.max_connections
            if not busy:
                active.add(conn)
        if busy:
            log.warning(
                "refused a %s connection: %d already open", listener.kind, self.settings.max_connections
            )
            _refuse(conn, listener.kind, "busy", "too many connections; try again later")
            return
        thread = threading.Thread(
            target=self._serve,
            args=(listener.kind, conn, peer),
            name=f"switchboard-broker-{listener.kind}",
            daemon=True,
        )
        with self._guard:
            self._threads = [item for item in self._threads if item.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _serve(self, kind: str, conn: socket.socket, peer: PeerCredentials) -> None:
        try:
            with conn.makefile("rb") as reader, conn.makefile("wb") as writer:
                if kind == CLIENT:
                    serve_client_connection(self.gateway, reader, writer, peer)
                else:
                    serve_operator_connection(self.operator_api, reader, writer, peer)
        except OSError:
            log.info("the %s connection from uid %d ended abruptly", kind, peer.uid)
        except Exception:
            log.exception("the %s connection from uid %d failed", kind, peer.uid)
        finally:
            with self._guard:
                self._connections[kind].discard(conn)
            with suppress(OSError):
                conn.close()


def _refuse(conn: socket.socket, kind: str, code: str, message: str) -> None:
    if kind == CLIENT:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": INVALID_REQUEST, "message": message},
        }
    else:
        payload = operator_failure(None, None, code, message)
    try:
        conn.settimeout(1.0)
        conn.sendall(encode_frame(payload))
    except OSError:
        log.debug("could not tell a refused %s peer why", kind)
    finally:
        conn.close()


def _audit_quietly(audit: AuditLog, kind: str, **fields: Any) -> None:
    try:
        audit.append(kind, **fields)
    except Exception:
        log.exception("could not write the %s audit record", kind)


def serve(app: App, settings: BrokerSettings) -> int:
    """Serve until SIGTERM or SIGINT; the running operation may finish within the tool deadline."""
    check_peer_credential_support()
    grace = float(app.ctx.config.bounded("switchboard", "tool_timeout_seconds") + 30)
    server = BrokerServer(Gateway(app), settings, grace_seconds=grace)
    server.start()
    previous: dict[signal.Signals, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda _number, _frame: server.request_stop())
    print(
        f"broker: client socket {settings.client_socket} for uid(s) "
        f"{', '.join(map(str, sorted(settings.client_uids)))}; operator socket {settings.operator_socket} "
        f"for uid(s) {', '.join(map(str, sorted(settings.operator_uids)))}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    finally:
        for signum, handler in previous.items():
            if handler is not None:
                signal.signal(signum, handler)
        server.close()
    print("broker: stopped", file=sys.stderr)
    return 0


# ---- clients: the operator command line and the assistant shim --------------------------------------


class LineChannel(Protocol):
    def send(self, data: bytes) -> None: ...

    def end_input(self) -> None: ...

    def readline(self, size: int, /) -> bytes: ...

    def close(self) -> None: ...


class SocketChannel:
    """A connected Unix stream socket used one line at a time."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._reader = sock.makefile("rb")

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def end_input(self) -> None:
        with suppress(OSError):
            self._sock.shutdown(socket.SHUT_WR)

    def readline(self, size: int, /) -> bytes:
        return self._reader.readline(size)

    def close(self) -> None:
        with suppress(OSError):
            self._reader.close()
        with suppress(OSError):
            self._sock.close()


def connect_unix(path: Path, *, timeout: float | None = 10.0) -> SocketChannel:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(os.fspath(path))
        sock.settimeout(None)
    except BaseException:
        sock.close()
        raise
    return SocketChannel(sock)


class OperatorClient:
    """Send operator requests over one connection; refused requests raise :class:`OperatorError`."""

    def __init__(self, channel: LineChannel) -> None:
        self._channel = channel
        self._next_id = 0

    def __enter__(self) -> OperatorClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._channel.close()

    def call(self, op: str, **fields: Any) -> Any:
        """Send ``{"op": op, "request_id": n, **fields}`` and return the reply's ``result``."""
        self._next_id += 1
        request_id = self._next_id
        try:
            self._channel.send(encode_frame({"op": op, "request_id": request_id, **fields}))
            frame = next(read_frames(self._channel), None)
        except FrameTooLargeError as exc:
            raise OperatorError("protocol", "the broker sent an oversized reply") from exc
        except OSError as exc:
            raise OperatorError(
                "closed", f"the connection to the broker failed: {exc.strerror or exc}"
            ) from exc
        if frame is None:
            raise OperatorError("closed", "the broker closed the connection")
        try:
            reply = json_loads(frame)
        except ValueError as exc:
            raise OperatorError("protocol", "the broker sent invalid JSON") from exc
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise OperatorError("protocol", "the broker sent an unexpected reply")
        if reply.get("request_id") not in (request_id, None) or reply.get("op") not in (op, None):
            raise OperatorError("protocol", "the broker answered a different request")
        if not reply["ok"]:
            error = reply.get("error")
            details = error if isinstance(error, dict) else {}
            raise OperatorError(str(details.get("code", "failed")), str(details.get("message", "refused")))
        return reply.get("result")


def open_operator_client(path: Path) -> OperatorClient:
    try:
        return OperatorClient(connect_unix(path))
    except OSError as exc:
        raise OperatorError(
            "unreachable", f"cannot reach the operator socket {path}: {exc.strerror or exc}"
        ) from exc


def relay(
    reader: SupportsReadline, writer: IO[bytes], channel: LineChannel, *, limit: int = MAX_LINE_BYTES
) -> str | None:
    """Copy request lines to ``channel`` and reply lines to ``writer``; return a problem, or None if clean."""
    problems: list[str] = []
    input_done = threading.Event()

    def forward() -> None:
        try:
            for frame in read_frames(reader, limit):
                channel.send(frame + b"\n")
        except FrameTooLargeError:
            problems.append(f"a request line exceeded {limit} bytes; the session was closed")
        except OSError as exc:
            problems.append(f"cannot send to the broker: {exc.strerror or exc}")
        else:
            input_done.set()
        channel.end_input()

    threading.Thread(target=forward, name="switchboard-shim-input", daemon=True).start()
    try:
        for frame in read_frames(channel, limit):
            writer.write(frame + b"\n")
            writer.flush()
    except FrameTooLargeError:
        return "the broker sent an oversized line"
    except BrokenPipeError:
        return None
    except OSError as exc:
        return f"the connection to the broker failed: {exc.strerror or exc}"
    if problems:
        return problems[0]
    if not input_done.is_set():
        return "the broker closed the connection"
    return None


def run_shim(
    socket_path: Path,
    *,
    stdin: SupportsReadline | None = None,
    stdout: IO[bytes] | None = None,
    connect: Callable[[Path], LineChannel] = connect_unix,
) -> int:
    """Relay standard input and output to the broker's client socket; reads no configuration or secrets."""
    reader: SupportsReadline = sys.stdin.buffer if stdin is None else stdin
    writer: IO[bytes] = sys.stdout.buffer if stdout is None else stdout
    try:
        channel = connect(socket_path)
    except OSError as exc:
        print(
            f"switchboard: cannot reach the broker at {socket_path}: {exc.strerror or exc}", file=sys.stderr
        )
        return 1
    try:
        problem = relay(reader, writer, channel)
    finally:
        channel.close()
    if problem is not None:
        print(f"switchboard: {problem}", file=sys.stderr)
        return 1
    return 0

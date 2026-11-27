# Native broker and approval separation

Approval separation is only meaningful with separate operating-system accounts.
The native broker runs one C++ engine under a dedicated account that owns the configuration, credentials and database.
It serves two local Unix domain sockets: the assistant's account reaches only the client socket, and the operator's account reaches only the operator socket.
The [contract](../../contracts/README.md#isolation) states the rule both engines follow.

[Validation](../../docs/VALIDATION.md#current-status) records where the broker tests have run.
No broker or shim has been installed under separate accounts, and no service unit has been started.

## Principals

| Principal | Account                              | Reaches                                       | Holds                                    |
| --------- | ------------------------------------ | --------------------------------------------- | ---------------------------------------- |
| Broker    | Dedicated, for example `switchboard` | Configuration, credentials, database, sockets | Everything the engine can do             |
| Assistant | Its own account                      | The client socket, normally through the shim  | Nothing secret; it can only ask          |
| Operator  | Your account                         | The operator socket                           | Approval, reconciliation and kill switch |
| `root`    | Not contained                        | Everything                                    | Everything; no design here constrains it |

The broker account must not be an assistant account: an assistant running as the broker can read the credentials and database directly.
The broker prints a warning when its own account is listed as a client.

## Sockets

Both sockets carry one JavaScript Object Notation (JSON) message per line.
The client socket speaks the engine's assistant protocol, JSON-RPC 2.0 (JSON remote procedure call); the operator socket takes JSON objects with an `op` field.

| Socket   | Accounts        | Allows                                                            |
| -------- | --------------- | ----------------------------------------------------------------- |
| Client   | `client_uids`   | Tool listing and assistant-origin tool calls only                 |
| Operator | `operator_uids` | Status, approvals, approve, deny, journal, reconcile, kill switch |

The client socket passes each line to the engine's protocol handler, exactly as standard-input serving does: exposure lists, schemas, policy, approval queueing and the duplicate guard all apply, and an act-tier call creates a pending approval rather than running.
No client line can reach an operator function.
The operator socket does not speak the assistant protocol.

Operator requests follow the shared [operator socket protocol](../../contracts/README.md#operator-socket-protocol),
so one operator client works with the Python and native brokers.
One object per line:

| Request                                                                   | Result                                                                   |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `{"op":"status"}`                                                         | Engine and version, kill switch, pending approvals, open journal entries |
| `{"op":"approvals"}`                                                      | Pending approvals without arguments                                      |
| `{"op":"approval","id":"<approval>"}`                                     | One approval with its exact stored arguments                             |
| `{"op":"approve","id":"<approval>"}`, `{"op":"deny","id":"..."}`          | The execution outcome, or the denial                                     |
| `{"op":"actions"}`, with `"state"`, `"limit"` (1 to 100), or `"id"` alone | Journal entries; `id` returns one entry with arguments                   |
| `{"op":"reconcile","id":"<action>","resolution":"applied","note":"..."}`  | The reconciled entry                                                     |
| `{"op":"kill_switch"}`, optionally with `"enable": true` or `false`       | The kill-switch state                                                    |

Every request may carry `"request_id"`, an integer or text of at most 128 bytes, which is echoed back.
Unknown fields are refused.
A response is `{"ok": true, "op": ..., "request_id": ..., "result": <engine report>}` or `{"ok": false, "op": ..., "request_id": ..., "error": {"code": ..., "message": ...}}`.
`ok` means the broker processed the request; the engine report's own `status` still says whether, for example, an approval ran.

A malformed request, a malformed or unknown approval identifier, and a status the engine cannot report are error responses.
The status comes from the engine's `switchboard_status` tool as operator, so a policy must not deny that tool.
Review `approval` before `approve`: approving executes exactly the stored arguments.

## Peer checks

After accepting a connection and before reading from it, the broker asks the operating system for the peer's effective numeric user identifier: `SO_PEERCRED` on Linux, `getpeereid()` on macOS and the Berkeley Software Distribution (BSD) family.
Other systems have no supported facility, and the broker refuses to start there.
A peer is admitted only when its identifier is listed for that socket's role; otherwise the connection closes at once.
Refusals are logged with the numeric identifier, the first ten each and later ones at powers of two.

The identifier is the one the peer had when it connected.
Listed accounts are not checked again per request.

## Socket files

The broker binds the paths from its configuration, which must be absolute, normalized and short enough for a local socket address (107 bytes on Linux, 103 on macOS).
Before binding it:

* resolves symbolic links in the parent directory once, then requires that directory and every parent to belong to the broker account or `root`;
* refuses a socket directory that other accounts can write, and a writable parent unless it is sticky like `/tmp`;
* refuses an existing symbolic link, a file that is not a socket, another account's socket, or a socket on which a process is listening; it removes only its own stale socket.

Sockets are created with mode 660 and listen with a backlog of 16.
The file's group is the broker's group, or the directory's group when the directory has the set-group-identifier bit, as in the example below.
File permissions are a second layer; the peer check is the authorization.
Some BSD-derived systems ignore permissions on the socket file itself, so restrict the directory too.
On exit the broker removes a socket file only if it is still the one it created.

## Limits on connections and lines

| Limit               | Value                                                                               |
| ------------------- | ----------------------------------------------------------------------------------- |
| Client connections  | 16 at once; the operator's 4 are separate, so clients cannot exhaust them           |
| Request line        | 1 mebibyte (MiB); a longer line gets one error response, then the connection closes |
| Response line       | 32 MiB; a larger response is replaced by an error response                          |
| Work per connection | One line per pass; reading pauses until the previous response is written            |

The broker is single-threaded.
A slow tool call, bounded by `tool_timeout_seconds`, delays every other connection, including the operator's.
An approval runs its tool inside the broker before the operator gets the response.

## Stopping

`SIGINT` or `SIGTERM` cancels web transfers of the call in progress and stops the loop when that call returns.
An interrupted write is recorded as uncertain, never retried.
Open connections close without further replies.
A second signal ends the process at once; a socket file left behind that way is removed as stale on the next start.

## The shim

The assistant's Model Context Protocol (MCP) client starts the shim under the assistant's account:

```bash
switchboard-native serve-mcp --broker /run/switchboard-native/client/client.sock
```

It relays standard input to the socket and replies to standard output, reads no configuration, and holds no credentials.
Request lines are bounded at 1 MiB and reply lines at 32 MiB; a longer line stops it.
At the end of its input it tells the broker that no more requests follow, writes the remaining replies, and exits with status 0.
It exits with status 1 when the broker closes first, for example because the account is not listed.

## Dedicated-user service example

These are unexecuted instructions for a Linux host with systemd.
Create the `switchboard` account and the `switchboard-client` and `switchboard-operator` groups, for example with [the sysusers file](../../deploy/systemd/switchboard-sysusers.conf).
Add the assistant's account only to `switchboard-client` and your account only to `switchboard-operator`.
Never add one account to both.

Give the broker its own configuration, readable by the broker account, with a `broker` section.
Replace the example user identifiers with the output of `id -u` for each account:

```json
{
  "switchboard": {"connectors": [], "data_dir": "/var/lib/switchboard-native"},
  "mcp": {"expose": ["switchboard_status", "approvals_pending"]},
  "broker": {
    "client_socket": "/run/switchboard-native/client/client.sock",
    "operator_socket": "/run/switchboard-native/operator/operator.sock",
    "client_uids": [1001],
    "operator_uids": [1000]
  }
}
```

Install the [service unit](../../deploy/systemd/switchboard-native-broker.service) as `/etc/systemd/system/switchboard-native-broker.service`.
It runs `switchboard-native --config /etc/switchboard/native.json broker` and creates two socket directories, each owned by the broker, set-group-identifier and restricted to one group:

| Directory                          | Mode   | Group                  | Socket          |
| ---------------------------------- | ------ | ---------------------- | --------------- |
| `/run/switchboard-native`          | `0711` | `switchboard`          | None            |
| `/run/switchboard-native/client`   | `2750` | `switchboard-client`   | `client.sock`   |
| `/run/switchboard-native/operator` | `2750` | `switchboard-operator` | `operator.sock` |

Credential files named by `file:` references must belong to the broker account and be private to it.
Polling jobs do not run in the broker; if you need them, run `switchboard-native --config ... run` as a separate service under the broker account.

As the operator, any line-oriented local socket client works.
The shim relays any lines, so it can also carry operator requests:

```bash
printf '%s\n' '{"op":"approvals"}' |
  switchboard-native serve-mcp --broker /run/switchboard-native/operator/operator.sock
```

Read each pending approval with `{"op":"approval","id":"..."}` before deciding: its summary and arguments are text the assistant influenced, and approving executes exactly those arguments.

## Same-account caveat

Running the assistant and the operator under one account restores the old boundary: that account can reach both sockets, and the peer check cannot tell its processes apart.
The broker warns when an account is listed for both sockets.
Without a separate broker account, an assistant with your files or shell can also read the configuration and database.
Neither engine claims a sandbox in that configuration.

## Limits

* The peer check identifies accounts, not programs.
  A listed account can pass its connected socket to another of its processes, and any process of a listed account is trusted equally.
* Peers in another user namespace can appear as the overflow user, usually 65534; do not list it.
* Protocol session state, such as the legacy `initialize` handshake, belongs to the engine and is shared by all client connections.
* Connections have no idle timeout.
  A listed client account can hold the client slots, never the operator's.
* Backup and retention are not operator socket operations; run them as the broker account.
* Tool handlers run with the broker account's privileges.
  The broker separates who may approve, not what an approved tool can do.
* Linux and macOS are the targets; no other operating system has a peer-credential implementation here.

## Tests

[Broker cases](../tests/broker.cpp) cover line framing, response bounds, authorization with injected user identifiers, the peer-credential facility on an anonymous socket pair, operator-only enforcement through injected roles, and the shared operator protocol, including approval review, status contents and journal bounds.
They need no second account and open no listening socket.

# Isolation: approvals under separate accounts

In the default deployment everything runs under your own account.
That account can read every credential, edit the database, and approve its own requests, so the approval gate only constrains clients that respect it (see [the operating boundary](./DEPLOYMENT.md#the-operating-boundary)).
The broker moves the configuration, credentials, database, and audit log to a dedicated service account.
The assistant and the operator each get their own local socket, so approval separation rests on operating-system accounts instead of on the assistant's good behavior.

**Status.**
The broker, the operator client, the assistant shim, the systemd units, and their tests are authored but have not been run, installed, or started on any host.
Treat the commands and units below as a reviewed starting point, not a verified deployment.

**Same-account caveat.**
Separation exists only when the assistant and the operator use different accounts.
One account allowed on both sockets can approve its own requests; the broker refuses that configuration unless you pass `--allow-shared-account`, and even then it provides no separation.

## Principals

The assistant connects through a Model Context Protocol (MCP) client; the operator is the person deciding.

| Principal     | Typical account                       | Can                                                                               | Cannot                                                                                                         |
| ------------- | ------------------------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Service       | `switchboard`, dedicated              | Read configuration and secrets, run connectors, own the data, serve both sockets  | Nothing within Switchboard stops it                                                                            |
| Assistant     | the account that runs your MCP client | List and call exposed tools through the client socket; read approval status       | Read configuration, secrets, or data; use the operator socket; approve, deny, reconcile, or toggle the switch  |
| Operator      | your login account                    | Review and decide approvals, reconcile uncertain actions, toggle the kill switch  | Read secrets or the data directory, unless it is also the service account or an administrator                  |
| Administrator | `root`, or any account with `sudo`    | Everything                                                                        | It is outside the boundary; nothing here constrains it                                                         |

## Threat model

With separate accounts, the broker is meant to stop a prompt-injected or compromised assistant that has a shell under its own account from:

* reading credentials, the configuration, the database, or the audit log;
* approving or denying its own requests, reconciling an uncertain action, or switching the kill switch off;
* calling tools outside `[mcp] expose`, or calling any tool as anything other than the `assistant` origin.

Another local account that is in neither socket group is refused by the socket file's permissions, and again by the peer check if those permissions were too broad.

It does not protect against:

* the administrator, the service account, or anything running as either, including connectors and the runner;
* an operator who approves a harmful request: approval executes exactly the stored arguments, so read them;
* disclosure through allowed tools: read results reach the assistant and, for a hosted model, its provider;
* an assistant that can act as the operator account, for example through a shared account, `sudo`, an unlocked terminal, or a forwarded agent socket;
* resource exhaustion: tool calls and operator requests run one at a time, so a busy assistant can delay the operator's requests until the running call's deadline; sessions are capped at 16 per socket;
* defects in the kernel, the Python interpreter, or Switchboard itself.

## How the broker enforces the boundary

1. Both sockets are created with a temporary umask of 177 (mode 600), handed to their group, then set to mode 660, so neither is ever wider than intended.
2. For each accepted connection the broker reads the peer's numeric user identifier from the kernel: `SO_PEERCRED` on Linux and `LOCAL_PEERCRED` on macOS.
   The kernel records it when the peer connects.
   The identifier must be listed in `client_uids` for the client socket or `operator_uids` for the operator socket.
3. A refused peer receives one error line.
   The broker logs the first ten refusals and then only every power of two; operator-socket refusals logged that way are also written to the audit log as `broker.refused`.
4. The client socket gives each connection its own MCP session over newline-delimited JavaScript Object Notation Remote Procedure Call (JSON-RPC) messages.
   It lists and calls only exposed tools, always with the `assistant` origin.
   No operator operation exists there.
5. Every call into the application, from either socket, runs under one lock: connectors are not thread-safe,
   so two tool calls never run at once.
6. A line longer than 1 mebibyte (MiB) ends that connection after one error line.
7. On `SIGTERM` or `SIGINT` the broker stops accepting, lets the running operation finish for up to `[switchboard] tool_timeout_seconds` plus 30 seconds, closes every connection, and removes its sockets.

Startup also refuses a socket directory that is a link, that is not owned by the service account or `root`, or that group or others can write.
It never replaces a symbolic link or a file that is not a socket, and it removes a leftover socket only if the service account owns it and nothing accepts connections on it.

## Setup on Linux with systemd

These steps assume `assistant` is the account running your MCP client and that you are the operator.
Run them from a checkout of this repository.

1. Install Switchboard into a directory that only `root` can change:

   ```bash
   sudo git clone "$SWITCHBOARD_REPO_URL" /opt/switchboard
   cd /opt/switchboard && sudo uv sync
   ```

2. Create the service account and the two socket groups, then add the assistant and yourself.
   A new group membership applies from that account's next login.

   ```bash
   sudo install -m 644 deploy/systemd/switchboard-sysusers.conf /etc/sysusers.d/switchboard.conf
   sudo systemd-sysusers
   sudo usermod -aG switchboard-client assistant
   sudo usermod -aG switchboard-operator "$USER"
   ```

   Without `systemd-sysusers`, create the same principals by hand:

   ```bash
   sudo useradd --system --home-dir / --shell /usr/sbin/nologin switchboard
   sudo groupadd --system switchboard-client
   sudo groupadd --system switchboard-operator
   sudo usermod -aG switchboard-client,switchboard-operator switchboard
   ```

3. Write the configuration as the service account's private file.
   `doctor` reports a configuration that other accounts can read.

   ```bash
   sudo install -d -m 700 -o switchboard -g switchboard /etc/switchboard
   sudo install -m 600 -o switchboard -g switchboard \
       src/switchboard/config.example.toml /etc/switchboard/config.toml
   sudoedit /etc/switchboard/config.toml
   ```

   Set the data directory and the broker table, using `id -u assistant` and `id -u` for the identifiers:

   ```toml
   [switchboard]
   connectors = []
   data_dir = "/var/lib/switchboard"

   [mcp]
   expose = ["switchboard_status", "approvals_pending", "approvals_status", "actions_recent"]

   [broker]
   client_socket = "/run/switchboard/client.sock"
   operator_socket = "/run/switchboard/operator.sock"
   client_uids = [1001]
   operator_uids = [1000]
   ```

4. Store secrets as files the service account owns with mode 600, because `file:` references refuse anything else, and reference them as `file:/etc/switchboard/secrets/<name>`:

   ```bash
   sudo install -d -m 700 -o switchboard -g switchboard /etc/switchboard/secrets
   sudo install -m 600 -o switchboard -g switchboard \
       /dev/null /etc/switchboard/secrets/mail-app-password
   sudoedit /etc/switchboard/secrets/mail-app-password
   ```

   `cmd:` references run as the service account inside the unit's sandbox, so a password manager tied to your desktop session will not answer them.

5. Install and start the units, then check the result:

   ```bash
   sudo install -m 644 deploy/systemd/switchboard-broker.service /etc/systemd/system/
   sudo install -m 644 deploy/systemd/switchboard-runner.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now switchboard-broker.service switchboard-runner.service
   sudo -u switchboard /opt/switchboard/.venv/bin/switchboard --config /etc/switchboard/config.toml doctor
   ls -l /run/switchboard
   ```

   The units use `ProtectHome=yes`, so connector folders such as finance and export import directories must live outside `/home`, for example under `/var/lib/switchboard`.

6. Point the assistant's MCP client at the shim.
   It relays standard input and output to the client socket and reads no configuration, data directory, or secrets:

   ```json
   {
     "mcpServers": {
       "switchboard": {
         "command": "/opt/switchboard/.venv/bin/switchboard",
         "args": ["serve-mcp", "--broker", "/run/switchboard/client.sock"]
       }
     }
   }
   ```

7. Decide from your own account.
   `--socket PATH` or `SWITCHBOARD_OPERATOR_SOCKET` selects another socket:

   ```bash
   switchboard operator status
   switchboard operator approvals list
   switchboard operator approvals show 1a2b3c4d
   switchboard operator approvals approve 1a2b3c4d
   switchboard operator actions list --state uncertain
   switchboard operator actions resolve "$ACTION_ID" --not-applied --note "not in sent mail"
   switchboard operator kill-switch on
   ```

   `approve` shows the stored arguments and asks for confirmation on a terminal; `--yes` confirms explicitly.
   Behind the broker, the quoted `switchboard approvals approve <id>` and `switchboard actions resolve <id>` hints are written in their `switchboard operator ...` forms, because only the service account can run the direct commands.
   That covers tool results, errors stored with an approval and shown later by `approvals_status`, and the MCP instructions.
   Custom `[mcp] instructions` get the same rewrite of those quoted forms; other text is left alone.

Backups, exports, retention, and audit archives run as the service account against its data directory; see [the data lifecycle guide](./DATA_LIFECYCLE.md).

## Socket permissions

| Path                             | Owner         | Group                  | Mode | Admitted                                                        |
| -------------------------------- | ------------- | ---------------------- | ---- | --------------------------------------------------------------- |
| `/run/switchboard`               | `switchboard` | `switchboard`          | 711  | Traversal only; nobody else can list or create entries          |
| `/run/switchboard/client.sock`   | `switchboard` | `switchboard-client`   | 660  | Group members, then only identifiers listed in `client_uids`    |
| `/run/switchboard/operator.sock` | `switchboard` | `switchboard-operator` | 660  | Group members, then only identifiers listed in `operator_uids`  |

The broker unit lists both socket groups in `SupplementaryGroups=` because an account can only hand a file to a group it belongs to.
Without `--client-group` and `--operator-group`, both sockets keep the service account's primary group.

## Operator protocol

Both engines accept the same requests, defined in [the cross-engine contract](../contracts/README.md#operator-socket-protocol): one JavaScript Object Notation (JSON) object per line each way, with `op`, an optional `request_id` that is echoed back, and only the fields that operation lists.
Unknown fields and nested argument objects are refused.

```json
{"op": "approval", "id": "1a2b3c4d", "request_id": 1}
{"ok": true, "op": "approval", "request_id": 1, "result": {"id": "1a2b3c4d", "status": "pending", "args": {"to": "friend@example.invalid"}}}
```

The result above is abbreviated.
The Python broker answers each operation as follows:

| `op`          | Fields                                          | `result`                                                                                     |
| ------------- | ----------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `status`      | none                                            | `{"status": "ok", "tool": "switchboard_status", "data": {...}}`: engine, kill switch, counts |
| `approvals`   | none                                            | `{"status": "ok", "tool": "approvals_pending", "data": [...]}`, oldest first, at most 100    |
| `approval`    | `id`                                            | The stored approval with its exact `args`                                                    |
| `approve`     | `id`                                            | The execution outcome, as a tool call returns it                                             |
| `deny`        | `id`                                            | `{"id": ..., "status": "denied"}`, or an outcome with `status` of `error` or `invalid`       |
| `actions`     | `state`, `limit` (1 to 100), or `id` alone      | `{"status": "ok", "tool": "actions", "actions": [...]}`, or `"action"` with its arguments    |
| `reconcile`   | `id`, `resolution`, `note`                      | An outcome with the reconciled `action`; nothing is repeated, reversed, or verified          |
| `kill_switch` | optional boolean `enable`                       | `{"status": "ok", "tool": "kill_switch", "kill_switch": ..., "changed": ...}`                |

`ok: false` means the request could not be processed as written and nothing changed.
Its `error.code` is `invalid` (malformed, unknown, or missing fields, a wrong field type, or an unknown approval for `approval`), `limit` (a line over 1 MiB), `unavailable` (shutting down), `error` (an application failure such as unreadable state), `internal`, `forbidden` (a refused peer), or `busy` (too many connections).
`ok: true` means the operation ran; like a tool outcome, its result can still report a refusal through `status`, for example approving an approval that is no longer pending.
Approve, deny, reconcile, and kill-switch changes write audit records with the peer's user and process identifiers.

## What remains trusted

* The administrator and the service account, with everything they run: the broker, the runner, connectors,
  the Python interpreter, and the installed Switchboard code.
* The operator account and whoever can use it.
* The kernel's peer-credential report and file permissions, and the group memberships you assign.
* The account providers, devices, and model provider that tools talk to.
* Client sessions: an allowed assistant can still read whatever the exposed read tools return.

## macOS status

Peer credentials are read through `LOCAL_PEERCRED` and decoded as `struct xucred` from Apple's published headers; macOS reports no process identifier there.
This path is untested.
No launchd job definition is provided: create the account and groups yourself, create a socket directory owned by the service account with mode 711 before each start, and keep the configuration and secrets private to that account.
Other systems, including Windows and the Berkeley Software Distribution (BSD) family, are refused at startup rather than trusted.

## Limits

* Nothing here has been executed; the tests are authored for the reader's own machine.
* One operation at a time means one slow provider call delays every other call until its deadline.
* The broker does not poll, process events, or run rules; the runner unit does, under the same account.
* Separate accounts protect the credentials and the decision, not the data that allowed tools return.
* Running both principals under one account is supported only with `--allow-shared-account` and gives no separation.

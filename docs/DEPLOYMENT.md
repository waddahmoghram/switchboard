# Deployment and operation

Switchboard is meant to run on a computer you own, under your own user account.
Nothing in it needs root.

## The operating boundary

The policy gate controls calls through Switchboard, not arbitrary programs running as your user.
The `human` origin and `approvals approve --yes` are command-line conveniences, not proof that a person made the request.
An assistant with unrestricted shell or file access under the same account can approve itself, remove the kill-switch file, read credentials, or bypass Switchboard entirely.

Use the gate only with clients you trust to respect this boundary.
Stronger separation requires separate operating-system accounts: the optional broker keeps the configuration, credentials, and data under a dedicated service account and gives the assistant and the operator separate local sockets.
It is authored but not yet exercised; see [Isolation](./ISOLATION.md) for its setup and limits.
The tool exposure list is also not a multi-user data-permission system: the approval queue is shared by processes using the same data directory.

Self-hosting removes the aggregator, not the model provider.
A hosted assistant can receive tool results containing mail, contacts, purchases, and ledger entries.
Use a local model and a client configured not to upload those results when local-only processing is required.
Never grant accounts or payment credentials merely because the test suite uses fakes.

## Install

The commands below install the full Python reference.
For the C++-first native engine and C/Rust clients,
use [the separate native build and coverage guide](../native/README.md).
Do not point native configuration,
databases or approval commands at Python state, or assume all account connectors are available natively.
No native program or test has been executed on the authoring host.

Requirements: Python 3.11 or newer (for `tomllib`) on Linux or macOS.
Windows is unsupported in this prototype:
the audit log and runner lock depend on `fcntl`,
and without it Switchboard refuses to start before it writes anything,
rather than write the audit log unlocked.
Runtime and platform compatibility remain untested.

```bash
git clone "$SWITCHBOARD_REPO_URL" switchboard   # your private copy of this repository
cd switchboard
uv sync                     # creates .venv and installs Switchboard in editable mode
uv run switchboard --version
```

Without `uv`, use `python3 -m venv .venv`, then `.venv/bin/pip install -e .`.

## First run

```bash
uv run switchboard init     # writes ~/.config/switchboard/config.toml (mode 600)
"${EDITOR:-nano}" ~/.config/switchboard/config.toml
uv run switchboard doctor   # checks permissions, secret references, and the audit chain
uv run switchboard tools    # lists tools with their tier and default decision
```

Enable one connector at a time.
Store each credential with your password manager and reference it, for example `password = "cmd:pass show switchboard/mail-app-password"`, or in a file you `chmod 600`.

`init` creates a private configuration folder when needed but does not change an existing parent folder's permissions.
It refuses symbolic links and folders writable by other users.
A new configuration is published without overwriting a concurrent initializer; `--force` replaces the file only after the new private file has been written.
It replaces the whole configuration, not just missing settings, so keep a copy before using it.
Use a dedicated directory for `data_dir`, not a workspace or home directory whose permissions you want preserved.

Core section and setting names are checked: `mcp.expos` is an error, not an omitted exposure list that exposes every tool.
Connector-specific settings still follow each connector's own validation.

## Run the tests

The authored tests target fakes and local servers bound to `127.0.0.1`, not real accounts.
None have been run on the authoring host.
Run them on your own machine before enabling connectors, ideally with outbound network access blocked except for loopback.
Static checks do not prove provider compatibility.

```bash
uv run pytest -m "not integration"   # unit tests
uv run pytest -m integration         # local servers, subprocesses, end-to-end flows
uv run ruff check src tests
uv run mypy
```

There is no automated multi-platform test pipeline or live-provider certification yet.
A clean static check is not a passing unit test, an integration run, or a successful sign-in to a real account.

## Connect an assistant

The Model Context Protocol (MCP) server talks over standard input and output, so the assistant starts it as a child process.
No network port is opened.
Expose only the tools you want with `[mcp] expose` in the configuration.
An omitted exposure list exposes all registered tools; `expose = []` exposes none.
A malformed list is a configuration error.
Restart the runner and assistant-owned server processes after editing configuration;
the kill-switch file is checked dynamically and does not require a restart.

These are source-checked configuration recipes, not end-to-end integrations tested here.
A model runner by itself does not connect tools: the assistant client must implement MCP.
A hosted chat interface that cannot launch a local subprocess needs a separate bridge; Switchboard does not implement a remote MCP endpoint.

**Claude Desktop** (Settings, then Developer, then Edit Config; the file is `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and `~/.config/Claude/claude_desktop_config.json` on Linux):

```json
{
  "mcpServers": {
    "switchboard": {
      "command": "/home/you/switchboard/.venv/bin/switchboard",
      "args": ["serve-mcp"]
    }
  }
}
```

**OpenClaw**: the current registry documentation confirms `--command` and `--arg`.
Registry mutations require the local Gateway to be stopped.
`add` normally connects to the server before saving; `--no-probe` saves the definition without starting it.
Use this non-executing setup command first:

```bash
openclaw mcp add switchboard \
  --command /home/you/switchboard/.venv/bin/switchboard \
  --arg serve-mcp \
  --no-probe
```

On your own machine, after testing Switchboard, `openclaw mcp probe switchboard` starts it and checks tool discovery.
The client runtime and its tool filters still decide which saved servers it consumes.

**Hermes Agent** (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  switchboard:
    command: "/home/you/switchboard/.venv/bin/switchboard"
    args: ["serve-mcp"]
```

When an assistant asks to act, it receives an approval id.
Review and decide from a terminal:

```bash
uv run switchboard approvals list
uv run switchboard approvals show 1a2b3c4d
uv run switchboard approvals approve 1a2b3c4d
```

Sources (checked 2026-11-11):
[Connect to local MCP servers](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers), [OpenClaw registry](https://github.com/openclaw/openclaw/blob/main/docs/cli/mcp/registry.md), and the [Hermes Agent MCP guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md).

## Add messaging and personal exports

These are opt-in, independent capabilities:

* [Telegram](./connectors/telegram.md): the runner receives allowlisted bot messages; your assistant pulls the inbox and submits approval-gated replies.
  Fill both numeric allowlists before enabling it.
  An incoming message does not launch a model or approve an action.
* [LinkedIn](./connectors/linkedin.md): import your own downloads, search them, and prepare local post drafts.
  Live publishing is disabled until you supply your own authorized application token, actor identifier, and version.
  Local drafts created without a configured actor are not a shortcut to publishing later as a different actor.
* [WhatsApp exports](./connectors/whatsapp.md): import a manually exported chat.
  Choose the actual date order explicitly; the empty starter value refuses to guess.
  The timezone is context, not proof of an exact offset.
  This is not a live WhatsApp account connection.

Merge the relevant tables from the starter configuration rather than appending duplicate `[switchboard]` or `[mcp]` tables.
Add names to your existing connector list; do not replace unrelated enabled connectors.
The Telegram allowlists and WhatsApp date order are intentionally incomplete until you configure them.

Keep export folders owned and private (mode 700) and export files at mode 600.
Do not select a shared Downloads directory as an import root.
Files and archive members are size-bounded and never extracted to the filesystem.
Imports remain historical snapshots and emit summaries, not a live event per old message.

If `[mcp] expose` is present, deliberately add the new tools you want the assistant to access.
Restart its server after changing that list.
A hosted client can receive imported private messages through these tools; no platform connector makes its model local automatically.

Only one process or assistant may consume updates for a Telegram bot.
If OpenClaw or Hermes already owns that bot's Telegram frontend, leave this receiver disabled for that bot and use the assistant's existing frontend with Switchboard tools instead.

## Add local devices, notes, tasks, and monitoring

Enable one capability at a time after the offline tests pass on your own machine:

* [Home Assistant](./connectors/home.md) supplies the broader device gateway, including supported lighting, climate, fan, speaker and sensor integrations.
  It is separate software you operate, not installed or started by Switchboard.
  Use reviewed target lists and per-device limits.
* [WLED](./connectors/wled.md) and [Shelly](./connectors/shelly.md) provide direct local hooks without the gateway.
  Configure fixed endpoints yourself; these are not network discovery or arbitrary command tools.
  Plain local transport, when needed, requires explicit consent and an appropriately trusted network.
* [Documents](./connectors/documents.md) reads bounded text from selected private collections.
  It is not permission to scan your home directory, work files, or shared folders.
* [Tasks](./connectors/tasks.md) needs no account.
  Reminder events require `switchboard run`; add an explicit notification rule and configure the notification connector if you want delivery.
* [Monitoring](./connectors/monitoring.md) reads fixed health endpoints, backup reports and disk observations.
  You supply report producers; no backup, restore, remote shell or host-management program is executed by this connector.

Merge the relevant namespaced tables and add names to the existing connector/exposure lists, not duplicate root tables.
The starter still enables no connectors.
Keep device controls disabled until their exact targets, capabilities, transport and approval behavior have been checked.

Do not treat a device acknowledgement as completion or an old healthy snapshot as proof a service remains healthy.
Keep manual overrides and device safety controls.
Do not expose locks, garage doors, alarms, unreviewed scripts/scenes, or high-power appliances merely because a generic integration supports them.
Safety profiles belong to their configured control path, not a universal device firewall.
Do not expose a second, broader path to the same device if you depend on the narrower profile's restrictions.
The new home profiles refuse generic calls on their configured control targets; unprofiled legacy configurations keep the original behavior.
New home and native lighting/plug actions bind the reviewed configuration, so changing targets or limits requires reading a new binding before requesting an action.

## Run the background runner

`switchboard run` runs enabled polling/monitoring jobs, queues due task reminders, processes events,
and runs your rules.
LinkedIn and WhatsApp export imports do not run automatically.
Run one per data directory: events are not claimed across processes, so a second `switchboard run` exits with "another 'switchboard run' is already processing events" while the first holds `runner.lock` there.
As a systemd user service on Linux (`~/.config/systemd/user/switchboard.service`):

```ini
[Unit]
Description=Switchboard runner

[Service]
ExecStart=%h/switchboard/.venv/bin/switchboard run
Restart=on-failure
RestartSec=30
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now switchboard.service
journalctl --user -u switchboard.service -f
```

Each job run has a deadline (`[switchboard] job_timeout_seconds`, default 300), and up to `job_workers` (default 4) jobs run at once, never two from the same connector.
Web requests inside a run are cut off at the deadline; other code cannot be interrupted, so a run stuck past it is reported and not restarted until it returns.
Intervals are minimum spacing, not maximum response or reminder delays.
The first `SIGINT` or `SIGTERM` stops scheduling and waits for running jobs within their deadlines; a second exits immediately.
`switchboard run --once` runs every job once and exits with status 1 when a job failed, got stuck, or did not start.
See [Recovery](./RECOVERY.md#the-runner).

For separate service, assistant, and operator accounts, use the system units in [deploy/systemd](../deploy/systemd/) instead of this user unit; [Isolation](./ISOLATION.md) walks through them.

The webhook receiver is a separate, optional process (`switchboard webhooks serve`).
It binds to `127.0.0.1`;
see [webhooks](./connectors/webhooks.md) before exposing it through a tunnel.

## Back up, revoke, and stop

* Back up with `switchboard backup create DIR`: it copies the database through SQLite's online backup interface and the audit log at a record boundary while the runner keeps working, then writes a manifest.
  Check a copy with `switchboard backup verify DIR`, and keep the printed audit head somewhere Switchboard cannot write.
  Back up `~/.config/switchboard/` separately; backups never include configuration or secrets.
  Both contain personal data: encrypt them.
  See [Data lifecycle](./DATA_LIFECYCLE.md).
* Block non-read tool calls with `switchboard kill-switch on`.
  This does not stop polling, read tools, price fetches, or inbound webhooks.
  Stop their processes or disable their connectors to stop that activity.
* Revoke at the source: delete app passwords, revoke OAuth grants, rotate bot tokens and webhook secrets, and remove the long-lived Home Assistant token.
  Then delete the matching secret references.
* Check the record: `switchboard audit verify` and `switchboard audit tail -n 50`.

## Failures, retries, and retained data

```bash
uv run switchboard events --failed -n 50
uv run switchboard actions list --state uncertain
uv run switchboard approvals list --all
uv run switchboard audit tail -n 50
```

`events --failed` shows subscriber or rule exceptions without event bodies.
A tool returning a denied or error outcome to a rule is recorded separately as `rule.fired` in the audit log.
Neither path retries automatically.
Do not blindly replay a partially processed event: another subscriber or an earlier action may already have succeeded.

Every draft and act call is recorded in the action journal before it runs.
A call whose effect is unknown, including one interrupted by a crash, becomes `uncertain`, and an identical request is refused until you check the provider or device and record the result with `switchboard actions resolve ID --applied` or `--not-applied`.
Resolving records your decision; it never repeats or reverses anything.
See [Recovery](./RECOVERY.md#reconciliation-workflow).
A process interrupted during a rate-limited call leaves an in-flight reservation that counts until its one-hour window passes.

Retention is explicit.
`switchboard data purge` applies the `[retention]` periods to tool-call rows, approvals, and finished journal entries, and connector hooks remove connector data only for connectors you list under `[retention.connectors]`; `--dry-run` counts first.
It never removes open journal entries or approvals that can still be decided, and it is not scheduled.
Processed event payloads are pruned after `event_retention_days` (14 by default), keeping deduplication keys.
Data without a hook, such as mail cursors, contact caches, and imported files, is not purged.
Original bank statements in `processed/` and `failed/` can contain full account numbers even though the ledger masks them; see [Data lifecycle](./DATA_LIFECYCLE.md#retention).

Audit verification detects changed records and interior deletions, but cannot detect removal of a valid tail unless you compare with an independently saved head hash, such as the one `backup create` prints.
It cannot stop the same user rewriting the entire chain.
`switchboard audit-archive rotate` archives a long log and keeps the chain linked.

Backup reference: [SQLite online backup interface](https://www.sqlite.org/backup.html), checked 2026-11-11.

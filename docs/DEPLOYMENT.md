# Deployment and operation

Switchboard is meant to run on a computer you own, under your own user account. Nothing in it needs root.

## Install

Requirements: Python 3.11 or newer (for `tomllib`) on Linux or macOS. Windows is untested: the audit log uses
`fcntl` file locks.

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

Enable one connector at a time. Store each credential with your password manager and reference it, for example
`password = "cmd:pass show switchboard/mail-app-password"`, or in a file you `chmod 600`.

## Run the tests

The tests never contact real accounts; they use fakes and local servers bound to `127.0.0.1`.

```bash
uv run pytest -m "not integration"   # unit tests
uv run pytest -m integration         # local servers, subprocesses, end-to-end flows
uv run ruff check src tests
uv run mypy
```

## Connect an assistant

The MCP server talks over standard input and output, so the assistant starts it as a child process. No network
port is opened. Expose only the tools you want with `[mcp] expose` in the configuration.

**Claude Desktop** (Settings, then Developer, then Edit Config; the file is
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and
`~/.config/Claude/claude_desktop_config.json` on Linux):

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

**OpenClaw** (`openclaw mcp add` checks the server before saving it; the `--command` and `--arg` flags were
confirmed against OpenClaw 2026.11.3's help output):

```bash
openclaw mcp add switchboard \
  --command /home/you/switchboard/.venv/bin/switchboard \
  --arg serve-mcp
```

**Hermes Agent** (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  switchboard:
    command: "/home/you/switchboard/.venv/bin/switchboard"
    args: ["serve-mcp"]
```

When an assistant asks to act, it receives an approval id. Review and decide from a terminal:

```bash
uv run switchboard approvals list
uv run switchboard approvals show 1a2b3c4d
uv run switchboard approvals approve 1a2b3c4d
```

Sources (checked 2026-11-11): [Connect to local MCP servers](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers)
and the [Hermes Agent MCP guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md).

## Run the background runner

`switchboard run` polls mailboxes and calendars, checks prices, processes events, and runs your rules. Run
one per data directory: events are not claimed across processes, so a second `switchboard run` exits with
"another 'switchboard run' is already processing events" while the first holds `runner.lock` there. As a
systemd user service on Linux (`~/.config/systemd/user/switchboard.service`):

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

The webhook receiver is a separate, optional process (`switchboard webhooks serve`). It binds to `127.0.0.1`;
see [webhooks](./connectors/webhooks.md) before exposing it through a tunnel.

## Back up, revoke, and stop

* Back up `~/.config/switchboard/` and the data directory (`~/.local/share/switchboard/` by default). They contain
  personal data: encrypt the backup.
* Stop everything that acts: `switchboard kill-switch on`. Reads keep working; every other call is denied.
* Revoke at the source: delete app passwords, revoke OAuth grants, rotate bot tokens and webhook secrets, and
  remove the long-lived Home Assistant token. Then delete the matching secret references.
* Check the record: `switchboard audit verify` and `switchboard audit tail -n 50`.

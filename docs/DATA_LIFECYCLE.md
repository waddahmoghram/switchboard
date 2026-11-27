# Data lifecycle: backups, exports, retention, and audit archives

The data directory (`~/.local/share/switchboard` by default, `/var/lib/switchboard` under [the broker](./ISOLATION.md)) holds the SQLite database `switchboard.sqlite3` with its write-ahead log companions, the hash-chained `audit.jsonl`, rotated audit segments in `audit-archive/`, `runner.lock`, an optional `KILL_SWITCH` file, and connector files such as the mail connector's token files.
The commands below back it up, check and restore those backups, export it, apply retention, and rotate and anchor the audit log, while the runner and assistants keep working.

**Status.**
[Validation](./VALIDATION.md#current-status) records where the tests of these commands and of the connector retention hooks have run; the commands have not been used on a real data directory.
Nothing in this guide is a measured result.

| Command                                                    | Loads the configuration       | Effect                                                                        |
| ---------------------------------------------------------- | ----------------------------- | ----------------------------------------------------------------------------- |
| `switchboard backup create DIR`                            | Yes                           | Copies the database, audit log, and archives into a new private directory     |
| `switchboard backup verify DIR [--expect-audit-head HASH]` | No                            | Checks a backup read-only                                                     |
| `switchboard backup restore DIR [--target DATA_DIR]`       | Only for the default target   | Verifies, then restores into a new or empty directory with the kill switch on |
| `switchboard data export FILE`                             | Yes                           | Writes a private JavaScript Object Notation (JSON) export                     |
| `switchboard data purge [--dry-run]`                       | Yes                           | Applies `[retention]`, or counts what it would remove                         |
| `switchboard audit-archive rotate`                         | Yes                           | Archives the audit log and starts a new, linked one                           |
| `switchboard audit-archive verify`                         | Yes                           | Verifies the live log and every archive it links                              |
| `switchboard audit anchor [--notify]`                      | Yes                           | Prints the latest record's position and hash; `--notify` also sends them      |
| `switchboard audit verify --anchor SEQ:HASH`               | Yes                           | Verifies the linked chain and fails if an anchored record is gone or changed  |

`backup verify` and `backup restore` never build the application, because building it creates a database in the data directory a restore needs empty.
Under the broker, run these commands as the service account, for example:

```bash
sudo -u switchboard install -d -m 700 /var/lib/switchboard/backups
sudo -u switchboard /opt/switchboard/.venv/bin/switchboard --config /etc/switchboard/config.toml \
    backup create /var/lib/switchboard/backups/2026-11-11
```

## Backups

`backup create` copies:

* the database through SQLite's online backup interface, which takes a consistent snapshot while other processes keep writing.
  The copy is switched to rollback-journal mode so it can be opened read-only later;
* the audit log and every file in `audit-archive/`, while holding the audit log's lock, so each copy ends at a record boundary and the recorded head matches it.
  Audit appends, and so tool calls, wait during the copy;
* a `manifest.json` describing the copy.

The directory must not exist yet; it is created with mode 700 and every file with mode 600.
Configuration, secret files, the mail connector's OAuth (delegated authorization) token files, calendar outbox files, connector import folders, and `runner.lock` are not copied.
The kill-switch state is recorded, but a restore always switches it on.

The database snapshot and the audit copy happen in the same pause of audit appends, but they are not one transaction: a change and its audit record can fall on either side of a backup.

The manifest records versions, sizes, digests, and counts, never secret values.
Its Secure Hash Algorithm 256 (SHA-256) entries are lowercase hexadecimal; this example abbreviates them:

```json
{
  "audit": {"head": "5f0c…", "records": 1234},
  "created_at": "2026-11-11T23:30:00+00:00",
  "engine": "python",
  "files": {
    "audit-archive/audit-20261101T000000Z-1a2b3c4d5e6f7a8b.jsonl": {"bytes": 524288, "sha256": "9e1d…"},
    "audit.jsonl": {"bytes": 2048, "sha256": "c4a7…"},
    "switchboard.sqlite3": {"bytes": 1048576, "sha256": "07b2…"}
  },
  "format": "switchboard-backup",
  "format_version": 1,
  "kill_switch": false,
  "schema_versions": {"core": 2, "tasks": 1},
  "switchboard_version": "0.1.0"
}
```

`backup create` prints the audit head and the manifest's own SHA-256 digest.
Keep both somewhere the Switchboard accounts cannot write, so a rewritten backup can be detected later.
A backup holds message text, approval arguments, the ledger, and other personal data in clear text: encrypt it before it leaves the machine, and delete old backups deliberately, because retention never reaches them.

## Verifying a backup

`backup verify` changes nothing.
It checks that:

1. the directory is a real directory, warning when another account owns it or can read it;
2. `manifest.json` is a backup manifest of a supported format version;
3. only the database, the audit log, and archive-named regular files are present, with none missing, none unlisted, and no links;
4. every file matches its SHA-256 digest and size in the manifest;
5. the database opens read-only, `PRAGMA integrity_check` reports `ok`, `PRAGMA foreign_key_check` finds nothing, and its schema versions match the manifest and are all known to this Switchboard;
6. the audit copy's chain verifies and ends at the recorded head and record count, and at `--expect-audit-head` when you pass one;
7. each archive the audit log links exists, matches the recorded digest, verifies, and ends at the head the link records.
   Archives that nothing links are reported as warnings.

| Verification shows                                                       | It does not show                                                                                      |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| The copy is internally consistent and matches its manifest               | That the manifest is authentic: whoever can rewrite the whole backup can recompute every digest       |
| The database is structurally sound, with schema versions this code knows | That its contents are what you expect, or that they suit your current configuration                   |
| The audit chain is unbroken and ends at the recorded head                | That no valid tail was cut before the backup, unless you compare a head recorded independently        |
| Linked archives are intact                                               | That providers, devices, calendars, or mailboxes still match what the backup describes                |

## Restoring

`backup restore` verifies the backup first and restores nothing if verification fails.
The target must be a new directory, created with mode 700, or an existing empty one that you own; an existing one is tightened to mode 700.
Only `runner.lock` may already be there, and the restore is refused while a runner holds that lock.
Each copied file is checked against the verified digest.
The restore creates `KILL_SWITCH` and appends a `backup.restored` record to the restored audit log, so nothing acts until you have reviewed pending approvals and uncertain actions.
Without `--target`, the restore uses the configured `data_dir`.

To replace the live data directory under the broker:

```bash
sudo systemctl stop switchboard-broker.service switchboard-runner.service
sudo mv /var/lib/switchboard /var/lib/switchboard.before-restore
sudo install -d -m 700 -o switchboard -g switchboard /var/lib/switchboard
sudo -u switchboard /opt/switchboard/.venv/bin/switchboard \
    backup restore /srv/switchboard-backups/2026-11-11 --target /var/lib/switchboard
sudo systemctl start switchboard-broker.service switchboard-runner.service
switchboard operator approvals list
switchboard operator actions list --state uncertain
switchboard operator kill-switch off
```

Restore the configuration and secrets separately.
Copy `mail/oauth/` back from the old directory, or authorize the mail accounts again.
A backup written by a newer Switchboard is refused, because its schema versions are newer than this code understands; an older backup is migrated when the restored directory is first opened.

## Exporting your data

`data export FILE` writes a JSON file with mode 600 from one consistent database snapshot.
It refuses to overwrite an existing file.
The export contains:

* approvals with their stored arguments, results, and errors;
* action journal entries with their arguments, without the internal owner token;
* events with their payloads, and tool-call budget rows;
* each key-value namespace with its key count, total characters, and last update, but no values, because those include cursors and connector state;
* the tables that enabled connectors offer through their retention modules, listed below;
* `problems`, naming any connector module that could not be used.

An export is for reading and portability, not a backup: it cannot be restored.
It contains message text and other personal data, and writing it is recorded as `data.exported` in the audit log.

## Retention

| Setting                              | Default | Removes once older than the period                                                                   | Never removes                                          |
| ------------------------------------ | ------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `[retention] tool_calls_days`        | 30      | Finished tool-call budget rows                                                                       | Rows still marked running                              |
| `[retention] approvals_days`         | 90      | Denied, expired, executed, or failed approvals, and pending ones that expired; by decision or expiry | Approvals that can still be approved, or are executing |
| `[retention] actions_days`           | 180     | Succeeded, failed, and reconciled journal entries                                                    | `in_flight` and `uncertain` entries                    |
| `[switchboard] event_retention_days` | 14      | Processed events; one with a deduplication key keeps the key and loses its payload                   | Unprocessed events                                     |
| `[retention.connectors] <name>`      | Not set | Whatever that connector's hook removes, below                                                        | Every connector without an entry                       |

`data purge --dry-run` runs the same statements inside transactions that it rolls back, so its counts match an apply made at that moment; the finance hook counts files instead of deleting them.
A real purge writes a `data.purged` audit record.
Purging is not scheduled: run it yourself, or from a timer you add.
The runner separately prunes processed events every six hours.
Retention never touches backups, exports, or audit archives.

Connector data is purged only for an enabled connector with an explicit `[retention.connectors]` entry, in whole days from 1 to 3650:

```toml
[retention.connectors]
telegram = 14
finance = 30
```

| Connector  | Removes                                                                                                          | Keeps                                                              |
| ---------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `telegram` | Stored inbox messages, with chat and sender identifiers                                                          | Messages whose reply is being sent or has an unknown outcome       |
| `tasks`    | Completed and cancelled tasks, by their last change                                                              | Open tasks; no `tasks.deleted` events are emitted                  |
| `webhooks` | Processed `webhook.received` bodies, emptied when they carry a deduplication key; expired replay entries         | Unprocessed deliveries and every other source's events             |
| `finance`  | Original statement files in `processed/` and `failed/` whose modification and status-change times are both older | The ledger, other names, hidden files, and links                   |
| `shopping` | Price observations                                                                                               | Each watch's newest observation, orders, shipments, and watches    |
| `linkedin` | Imported records and import markers, dated by the import that first stored them                                  | Post drafts and publication attempts                               |
| `whatsapp` | Imported records and import markers, dated by the import that first stored them                                  | Export files in the import folder                                  |

The same modules offer these tables to `data export`: `telegram_messages`, `tasks_items`, the four finance ledger tables, the four shopping tables, the LinkedIn import, record, draft, and publication tables, and the WhatsApp import and record tables.

A configured entry for an unknown connector, or for an enabled connector without a hook, is reported as a problem and the command exits with status 1; an entry for a disabled connector is skipped.

### Writing a retention hook

A connector opts in by adding `switchboard/connectors/<name>/retention.py`; copy the imports from [the tasks hook](../src/switchboard/connectors/tasks/retention.py):

```python
EXPORT_TABLES = ("example_items",)  # optional; only the connector's own tables


def purge(ctx: AppContext, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
    with ctx.db.session() as conn, purge_transaction(conn, dry_run=dry_run):
        removed = conn.execute(
            "DELETE FROM example_items WHERE state = 'closed' AND updated_at < ?", (iso(older_than),)
        ).rowcount
    return {"example_items": removed}
```

A hook must not contact the network, must leave open or in-progress work alone, must count without changing anything when `dry_run` is true, and returns whole-number counts.
`purge_transaction` comes from `switchboard.lifecycle`; it commits, or rolls back for a dry run.
Database hooks run in one transaction; a hook that removes files, like finance, removes them one at a time and counts failures.

## What audit records keep

An audit record keeps the fields its event passes, such as a tool call's arguments or an approval's summary, after `audit.py` redacts them.
The rules apply as a record is written, so changing them never alters existing records or their hashes, and older logs still verify.
A tool without its own `summarize` gets an approval summary quoting its arguments after the same redaction, while `switchboard approvals show` still prints the exact arguments.

* **Credential keys.**
  A value under a key that contains `pass`, `secret`, `token`, `authorization` or `authorisation`, `cookie`, `api_key`, `credential`, or `private_key` (with `_`, `-`, or nothing between the words) becomes `[redacted]`.
  So does a value under a key with a segment, split at punctuation and case changes, of `otp`, `totp`, `hotp`, `mfa`, `2fa`, `pin`, `session`, `private`, `auth`, `cvv`, or `cvc`.
  Segments match whole, so `card_pin`, `userPin`, and `session_id` are redacted, while `pinned`, `spinner`, `mapping`, `sessions`, and `author` are kept.
* **Secret-shaped text.**
  Every other string, at any depth and under any key, loses the secrets that the rules below recognize.
* **Long text.**
  A string still longer than 300 characters keeps its first 120 characters, its length, and the first 16 hexadecimal digits of its SHA-256 hash, as in `…[1000 chars, sha256:0123456789abcdef]`.
  Scrubbing runs first, so the prefix holds no recognized secret, and the length and hash describe the scrubbed text, so they cannot confirm a guess at a removed secret.

The scrubbing rules run in this order:

1. Private keys in Privacy-Enhanced Mail (PEM) form, from `-----BEGIN … PRIVATE KEY-----` to the matching end line or the end of the text, become `[redacted:private-key]`.
2. JSON Web Tokens (JWTs), three dot-separated parts of which the first two start with `eyJ`, become `[redacted:jwt]`.
3. A password in a web address, as in `postgres://alice:…@db.example.com`, becomes `[redacted]`.
4. The values of query parameters named `access_token`, `refresh_token`, `id_token`, `token`, `api_key`, `apikey`, `key`, `secret`, `client_secret`, `password`, `passwd`, `auth`, `sig`, `signature`, `X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`, `X-Goog-Signature`, or `X-Goog-Credential` become `[redacted]`.
5. Labeled values such as `password: hunter2`, `api_key = 'k-1'`, or `"token": "…"` become `[redacted]` up to the next space or the closing quote, for the labels `password`, `passwd`, `passphrase`, `passcode`, `secret`, `client_secret`, `api_key`, `access_token`, `refresh_token`, `auth_token`, `token`, `otp`, and `pin`.
6. The values of `Authorization`, `Proxy-Authorization`, `X-Api-Key`, and `X-Auth-Token` headers, and of `Cookie` and `Set-Cookie` headers that hold a `name=value` pair, become `[redacted]` up to their closing quote or the end of the line, keeping a leading `Basic`, `Bearer`, `Digest`, `Negotiate`, or `token`.
7. A token after `Bearer` becomes `[redacted]` when it has at least 16 characters and a digit, or at least 32 characters.
8. GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, and `github_pat_`) become `[redacted:github-token]`, Slack tokens (`xoxa-`, `xoxb-`, `xoxp-`, `xoxr-`, and `xoxs-`) become `[redacted:slack-token]`, and Amazon Web Services (AWS) access key identifiers (`AKIA` or `ASIA` and 16 capitals or digits) become `[redacted:aws-key-id]`.
9. Keys with a digit that start with `sk-`, `sk_live_`, `sk_test_`, `rk_live_`, `rk_test_`, or `AIza` become `[redacted:api-key]`, and the secret half of a Telegram bot token (`123456789:…`) becomes `[redacted]`.
10. Runs of 32 or more hexadecimal characters with at least one digit and one letter become `[redacted:hex]`.
    Runs of 40 or more letters, digits, `-`, and `_` become `[redacted:base64]` when they mix capitals, lower case, and digits in pieces that average under three characters, as random tokens do.

The last rule skips values under identifier keys: keys whose last segment is `id`, `ids`, `uid`, `uuid`, `hash`, `digest`, `sha1`, `sha256`, `sha512`, `checksum`, `fingerprint`, `head`, `binding`, `etag`, `revision`, `action`, or `target`.
Those keys hold action and message identifiers, approval bindings, and digests; the other rules still apply to them.

Ordinary words, abbreviated commit hashes, universally unique identifiers (UUIDs) with their dashes, file names, and CamelCase or snake_case names are kept.
A full 40-character commit hash or a 64-character SHA-256 digest in free text is redacted.
Known false positives are the word after a label such as `password:` in prose, the rest of a line that uses `Authorization:` as a label, keys such as `mfa_enabled`, and long names made mostly of short numbers and single letters, such as `2025-11-26T12-00-00-backup-Archive-File-v3`.
The rules miss secrets without a recognizable shape: an unlabeled password in a sentence, standard base64 with `+` and `/` (it looks like a file path), random text in one letter case that is not hexadecimal, random runs shorter than the thresholds, and a secret split across fields.

### Keeping only a hash of message text

`[audit] hash_only` names fields whose text the log keeps only as a length and hash, such as `[11 chars, sha256:…]`, instead of a prefix:

```toml
[audit]
hash_only = ["body", "text", "message", "summary"]
```

Names match at any depth and in any letter case, so `body` also covers `args.body`; numbers and true-or-false values are kept.
These four names cover the mail `body`, the Telegram, LinkedIn, and home announcement `text`, the notification `message`, and approval summaries, which quote message text.
They also cover other fields with those names, such as the mail search `text` and the calendar event `summary`; add `description`, `notes`, and `note` for calendar descriptions, task notes, and shopping notes.
Up to 64 names are allowed, and an empty name or one with surrounding spaces is refused.
The hash is an unkeyed SHA-256 prefix: it can match a record to a message you hold, and short or predictable text can be confirmed by guessing.
The setting applies to new records only, and the approval queue in the database still stores exact arguments and summaries until [retention](#retention) removes them.

## Audit archives

`audit-archive rotate` holds the log's exclusive lock while it copies the log into `audit-archive/` and verifies the copy, refusing a broken chain without changing anything.
The archive is named `audit-<time>-<head prefix>.jsonl`, with the time in Coordinated Universal Time (UTC), mode 600, in a directory of mode 700.
Rotation then truncates the live log and writes one record:

```json
{"archive": "audit-20261111T233000Z-5f0c1d2e3f4a5b6c.jsonl", "archive_bytes": 524288, "archive_sha256": "9e1d…", "hash": "…", "kind": "audit.rotated", "prev": "000…", "previous_head": "5f0c…", "previous_records": 1234, "ts": "2026-11-11T23:30:00+00:00"}
```

Its `prev` is the genesis value, so `switchboard audit verify` checks the new log on its own.
The link to the archive lives in `previous_head`, `previous_records`, and `archive_sha256`, all covered by the record's hash.
Appends from other processes wait on the lock and continue the new chain.

`audit-archive verify` verifies the live log, then follows each rotation record to its archive, checking the digest, the archive's own chain, its final head, and its record count, and reports archives that nothing links as warnings.
A crash during rotation leaves the complete archive in place.
Before truncation, the live log still holds every record; after it, the live log restarts without a linking record, or rarely ends in an incomplete line that `switchboard audit verify` reports.
Verification reports the unlinked archive.

Rotation keeps the same tamper evidence as before: whoever can rewrite the live log and its archives can recompute every hash.
[Audit anchors](#audit-anchors) keep a record's position and hash somewhere those accounts cannot write, and they name the same record across rotations.
Backups include the archives, and a restore brings them back.

## Audit anchors

The hash chain reveals changed, reordered, and deleted records everywhere except at the end: a log whose last records were cut off is still a valid chain.
An anchor closes that gap up to the record it names.
It is the position and hash of one record, kept where the accounts that can write the log cannot.

`switchboard audit anchor` verifies the live log and every archive it links while appends wait, then prints the latest record as `SEQ:HASH`, alone on standard output:

```text
1234:5f0c1d2e3f4a5b6c7d8e9fa0b1c2d3e4f5061728394a5b6c7d8e9fa0b1c2d3e4
```

`SEQ` counts from the first record of the oldest linked archive, rotation records included, so an anchor names the same record after later rotations.
The command refuses a chain that does not verify or has no records, and it writes nothing to the audit log.
With `--notify`, it also sends the anchor at low priority to every channel of the [notify connector](./connectors/notify.md), and exits with status 1 when no channel accepts it.

`switchboard audit verify --anchor SEQ:HASH` verifies the same linked chain, then checks each anchor; repeat `--anchor` to check several.
It fails when the chain has fewer than `SEQ` records, because records were cut from its end or the log was replaced, and when record `SEQ` has a different hash, because the chain was rewritten at or before that record.
A chain that does not verify, for example because a linked archive is missing, fails without its anchors being checked.
Without `--anchor`, `audit verify` checks only the live log, whose record count restarts at each rotation, so take anchors from `audit anchor` rather than from its output.

### Keeping and checking anchors

Keep anchors where neither the account that runs Switchboard nor the assistant's account can change or delete them.
In the direct setup these are the same account, so send anchors off the machine: to your phone with `--notify`, or to an inbox that Switchboard is not configured to read.
Under [the broker](./ISOLATION.md), the service account can add entries to the system journal but cannot change them, so the journal qualifies too, as do files only you can write and a remote syslog server.
A notify channel's history is only as far out of reach as the credentials Switchboard holds for it: the Telegram Bot API, for example, lets a bot delete messages it sent less than 48 hours ago.

Under the broker, `/etc/cron.d/switchboard-anchor` can record an anchor in the journal every hour:

```text
17 * * * * switchboard /opt/switchboard/.venv/bin/switchboard --config /etc/switchboard/config.toml audit anchor 2>&1 | logger -t switchboard-anchor
```

The job's errors, such as a chain that does not verify, land in the journal under the same tag.
To check every anchor the service account recorded, run this as root or as a member of a group that may read the system journal, such as `systemd-journal`:

```bash
journalctl --identifier=switchboard-anchor _UID="$(id -u switchboard)" --output=cat \
    | grep -E '^[0-9]+:[0-9a-f]{64}$' \
    | sed 's/^/--anchor=/' \
    | sudo -u switchboard xargs /opt/switchboard/.venv/bin/switchboard \
        --config /etc/switchboard/config.toml audit verify
```

The `_UID` match keeps only entries the service account wrote, so other accounts cannot add false alarms.
The journal drops its oldest entries at its size and age limits, which a flood of entries from any local account can reach, so forward it to another host or check more often than it rotates.

In the direct setup on Linux, a line in your own crontab (`crontab -e`) sends an anchor every hour:

```text
17 * * * * $HOME/switchboard/.venv/bin/switchboard audit anchor --notify
```

On macOS, save this as `~/Library/LaunchAgents/local.switchboard.anchor.plist`, replacing `you` with your account name:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.switchboard.anchor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/you/switchboard/.venv/bin/switchboard</string>
        <string>audit</string>
        <string>anchor</string>
        <string>--notify</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
</dict>
</plist>
```

Then load it:

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/local.switchboard.anchor.plist
```

To check anchors you received, pass them to `switchboard audit verify`, one `--anchor` each.
Check every anchor, or at least the newest one that passed your previous check and every anchor recorded since.
A matching anchor shows that every record up to it is unchanged, because each record's hash covers the one before it, and each rotation record covers its archive.
A forged anchor, such as a `notify_self` note with the same title, can only make a check fail; it cannot hide a cut while the genuine anchors are checked too.

Anchors have limits:

* Records after the newest anchor can still be cut without a trace; anchoring more often narrows that window.
* Whoever controls both the log and every place its anchors are kept can cut or rewrite the log and replace the anchors to match.
* Whoever can stop the scheduled job stops new anchors, so notice when they stop arriving.
* Positions depend on every linked archive: while one is missing, `audit anchor` refuses and `audit verify --anchor` fails.
* A restored backup ends where the backup did, so anchors taken after the backup fail against it; start a new list after a restore and keep the old one.

## Limits

* The tests use temporary directories and synthetic data, not a real data directory.
* Backups and exports are clear text; encrypting and storing them elsewhere is up to you.
* Audit redaction recognizes secrets by key name and shape only; a secret without a recognized shape stays in the record, as [What audit records keep](#what-audit-records-keep) lists.
* The finance hook relies on file-system times.
  Where a rename does not update the status-change time, a statement downloaded long ago becomes eligible soon after it is imported.
* Data without a hook is not purged, including mail cursors, calendar and contact caches, home and monitoring snapshots, LinkedIn drafts, and imported files in connector folders.
* A large database lengthens the pause of audit appends, and so of tool calls, during `backup create`.

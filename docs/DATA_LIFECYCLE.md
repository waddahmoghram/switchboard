# Data lifecycle: backups, exports, retention, and audit archives

The data directory (`~/.local/share/switchboard` by default, `/var/lib/switchboard` under
[the broker](./ISOLATION.md)) holds the SQLite database `switchboard.sqlite3` with its write-ahead log
companions, the hash-chained `audit.jsonl`, rotated audit segments in `audit-archive/`, `runner.lock`, an
optional `KILL_SWITCH` file, and connector files such as the mail connector's token files. The commands below
back it up, check and restore those backups, export it, apply retention, and rotate the audit log, while the
runner and assistants keep working.

**Status.** These commands, the connector retention hooks, and their tests are authored but have not been run
on any host. Nothing in this guide is a measured result.

| Command                                                    | Loads the configuration       | Effect                                                                        |
| ---------------------------------------------------------- | ----------------------------- | ----------------------------------------------------------------------------- |
| `switchboard backup create DIR`                            | Yes                           | Copies the database, audit log, and archives into a new private directory     |
| `switchboard backup verify DIR [--expect-audit-head HASH]` | No                            | Checks a backup read-only                                                     |
| `switchboard backup restore DIR [--target DATA_DIR]`       | Only for the default target   | Verifies, then restores into a new or empty directory with the kill switch on |
| `switchboard data export FILE`                             | Yes                           | Writes a private JavaScript Object Notation (JSON) export                     |
| `switchboard data purge [--dry-run]`                       | Yes                           | Applies `[retention]`, or counts what it would remove                         |
| `switchboard audit-archive rotate`                         | Yes                           | Archives the audit log and starts a new, linked one                           |
| `switchboard audit-archive verify`                         | Yes                           | Verifies the live log and every archive it links                              |

`backup verify` and `backup restore` never build the application, because building it creates a database in
the data directory a restore needs empty. Under the broker, run these commands as the service account, for
example:

```bash
sudo -u switchboard install -d -m 700 /var/lib/switchboard/backups
sudo -u switchboard /opt/switchboard/.venv/bin/switchboard --config /etc/switchboard/config.toml \
    backup create /var/lib/switchboard/backups/2026-11-11
```

## Backups

`backup create` copies:

* the database through SQLite's online backup interface, which takes a consistent snapshot while other
  processes keep writing. The copy is switched to rollback-journal mode so it can be opened read-only later;
* the audit log and every file in `audit-archive/`, while holding the audit log's lock, so each copy ends at a
  record boundary and the recorded head matches it. Audit appends, and so tool calls, wait during the copy;
* a `manifest.json` describing the copy.

The directory must not exist yet; it is created with mode 700 and every file with mode 600. Configuration,
secret files, the mail connector's OAuth (delegated authorization) token files, calendar outbox files,
connector import folders, and `runner.lock` are not copied. The kill-switch state is recorded, but a restore
always switches it on.

The database snapshot and the audit copy happen in the same pause of audit appends, but they are not one
transaction: a change and its audit record can fall on either side of a backup.

The manifest records versions, sizes, digests, and counts, never secret values. Its Secure Hash Algorithm 256
(SHA-256) entries are lowercase hexadecimal; this example abbreviates them:

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

`backup create` prints the audit head and the manifest's own SHA-256 digest. Keep both somewhere the
Switchboard accounts cannot write, so a rewritten backup can be detected later. A backup holds message text,
approval arguments, the ledger, and other personal data in clear text: encrypt it before it leaves the
machine, and delete old backups deliberately, because retention never reaches them.

## Verifying a backup

`backup verify` changes nothing. It checks that:

1. the directory is a real directory, warning when another account owns it or can read it;
2. `manifest.json` is a backup manifest of a supported format version;
3. only the database, the audit log, and archive-named regular files are present, with none missing, none
   unlisted, and no links;
4. every file matches its SHA-256 digest and size in the manifest;
5. the database opens read-only, `PRAGMA integrity_check` reports `ok`, `PRAGMA foreign_key_check` finds
   nothing, and its schema versions match the manifest and are all known to this Switchboard;
6. the audit copy's chain verifies and ends at the recorded head and record count, and at `--expect-audit-head`
   when you pass one;
7. each archive the audit log links exists, matches the recorded digest, verifies, and ends at the head the
   link records. Archives that nothing links are reported as warnings.

| Verification shows                                                       | It does not show                                                                                      |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| The copy is internally consistent and matches its manifest               | That the manifest is authentic: whoever can rewrite the whole backup can recompute every digest       |
| The database is structurally sound, with schema versions this code knows | That its contents are what you expect, or that they suit your current configuration                   |
| The audit chain is unbroken and ends at the recorded head                | That no valid tail was cut before the backup, unless you compare a head recorded independently        |
| Linked archives are intact                                               | That providers, devices, calendars, or mailboxes still match what the backup describes                |

## Restoring

`backup restore` verifies the backup first and restores nothing if verification fails. The target must be a
new directory, created with mode 700, or an existing empty one that you own; an existing one is tightened to
mode 700. Only `runner.lock` may already be there, and the restore is refused while a runner holds that lock.
Each copied file is checked against the verified digest. The restore creates `KILL_SWITCH` and appends a
`backup.restored` record to the restored audit log, so nothing acts until you have reviewed pending
approvals and uncertain actions. Without `--target`, the restore uses the configured `data_dir`.

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

Restore the configuration and secrets separately. Copy `mail/oauth/` back from the old directory, or
authorize the mail accounts again. A backup written by a newer Switchboard is refused, because its schema
versions are newer than this code understands; an older backup is migrated when the restored directory is
first opened.

## Exporting your data

`data export FILE` writes a JSON file with mode 600 from one consistent database snapshot. It refuses to
overwrite an existing file. The export contains:

* approvals with their stored arguments, results, and errors;
* action journal entries with their arguments, without the internal owner token;
* events with their payloads, and tool-call budget rows;
* each key-value namespace with its key count, total characters, and last update, but no values, because
  those include cursors and connector state;
* the tables that enabled connectors offer through their retention modules, listed below;
* `problems`, naming any connector module that could not be used.

An export is for reading and portability, not a backup: it cannot be restored. It contains message text and
other personal data, and writing it is recorded as `data.exported` in the audit log.

## Retention

| Setting                              | Default | Removes once older than the period                                                                   | Never removes                                          |
| ------------------------------------ | ------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `[retention] tool_calls_days`        | 30      | Finished tool-call budget rows                                                                       | Rows still marked running                              |
| `[retention] approvals_days`         | 90      | Denied, expired, executed, or failed approvals, and pending ones that expired; by decision or expiry | Approvals that can still be approved, or are executing |
| `[retention] actions_days`           | 180     | Succeeded, failed, and reconciled journal entries                                                    | `in_flight` and `uncertain` entries                    |
| `[switchboard] event_retention_days` | 14      | Processed events; one with a deduplication key keeps the key and loses its payload                   | Unprocessed events                                     |
| `[retention.connectors] <name>`      | Not set | Whatever that connector's hook removes, below                                                        | Every connector without an entry                       |

`data purge --dry-run` runs the same statements inside transactions that it rolls back, so its counts match an
apply made at that moment; the finance hook counts files instead of deleting them. A real purge writes a
`data.purged` audit record. Purging is not scheduled: run it yourself, or from a timer you add. The runner
separately prunes processed events every six hours. Retention never touches backups, exports, or audit
archives.

Connector data is purged only for an enabled connector with an explicit `[retention.connectors]` entry, in
whole days from 1 to 3650:

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

The same modules offer these tables to `data export`: `telegram_messages`, `tasks_items`, the four finance
ledger tables, the four shopping tables, the LinkedIn import, record, draft, and publication tables, and the
WhatsApp import and record tables.

A configured entry for an unknown connector, or for an enabled connector without a hook, is reported as a
problem and the command exits with status 1; an entry for a disabled connector is skipped.

### Writing a retention hook

A connector opts in by adding `switchboard/connectors/<name>/retention.py`; copy the imports from
[the tasks hook](../src/switchboard/connectors/tasks/retention.py):

```python
EXPORT_TABLES = ("example_items",)  # optional; only the connector's own tables


def purge(ctx: AppContext, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
    with ctx.db.session() as conn, purge_transaction(conn, dry_run=dry_run):
        removed = conn.execute(
            "DELETE FROM example_items WHERE state = 'closed' AND updated_at < ?", (iso(older_than),)
        ).rowcount
    return {"example_items": removed}
```

A hook must not contact the network, must leave open or in-progress work alone, must count without changing
anything when `dry_run` is true, and returns whole-number counts. `purge_transaction` comes from
`switchboard.lifecycle`; it commits, or rolls back for a dry run. Database hooks run in one transaction; a
hook that removes files, like finance, removes them one at a time and counts failures.

## Audit archives

`audit-archive rotate` holds the log's exclusive lock while it copies the log into `audit-archive/` and
verifies the copy, refusing a broken chain without changing anything. The archive is named
`audit-<time>-<head prefix>.jsonl`, with the time in Coordinated Universal Time (UTC), mode 600, in a
directory of mode 700. Rotation then truncates the live log and writes one record:

```json
{"archive": "audit-20261111T233000Z-5f0c1d2e3f4a5b6c.jsonl", "archive_bytes": 524288, "archive_sha256": "9e1d…", "hash": "…", "kind": "audit.rotated", "prev": "000…", "previous_head": "5f0c…", "previous_records": 1234, "ts": "2026-11-11T23:30:00+00:00"}
```

Its `prev` is the genesis value, so `switchboard audit verify` checks the new log on its own. The link to the
archive lives in `previous_head`, `previous_records`, and `archive_sha256`, all covered by the record's hash.
Appends from other processes wait on the lock and continue the new chain.

`audit-archive verify` verifies the live log, then follows each rotation record to its archive, checking the
digest, the archive's own chain, its final head, and its record count, and reports archives that nothing links
as warnings. A crash during rotation leaves the complete archive in place. Before truncation, the live log
still holds every record; after it, the live log restarts without a linking record, or rarely ends in an
incomplete line that `switchboard audit verify` reports. Verification reports the unlinked archive.

Rotation keeps the same tamper evidence as before: whoever can rewrite the live log and its archives can
recompute every hash. Record the new head after each rotation somewhere those accounts cannot write. Backups
include the archives, and a restore brings them back.

## Limits

* Nothing here has been executed; the tests are authored for the reader's own machine.
* Backups and exports are clear text; encrypting and storing them elsewhere is up to you.
* The finance hook relies on file-system times. Where a rename does not update the status-change time, a
  statement downloaded long ago becomes eligible soon after it is imported.
* Data without a hook is not purged, including mail cursors, calendar and contact caches, home and monitoring
  snapshots, LinkedIn drafts, and imported files in connector folders.
* A large database lengthens the pause of audit appends, and so of tool calls, during `backup create`.

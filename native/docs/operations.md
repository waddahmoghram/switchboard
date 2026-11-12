# Native operations: journal, deadlines, runner, state and recovery

This guide covers the operational behavior of the C++ native engine: the durable action journal and
reconciliation, deadlines and cancellation, bounded runner ticks, state versions, backup and verification,
retention, and the kill switch. It implements the native side of
[contract revision 1](../../contracts/README.md); where this guide and the contract differ, the contract wins.
Requests, reports and configuration use JavaScript Object Notation (JSON).

All of this is **authored but unrun**. The code was checked only with compiler syntax checks; it has not been
built, linked or executed, and the tests listed in [Validation status](#validation-status) have not been run.
Nothing here is evidence of runtime behavior, migration of real data, device compatibility or a physical effect.

## Action journal

The engine records every **non-read** execution attempt (draft and act tiers; spending never executes) in the
`native_actions` table before the handler runs. Read calls, pending approvals, and refusals before execution
(unknown or unexposed tool, schema failure, policy, kill switch, hourly budget, duplicate guard) have no entry.

| State                    | Entered when                                                                  |
| ------------------------ | ----------------------------------------------------------------------------- |
| `in_flight`              | Recorded atomically with the sweep, duplicate check and budget check          |
| `succeeded`              | The handler returned a report without declaring its effect unknown            |
| `failed`                 | The handler raised an error that asserts the effect did not happen            |
| `uncertain`              | The effect might or might not have happened                                   |
| `reconciled_applied`     | The operator confirmed that an uncertain effect happened                      |
| `reconciled_not_applied` | The operator confirmed that an uncertain effect did not happen                |

Every executed non-read call returns `action` in its outcome. The shape below is illustrative:

```json
{"status":"ok","tool":"tasks_add","data":{"id":4},"action":{"id":"<32 lowercase hexadecimal>","state":"succeeded"}}
```

`action.state` is the stored state after the call. It can be `in_flight` only if the journal itself could not
record the result (for example, a storage failure); treat that as unknown. The protocol host also sets
`isError` for any action state other than `succeeded`, so a returned report whose effect is unknown is flagged.

### Classifying a result

| Handler result                                                                      | Journal state | Outcome `status`                           |
| ----------------------------------------------------------------------------------- | ------------- | ------------------------------------------ |
| Returns an object with top-level `"effect_unknown": true`                           | `uncertain`   | `ok`                                       |
| Returns any other report                                                            | `succeeded`   | `ok`                                       |
| Returns a report that is not valid data or exceeds the 8 MiB output limit           | `uncertain`   | `error`                                    |
| Raises `Error` with code `uncertain`, `transport`, `limit`, `cancelled`, `deadline` | `uncertain`   | `error`                                    |
| Raises `Error` with any other code                                                  | `failed`      | `error` (`invalid` for the code `invalid`) |
| Raises any other exception                                                          | `uncertain`   | `error`                                    |

Raising an `Error` with an ordinary code asserts that the effect did not happen. Adapters must not do that after
a write may have been delivered: they return `"effect_unknown": true` or raise the code `uncertain`. Adapters
that wrap transport failures in their own codes must follow the same rule. The WLED and Shelly write reports
set `"effect_unknown": true` whenever their `status` is `uncertain`, and always include a boolean `confirmed`
that is `true` only when `status` is `state_matched` (a matching software readback, never physical proof); see
the [device guide](./devices.md). If the request audit record cannot be written, the handler never starts, the
attempt is recorded as `failed`, and the storage error is returned to the caller.

### Stored fields

`native_actions` stores the attempt identifier (32 lowercase hexadecimal characters, enforced by a check
constraint), tool, canonical argument digest, arguments, origin, linked approval identifier, state, owner,
`started_at`, `deadline_at`, `finished_at`, `error`, `resolved_at` and `resolution_note`. Times are integer
seconds and are rendered as `YYYY-MM-DDTHH:MM:SSZ` in reports.

The digest is the SHA-256 (Secure Hash Algorithm, 256-bit) digest of the canonical JSON arguments, computed
before redaction. The stored arguments are redacted like the audit log: values under keys containing
`password`, `secret`, `token`, `authorization` or `cookie` become `[REDACTED]`. Error text is bounded printable
text. The owner is the process identifier plus a random value, created once per `Runtime` instance.

### Duplicate guard

Before recording a new attempt, the engine looks for an attempt with the same tool and digest in state
`in_flight` or `uncertain`. If one exists, the call returns `denied` and the message names the blocking action:
an in-flight attempt asks you to wait for it to finish or be swept, and an uncertain one requires
reconciliation. Failed and succeeded attempts never block a repeat. Approvals are checked when they execute:
a blocked approval finishes as `failed`, with the blocking message as its error, and cannot be replayed.
Executions of approvals are journaled with origin `human` and the approval identifier. Approving executes
exactly the stored arguments, so review them first with `approvals show ID` (`Runtime::approval`), which also
shows the approval's current state and any stored result or error.

### Sweeps and late results

A sweep marks an `in_flight` attempt `uncertain` once its `deadline_at` is more than 60 seconds in the past.
Sweeps run when a runtime is constructed, inside the transaction before every duplicate check, and at the start
of every runner tick; each sweep that changes rows writes an `action.swept` audit record. An attempt left by a
process that ended mid-call therefore becomes uncertain instead of blocking forever.

The owner runtime may still record its own late result for an attempt that a sweep marked uncertain, until the
operator reconciles it; an operator decision is never overwritten. No other runtime can finish the attempt.

### Reconciliation

Only the operator reconciles: the native command line, `Runtime::reconcile`, the C interface or the native
broker's operator socket. There is no assistant tool for it. Only `uncertain` attempts can be reconciled, as
`applied` or `not_applied`, with a note of 1 to 500 characters after surrounding whitespace is trimmed; control,
line-separator, bidirectional-formatting and zero-width characters are refused. Either resolution lifts the
guard. Reconciling records a human decision; it never repeats, reverses or verifies the effect. A reconcile
request sweeps first, so an attempt abandoned past its deadline can be reconciled without waiting for a tick.

Assistants can read the journal through the read tool `actions_recent` (`state` and `limit` 1-100), which omits
arguments and owners and reports open counts. `switchboard_status` also reports `open_actions` and the
`schema_version`. The operator view (`Runtime::actions`) accepts `{"state"?, "limit"?}` (limit 1-100, default
20) or `{"id"}`; a single entry includes its stored arguments and owner.

## Deadlines and cancellation

| Setting                            | Range   | Default | Meaning                                                  |
| ---------------------------------- | ------- | ------- | -------------------------------------------------------- |
| `switchboard.tool_timeout_seconds` | 5-3600  | 120     | Deadline for one tool execution, including approved ones |
| `switchboard.job_timeout_seconds`  | 5-3600  | 300     | Deadline for one polling-job run                         |
| `switchboard.tick_budget_seconds`  | 30-3600 | 600     | Native only: time within which a tick starts due jobs    |
| `switchboard.job_workers`          | -       | -       | Python only; the native engine refuses it                |

An attempt's `deadline_at` is its start plus `tool_timeout_seconds` on the runtime clock. The runtime installs
per-thread execution limits around every handler and polling job, so adapters do not pass them explicitly;
`current_execution_limits()` returns them. The web transport then:

* refuses to start a request once the operation is cancelled (code `cancelled`) or past its deadline (code
  `deadline`), before any connection is made;
* clamps its connect and total timeouts to the smaller of the request's own limit and the remaining time, using
  the millisecond forms of `CURLOPT_CONNECTTIMEOUT` and `CURLOPT_TIMEOUT` (at least 1 ms);
* installs `CURLOPT_XFERINFOFUNCTION` with `CURLOPT_NOPROGRESS` set to 0. libcurl calls it frequently during a
  transfer and about once per second when idle; it aborts the transfer with `cancelled` or `deadline`.

A timeout caused by the clamp is reported as `deadline`; a request's own shorter timeout stays `transport`.
These codes classify as uncertain, because a write may already have been delivered. The engine does not
interrupt other handler work, such as local file or database writes, and never abandons a write midway.

`Runtime::cancel()` (and the C interface's `sb_runtime_cancel`) increments a lock-free counter and returns at
once. Each tool call, approval execution and runner tick records the counter when it starts and counts as
cancelled once it changes. Cancellation aborts web transfers of the operation in progress and makes a tick
defer its remaining jobs. A cancel with nothing running affects nothing, and later operations start fresh.
A cancelled write is uncertain. Because the counter is lock-free, the command-line runner calls it from a signal
handler.

## Runner ticks

Each tick expires stale approvals, sweeps the journal, applies event retention, and then runs due jobs.

Jobs run **serially**: adapters share one SQLite connection and one context, so concurrent jobs could interleave
transactions on that connection. The runtime lock is taken per job, so operator calls can run between jobs.

* **Order.** Due jobs run most overdue first (earliest `next_run`); equally overdue jobs keep registration
  order.
* **Budget.** A job starts only while the tick's elapsed time is below `tick_budget_seconds`, measured on the
  monotonic clock and on the runtime clock, whichever has advanced more. Unreached jobs stay due and are
  listed in `deferred`. A started job keeps its own deadline, so a tick can last up to the budget plus one job
  deadline, plus any handler work that the engine does not interrupt.
* **Backoff.** After failure *n* (n >= 1) the next run waits `interval * 2^(n - 1)`, capped at one hour or at
  the interval if that is longer; a success resets it. For a 60-second job: 60, 120, 240, 480, 960, 1920, then
  3600 seconds.
* **Overruns.** A run that took longer than `job_timeout_seconds` counts as an overrun. Its own result still
  decides whether it failed.

The tick report lists `jobs` (`name`, `error`, `duration_ms`, `overran`, `consecutive_failures`, `next_run`),
`deferred`, `budget_seconds`, `budget_exhausted`, `cancelled` and `swept_actions`. The `jobs_status` tool
reports, per job, `interval_seconds`, `next_run`, `error`, `consecutive_failures`, `overruns`,
`last_started_at`, `last_duration_ms` and `timeout_seconds`.

## State versions and migration

The state database records its schema in SQLite's `user_version`. This engine writes version 2.

| Found                                | Behavior                                                                              |
| ------------------------------------ | ------------------------------------------------------------------------------------- |
| Empty file (version 0, no tables)    | Creates version 2 directly                                                            |
| Version 0 with tables                | Refused: unversioned state is not migrated                                            |
| Version 1                            | Migrated in one transaction: adds `native_actions` and its three indexes              |
| Version 1 lacking a core table       | Refused as incomplete                                                                 |
| Version 1 that already has a journal | Refused rather than guessing its history                                              |
| Version 2                            | Opened; refused as incomplete if any of the five tables is missing                    |
| Version 3 or later                   | Refused: restore a matching backup or upgrade instead of downgrading                  |

Migration is one way. Before the first start of this version, stop every process that uses the data directory
and copy it, including any `native-state.sqlite3-wal` and `native-state.sqlite3-shm` files. Native state is not
the Python database, and neither engine reads the other's files.

## Backup and verification

`backup create DIR` (`Runtime::backup`) writes a consistent online copy:

1. `DIR` must be new, with an existing parent, or an empty directory that you own with mode 700. A new directory
   is created with mode 700.
2. The SQLite online backup interface copies every page in one step under a single read transaction, so the copy
   is consistent even if another process writes. The copy, `native-state.sqlite3`, is a new mode 600 file
   switched from write-ahead log (WAL) mode to rollback-journal mode so that it can be opened read-only.
3. The copy is reopened read-only and checked (integrity, schema, audit chain) before `manifest.json` (mode
   600) is written. A failure removes both files.
4. The live state then receives a `state.backup` audit record, so the copy's audit head precedes it.

| Manifest field   | Meaning                                                          |
| ---------------- | ---------------------------------------------------------------- |
| `format`         | `switchboard-native-backup`                                      |
| `format_version` | 1                                                                |
| `engine`         | `cpp-native`                                                     |
| `engine_version` | Engine version that wrote the copy                               |
| `created_at`     | Runtime-clock time of the copy                                   |
| `database`       | Always `native-state.sqlite3`                                    |
| `bytes`          | Size of the copy                                                 |
| `sha256`         | SHA-256 digest of the copy                                       |
| `schema_version` | `user_version` of the copy                                       |
| `audit_head`     | Last audit hash in the copy, or empty if the copy has no records |
| `audit_records`  | Number of audit records in the copy                              |

`backup verify DIR` (`Runtime::verify_backup`) needs no configuration and constructs no runtime. It reads the
private manifest (exact fields, at most 64 KiB), compares the digest of the copy before SQLite opens it, requires
rollback-journal mode, opens the copy read-only, requires a recognized `user_version` (1 or 2) equal to the
manifest, runs SQLite's `integrity_check`, re-verifies the audit hash chain (up to 10,000,000 records), and
requires the chain's head and record count to match the manifest. The report has `verified` and, on failure,
a message.

Verification detects accidental damage and inconsistent edits. Someone who can rewrite the copy, its audit
chain and the manifest together can produce a backup that verifies, so keep the reported `audit_head` somewhere
independent. Verification never shows that providers' or devices' current state matches the backup.

There is no restore command. A manual restore (unexecuted instructions) is: verify the backup; stop every process
that uses the data directory; move `native-state.sqlite3` and any `-wal` and `-shm` files out of it; copy the
backup's `native-state.sqlite3` in with mode 600; start the engine. Restoring rewinds approvals, budgets and the
journal. Attempts that were in flight in the copy become uncertain at the first start once their deadlines have
passed; reconcile them against the providers' current state, not against the backup.

## Retention

`purge [--dry-run]` (`Runtime::purge`) applies retention to the core tables in one transaction. A dry run counts
the same rows without changing anything. An applied purge writes a `state.purge` audit record with its counts.

| Data                                                                                  | Removed when created or finished before                    | Setting (default)                       |
| ------------------------------------------------------------------------------------- | ---------------------------------------------------------- | --------------------------------------- |
| Call history used by the hourly budget (`native_calls`)                               | `now - tool_calls_days`                                    | `retention.tool_calls_days` (30)        |
| Approvals `executed`, `failed`, `denied`, `expired`, and `pending` past expiry        | `now - approvals_days` (creation time)                     | `retention.approvals_days` (90)         |
| Journal entries `succeeded`, `failed`, `reconciled_applied`, `reconciled_not_applied` | `now - actions_days` (resolution, else finish, else start) | `retention.actions_days` (180)          |
| Events without a deduplication key                                                    | `now - event_retention_days`                               | `switchboard.event_retention_days` (14) |
| Events with a deduplication key (payload replaced by `{}`, key kept)                  | `now - event_retention_days`                               | `switchboard.event_retention_days` (14) |

Never purged: `in_flight` and `uncertain` journal entries, `running` approvals (interrupted executions that need
manual inspection), unexpired `pending` approvals, and the audit chain. Native retention covers only these core
tables; connector data such as local tasks and monitoring snapshots is unaffected, and `retention.connectors` is
Python-only and refused. Runner ticks already apply the event rule on every tick.

## Kill switch

`kill-switch on|off|status` (`Runtime::kill_switch(true)`, `(false)` or `(std::nullopt)`) creates, removes or
reports an entry named `KILL_SWITCH` in the data directory. Any entry with that name, including a symbolic link,
engages the switch, and a state that cannot be read counts as engaged. Creating or removing the file by hand
still works.

While engaged, new non-read calls and approval executions are denied. Reads, polling and requests already in
progress continue; the switch does not cancel anything (use `Runtime::cancel` or stop the process). The file is
changed before the runtime lock is taken, so other processes that share the data directory see it on their next
call even while this runtime is busy. Each requested change writes a `kill_switch.on` or `kill_switch.off` audit
record, including whether anything changed; status reports write nothing.

## Commands

Commands that need a runtime take `--config CONFIG.json` first. The command line resolves relative `DIR` and
`SOCKET` arguments against the current directory.

| Command                                        | Purpose                                                                    |
| ---------------------------------------------- | -------------------------------------------------------------------------- |
| `approvals show ID`                            | One approval with its exact stored arguments; review it before `approve`   |
| `actions [--state STATE] [--limit N]`          | Recent journal entries, without arguments                                  |
| `actions show ID`                              | One journal entry with its stored arguments and owner                      |
| `reconcile ID applied\|not-applied NOTE`       | Record the operator's decision for an uncertain entry                      |
| `kill-switch on\|off\|status`                  | Engage, release or report the kill switch                                  |
| `backup create DIR`                            | Write a consistent copy and its manifest                                   |
| `backup verify DIR`                            | Verify a backup; needs no `--config` and constructs no runtime             |
| `purge [--dry-run]`                            | Apply or preview retention                                                 |
| `run [--once]`                                 | Runner ticks every 30 seconds, or one tick                                 |
| `broker`                                       | Serve the broker sockets from the `broker` section                         |
| `serve-mcp --broker SOCKET`                    | Forward protocol lines to a broker client socket; loads no configuration   |

A report whose `status` is `error`, `invalid` or `denied` exits with 1; a usage error exits with 2. `deny` is the
exception: its success report has status `denied`, so it exits with 0 when the approval was denied and with 1
otherwise. `approvals show` exits with 0 whenever the approval exists, whatever its state, and with 1 for a
malformed or unknown identifier. `backup verify` exits with 0 only when the backup verified. `run --once` exits
with 1 if a job failed or the tick was cancelled.

`run` installs handlers for the interrupt (`SIGINT`) and termination (`SIGTERM`) signals, unless a signal was
already ignored when the runner started, and restores the previous handling when it returns. The first signal sets
a stop flag and cancels the operation in progress: web transfers of the running job abort and the rest of that
tick is deferred. The flag is checked between ticks, and the 30-second pause is taken in one-second steps. The
handler then resets, so a second signal ends the process at once; an attempt interrupted that way stays in
flight until a sweep marks it uncertain.

The `broker` command requires all four `broker` settings: `client_socket` and `operator_socket` (nonempty
paths) and `client_uids` and `operator_uids` (1 to 64 numeric user identifiers from 0 to 4294967294). The
broker, its peer checks and the configuration-free Model Context Protocol (MCP) forwarding command are described
in the [isolation guide](./isolation.md).

## C++ embedding

The fourth `Runtime` constructor argument is an optional `Extension` that receives the runtime's `Context` and
`Registry` after built-in registration. Extension tools pass the same schema, exposure, policy, approval,
journal, kill-switch and audit path as built-in tools; a name that collides with an existing tool fails
construction. Jobs added through `Context::add_job` follow the runner rules above.

The operator methods `actions`, `reconcile`, `kill_switch`, `backup`, `purge` and the static `verify_backup`
return JSON reports with a `status`; invalid operator input returns an `invalid` report rather than raising.
`approval` instead returns the approval record itself, whose `status` is the approval state (`pending`,
`running`, `executed`, `failed`, `denied` or `expired`), with parsed `args` and `result` and rendered times; it
raises `Error` with code `invalid` for a malformed or unknown identifier. `cancel` returns nothing. Storage
failures raise `switchboard::Error`. The C application binary interface exposes the same operations; see
[C/Rust interface ownership and error semantics](./bindings.md). The header
[native.hpp](../include/switchboard/native.hpp) also provides `STATE_SCHEMA_VERSION`,
`DatabaseMode::read_only`, `Database::backup_to`, `sha256_file`, `write_private_file`, and the runner fields of
`Job`.

## Validation status

The cases are authored in [core.cpp](../tests/core.cpp) (settings, journal tool, bounded ticks, tick
cancellation), [recovery.cpp](../tests/recovery.cpp) (classification, duplicate guard, sweeps, late results,
approval review and linking, cancellation and deadline clamping, extension gate) and
[lifecycle.cpp](../tests/lifecycle.cpp) (migration, refusal of newer or incomplete state, backup and tampering,
retention, kill switch). They use synthetic extension tools and fake transports. One recovery case constructs
the real web transport only to check that it refuses to start once cancelled or past its deadline; it targets
the documentation-only address `192.0.2.1` and is designed to stop before any transfer.

None of these tests has been built or run. Compiler syntax checks do not show that the code links, that the
tests pass, that migrations work on real data, or that any provider or device behaves as described.

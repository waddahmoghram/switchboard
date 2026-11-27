# Backup and server-health monitoring

The `monitoring` connector reads explicitly configured endpoints, user-supplied completion reports, and local filesystem capacity.
No external monitoring agent or cloud subscription is required.
It does not implement backup software, restore data, start commands, connect to remote shells, discover hosts, or scan ports.

Hypertext Transfer Protocol (HTTP) responses can establish endpoint reachability.
Application readiness additionally requires an explicit JavaScript Object Notation (JSON) field/value criterion.
A successful response alone never certifies a backup or restore.
A fresh completion report is a producer's assertion, not independent evidence that its data can be restored.

**Validation status:** [Validation](../VALIDATION.md#current-status) records where these tests have run.
The own-machine procedures below have not been run.
No real endpoint, completion report, or disk has been checked with this connector.

## Configure named checks

Start with [the configuration example](../config/monitoring.toml).
Merge its tables into your private configuration, retain other enabled connectors, and add `"monitoring"` to `[switchboard].connectors`.
All addresses and paths in the example are synthetic placeholders.
Configuration never interpolates `~`, shell variables, or environment variables inside paths; supply real absolute local paths yourself.

`[connectors.monitoring]` accepts only:

| Key                            | Meaning and bounds                                        |
| ------------------------------ | --------------------------------------------------------- |
| `checks`                       | 1-32 named check tables                                   |
| `poll_interval_seconds`        | Integer, 30-86400; default 300                            |
| `snapshot_stale_after_seconds` | Integer, polling interval through 604800; default below   |

The default snapshot lifetime is the larger of 600 seconds and twice the polling interval.
Check names match `[a-z][a-z0-9_]{0,63}`.
Unknown fields, boolean numeric thresholds, non-finite numbers, and unsupported kinds are refused rather than ignored.
Each check has exactly one `kind`: `endpoint`, `backup`, or `disk`.

### Fixed endpoint checks

Each `endpoint` accepts `url`, `expected_status`, `readiness_field`, `readiness_equals`, `bearer_token`, `allow_http`,
and `timeout_seconds`, in addition to `kind`.

- `url` is required and fixed in configuration, at most 2048 characters.
  Use a certificate-checked `https://` address.
  Credentials, queries, fragments, percent escapes, backslashes, whitespace, invalid ports, and dot path segments are rejected.
  Paths use only letters, digits, `/`, `_`, `.`, `~`, and `-`.
- `expected_status` is one integer from 200 through 299, default 200.
  Other responses are `unhealthy` even though the endpoint was reached.
  Redirects are returned and refused, never followed.
- Supply both `readiness_field` and `readiness_equals`, or neither.
  The field is a dotted object-property path, at most eight identifier components of 32 characters each and 255 characters overall.
  Components use letters, digits, or underscores, starting with a letter or underscore.
  The expected value is a boolean,
  an integer from -1000000000 through 1000000000, or 1-128 printable basic-Latin characters.
  Boolean and numeric values remain distinct.
  Arrays and objects cannot be readiness criteria.
- With no readiness criterion, the expected response produces `status="reachable"` and `ready=null`, not `healthy`.
  With a criterion, the expected status and matching parsed field are both necessary for `healthy`.
  The response must be a JSON object; duplicate properties, malformed encoding, and non-finite numbers fail.
- `bearer_token`, if used, must be `env:NAME` or `file:/absolute/private/path`.
  It is resolved only when checking that endpoint, not at registration or in the monitoring doctor.
  Command-based references and literal tokens are forbidden.
  Resolved tokens must be 1-4096 visible basic-Latin characters without whitespace.
- Plain HTTP, including loopback, needs explicit `allow_http=true`.
  It is limited to exact `localhost`, numeric loopback, the private ranges `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, or `fc00::/7`.
  Public addresses, link-local metadata addresses, and arbitrary hostnames remain refused.
  A token sent over plain HTTP is exposed to observers on that network.
- `timeout_seconds` is an integer from 1 through 30, default 5.
  Response reads are capped at 65536 bytes.
  The shared transport's timeout applies per socket operation, not as a guaranteed whole-check deadline.

Every endpoint operation is a bodyless `GET` of the configured address.
Neither a tool caller nor a response can choose another address, redirect destination, command, or file.
Choose genuinely read-only endpoints on servers you control; a connector cannot prove that a badly designed server treats `GET` as read-only.
Responses may still create ordinary access logs on that server.

### Backup completion reports

A `backup` check requires `directory`, `filename`, and `max_age_seconds`.
The directory must be an explicit absolute local path.
The filename is one simple `.json` basename, at most 128 characters.
The freshness limit is an integer from 30 through 31536000 seconds and measures time **since completion**, never the file's modification time.

The completion report schema is intentionally small:

| Property         | Required | Contract                                                           |
| ---------------- | -------- | ------------------------------------------------------------------ |
| `schema_version` | Yes      | Integer exactly `1`; a boolean or `1.0` is not accepted            |
| `check`          | Yes      | Exact configured check name                                        |
| `status`         | Yes      | Exactly `"succeeded"` or `"failed"`                                |
| `started_at`     | Yes      | Timezone-aware start timestamp                                     |
| `finished_at`    | Yes      | Timezone-aware completion timestamp                                |
| `bytes_written`  | No       | Integer bytes, 0 through 9223372036854775807; never a boolean      |

No other properties are allowed.
The document must be a JSON object encoded in the [8-bit Unicode transformation format (UTF-8)](https://www.unicode.org/faq/utf_bom.html).
Duplicate properties and non-finite numeric values are rejected.
Both metadata size and actual reads are bounded to 16384 bytes.

Timestamps use `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` or a numeric `+HH:MM`/`-HH:MM` offset.
Fractions have one through six digits.
Date-only values, naive times, unknown `-00:00` offsets, invalid calendar/offset values, instants before 1970, and future instants are refused.
The ordering is `started_at <= finished_at <= checked_at`.
There is no future-clock grace period; synchronize producer and consumer clocks.
Valid instants are normalized to [Coordinated Universal Time](https://docs.python.org/3/library/datetime.html#aware-and-naive-objects), with explicit `+00:00` offsets and preserved microsecond precision.

This is **fake sample data**, evaluated only against a hypothetical clock at `2030-01-02T12:00:00+00:00` and a 3600-second freshness limit:

```json
{
  "schema_version": 1,
  "check": "backup",
  "status": "succeeded",
  "started_at": "2030-01-02T10:00:00+00:00",
  "finished_at": "2030-01-02T11:00:00+00:00",
  "bytes_written": 4096
}
```

At that hypothetical instant, age and duration are both 3600 seconds, and equality at the freshness threshold passes.
One microsecond later the unchanged report is stale.
In an actual earlier year this sample is invalid because it is future-dated; do not install it as evidence of a real backup.
The [healthy](../../tests/fixtures/monitoring/healthy.json) and [failed](../../tests/fixtures/monitoring/failed.json) fixtures are equally synthetic.

Fresh `"failed"` reports are `unhealthy` with `backup_failed`.
Stale reports are `stale` with `backup_report_stale`, even when they claim success.
A stale failed report preserves `report.status="failed"`.
Missing, unreadable, malformed, wrongly named, unsupported-schema, or invalid-time reports are `error`, never silently treated as success.

#### Producer responsibilities

The producer is **user supplied**, usually an integration with backup software you already operate.
Switchboard does not supply, invoke, schedule, or verify that software.

1. Capture the actual start and finish instants of one completed attempt.
1. Report the backup program's real completion outcome.
   A report reader cannot validate whether that program included all intended data or whether its success claim is truthful.
1. Publish one complete report in a directory owned by the same unprivileged user as Switchboard, with no group or other access.
   Mode `700` is recommended for the directory and `600` for the file.
1. Write a new private regular staging file in that same directory, close it, and atomically replace the final basename.
   Use the producer's atomic-file support or a same-filesystem rename.
   Do not truncate a report while the reader may be reading it.
1. Publish failed completion attempts too.
   Do not rewrite an old success with a new finish timestamp merely to keep monitoring green.
   In-progress attempts do not satisfy this completion-only schema.
1. Conduct and record separate restore drills appropriate to your data.
   A green report is not verified restore integrity, repository consistency, recovery-point coverage, or retention compliance.

The reader opens read-only descriptors, pins the directory, and refuses links at the directory's final component and report basename.
It rechecks descriptor ownership and permissions and rejects non-regular files, multiple hard links, shared permissions, and oversized reads.
Ancestor directories are trusted deployment configuration; this is not a general filesystem sandbox against an account that can replace those ancestors.
Platforms lacking the required no-follow and nonblocking open protections fail closed with `report_security_unavailable`.

### Local disk-capacity checks

A `disk` check requires an explicit absolute `path` and at least one of:

- `min_free_bytes`: integer bytes from 1 through 9223372036854775807.
- `min_free_percent`: finite number greater than 0 and at most 100.

The connector calls the standard library's `shutil.disk_usage` for that configured location on the machine and mount namespace where Switchboard runs.
It does not query another host or read raw devices.
Use a local filesystem location; checking a mount does not prove which physical storage backs it.
Containers see their own namespace, and a missing expected mount may expose the underlying filesystem instead.

Capacity below **either** configured minimum is `unhealthy` with `disk_below_threshold`; equality passes.
Measurements are `total_bytes`, `used_bytes`, `free_bytes`, and `free_percent`, plus the configured minima with the same unit suffixes.
Free capacity may exclude reserved space, so used plus available free bytes need not equal total bytes.
Missing, inaccessible, or unsupported capacity is `disk_unavailable`; invalid values are `disk_data_invalid`, never a made-up zero measurement.

## Tools, snapshots, and events

Both tools have tier `READ`.
Their only argument is an optional or required configured `name`; addresses, paths,
commands, discovery options, and report bodies are not accepted.

| Tool                | Arguments                | Result                                                        |
| ------------------- | ------------------------ | ------------------------------------------------------------- |
| `monitoring_status` | `{}` or `{"name":"..."}` | Cached `checks`, polling interval, and snapshot lifetime      |
| `monitoring_check`  | `{"name":"..."}`         | One `check`, boolean `changed`, integer `events_queued`       |

All tools still pass through the dispatcher and its policy.
A tool outcome of `"ok"` means the tool completed; inspect the inner check's `status` and `error` to judge the observation.
`READ` can cache snapshots and queue local events; it does not mean there are no local writes.
No monitoring tool sends a notification directly.

Each snapshot contains:

- `name`, `kind`, `status`, `error` (a fixed safe code or null), and timezone-aware `checked_at`.
- `reachable` and `ready`, which are nullable and meaningful for endpoints only.
  A 503 or refused redirect can have `reachable=true` while remaining unhealthy; network failures have `reachable=false`, `ready=null`.
- `measurements` with explicit byte, percent, or second suffixes.
  `http_status` is a dimensionless status code.
  `report` contains only normalized backup outcome/start/finish fields, never producer output.
- `changed_at` and `revision`, which advance only on a semantic transition or configuration change.

The status tool additionally returns `observed_status`, `observed_error`, `snapshot_stale`, and `report_stale`.
`unchecked` has null timestamps, revision zero, and `not_checked`; a changed configuration invalidates the old cache.
Cached reads recompute expiry and backup freshness without reading reports, taking measurements, writing snapshots, or emitting events.
If expired, displayed status is `stale` and error is `snapshot_stale`; prior observation status/error remain visible.
Otherwise stale backup evidence displays `backup_report_stale`.
Measurements remain those from `checked_at`, not an invented new measurement.
Backward clock movement also makes a cached snapshot stale.

The connector emits:

- `monitoring.changed` on the first observation and each semantic transition.
- `monitoring.alert` on those transitions whose new status is `unhealthy`, `stale`, or `error`.

Both event types have the same bounded payload: `name`, `kind`, `status`, `error`, `reachable`, `ready`, `measurements`, `report`, `checked_at`, `changed_at`, `revision`, and `previous_status`.
There are no raw response bodies, tokens, endpoint addresses, or filesystem paths.
Expected criterion values and secret references are not included either.
Repeated unchanged errors do not emit reminders, and changing byte counts or ages alone does not create event floods.
Backup outcome changes are semantic even if both reports remain stale.

Each event key includes the check name, monotonic transition revision, and event type.
A recovery and a later recurrence can therefore alert again.
Source snapshots and both events commit in **one database transaction**; an event-insertion failure rolls back the snapshot and all associated events.
An older observation cannot replace a newer observation of the same configured target.
This is local atomicity and deduplication, not exactly-once notification delivery.

## Architecture and offline registration

1. `NAME="monitoring"` identifies the package.
   `MIGRATIONS` exposes migration `monitoring:1`, creating `monitoring_snapshots` with a name primary key, configuration/state fingerprints, bounded snapshot content, timestamps, and a positive transition revision.
   Only the core applies migrations.
1. `register` validates configuration, registers the two tools, provides the `monitoring` service, and schedules `monitoring.poll`.
   It performs no endpoint requests, file inspection, secret resolution, or capacity checks.
   The monitoring doctor checks configuration only; it is not an operational health test.
1. The polling job runs only when the runner advances the scheduler.
   Its first run is due on the first tick;
   subsequent runs obey the configured interval, never less than 30 seconds.
   Checks run serially.
1. `MonitoringService` provides `status(args)`, `check(args)`, `poll()`, and `poll_job()`.
   `poll()` returns integer counts `checked`, `changed`, `events_queued`, and `record_errors`.
   The job takes no payload and returns nothing.
   Measurement failures become snapshots so other checks still run.
   Storage failures are counted independently;
   the job reports a safe failure after attempting other checks, leaving old snapshots to age.
1. Core event processing runs configured rules after subscribers.
   Monitoring imports no other connector and does not invoke notification services itself.

The dispatcher policy governs tool calls, not trusted internal service calls or the configured polling job.
For example, denying `monitoring_check` or setting its hourly cap does not stop polling.
The kill switch does not stop read-tier checks.
Remove a check or disable monitoring and restart the runner to stop its measurements.
Retired names are no longer exposed, but existing local rows and events are not automatically deleted.

## Optional notifications to yourself

Enable the [notify connector](notify.md), configure only your own channels, and then adapt the commented rule in the configuration example.
For a channel called `self`, a minimal rule is:

```toml
[[rules]]
name = "Backup needs attention"
on = "monitoring.alert"
[rules.match]
"payload.name" = "backup"
[[rules.do]]
tool = "notify_self"
args = { title = "Check {payload.name}", message = "{payload.status}: {payload.error}", channels = ["self"] }
```

`notify_self` is tier `DRAFT` and reaches only its configured self channels.
This rule is an explicit opt-in to a separate outbound connector, not a monitoring remote-write capability.
A `monitoring.changed` rule matching `payload.status="healthy"` can request recovery notifications if desired.
Set notification rate limits and inspect event failures.
External delivery can fail, be denied, or be duplicated around crashes; the event processor does not provide exactly-once delivery or automatic retry of failed rule effects.

A stopped runner cannot generate missing-poll alerts about itself.
Reading status can identify aged snapshots,
but no external observer is supplied to alert when the machine or runner is entirely down.

## Own-machine operating recipe

These commands are **UNRUN** and intended only for a machine you own after reviewing the configuration.
They assume Switchboard is already installed.
No project installation or service launch was performed here.

1. Create your dedicated private report folder, if you intend to use backup reports:

   ```bash
   install -d -m 700 "$HOME/.local/state/switchboard-backup-status"
   ```

1. Edit your private configuration manually using the example.
   Replace `OWNER`, choose only owned endpoints and local paths, and arrange the **user-supplied** producer contract above.
   Do not copy the synthetic 2030 report into a real status folder or configure an arbitrary system file as a report.
   Prefer a dedicated, least-privilege read-only endpoint credential.
1. Inspect offline configuration and cached status:

   ```bash
   switchboard --config "$HOME/.config/switchboard/config.toml" doctor
   switchboard --config "$HOME/.config/switchboard/config.toml" tools
   switchboard --config "$HOME/.config/switchboard/config.toml" call monitoring_status
   ```

   Monitoring's doctor is configuration-only.
   The application's doctor may inspect other enabled components.
   Initial cached checks should be `unchecked`; that is not healthy.
1. Explicitly measure one configured target at a time:

   ```bash
   switchboard --config "$HOME/.config/switchboard/config.toml" \
     call monitoring_check --args '{"name":"server"}'
   switchboard --config "$HOME/.config/switchboard/config.toml" \
     call monitoring_check --args '{"name":"backup"}'
   switchboard --config "$HOME/.config/switchboard/config.toml" \
     call monitoring_check --args '{"name":"disk"}'
   switchboard --config "$HOME/.config/switchboard/config.toml" events -n 20
   ```

   These calls make the configured requests/reads and write local state, but do not process queued notification rules.
   Inspect `checked_at`, `error`, nullable readiness, units, and producer finish time.
   A missing report should yield `report_missing`, not success.
1. Only after inspecting all enabled connectors and rules, run a single scheduler/event cycle or start polling:

   ```bash
   switchboard --config "$HOME/.config/switchboard/config.toml" run --once
   switchboard --config "$HOME/.config/switchboard/config.toml" events --failed
   ```

   `run --once` runs **all enabled jobs** and processes rules, which may send configured notifications or cause other enabled actions.
   For continuous operation, use `switchboard run` under your existing supervisor, with the reviewed configuration and secret environment.
   Run only one core event processor.

For fault validation on your own machine, use a separate named test check and a separate producer directory.
Publish a deliberate failed test completion, let its valid completion age past its limit, and then publish a fresh successful completion.
Verify changed/alert/recovery revisions and repeated-state suppression.
Never interrupt a real backup, fill a real disk, scan hosts, restore production data, or substitute fake success reports merely to test monitoring.

## Test coverage and limits

The [configuration tests](../../tests/unit/test_monitoring_settings.py), [measurement tests](../../tests/unit/test_monitoring_checks.py), [private-file tests](../../tests/unit/test_monitoring_files.py), and [application flows](../../tests/integration/test_monitoring_flow.py) use fake HTTP, synthetic report fixtures, substituted file descriptors and capacity calls, and an injected clock.
They cover healthy/failing/stale transitions, schema/time bounds, private read-only file policy, offline registration, policy-gated tools, atomic rollback, durable deduplication, and opt-in self-channel rules.
They do not certify a real host, network, backup product, mount identity, delivered notification, or restore.

The connector has no third-party runtime dependency.
It does not provide mount-identity validation, a whole-check deadline, producer authentication beyond local ownership/permissions, reminder escalation, backup execution, remote administration, or an external availability watchdog.

Protocol terms follow the defining [HTTP specification](https://www.rfc-editor.org/rfc/rfc9110.html#abstract) and [JSON specification](https://www.rfc-editor.org/rfc/rfc8259.html).

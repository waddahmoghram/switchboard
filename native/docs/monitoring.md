# Native monitoring: observations, not restore verification

The C++20 `monitoring` connector reads operator-selected endpoints, producer completion reports,
and capacity for configured local paths. A response establishes reachability, not application
readiness. A producer's successful completion report is **not independent restore verification**.
There is no backup execution, restore execution, host administration, remote shell, subprocess,
discovery, port scan, or arbitrary address supplied by a tool caller.

**Runtime validation: UNRUN.** The 44 named tests in [monitoring.cpp](../tests/monitoring.cpp) are
authored, not executed or collected. No project program, example, live endpoint, account, backup,
report fixture, or host/disk measurement was executed while authoring. Syntax-only compiler
validation does not establish operational correctness, successful linking, or test results.

**Static validation:** the permitted C++20 `-Wall -Wextra -Wpedantic -fsyntax-only` command over
the implementation and authored test source completed successfully without diagnostics.
The example JSON, synthetic report example, Markdown links/fences, and text whitespace were
checked as data only.

## Configuration and offline registration

Merge the [synthetic configuration example](../config/monitoring.example.json) into a private
native configuration. It is JavaScript Object Notation (JSON), not Python's configuration file.
Retain any other enabled connectors. All example addresses and paths are placeholders; the
example is not evidence that any service or backup exists.

Enable `"monitoring"` in `switchboard.connectors`. Settings live exclusively under
`connectors.monitoring`; disabled registration creates no monitoring table, tool, or job.
Registration validates all settings before creating its native table and registering its tools.
Registration and cached status do not resolve credentials, read reports, inspect target paths,
measure capacity, or make network requests. They may access the native database and the clock.

Only these top-level settings are accepted:

| Key                            | Bounds and default                                                                          |
| ------------------------------ | ------------------------------------------------------------------------------------------- |
| `checks`                       | Required object with 1-32 named check objects                                               |
| `poll_interval_seconds`        | Integer 30-86400; default 300                                                               |
| `snapshot_stale_after_seconds` | Integer from polling interval through 604800; default `max(600, 2 * poll_interval_seconds)` |

Names match `[a-z][a-z0-9_]{0,63}`. Every check requires `kind`: `endpoint`, `backup`, or `disk`.
Unknown fields, unsupported kinds, null required values, boolean numbers, and non-finite numeric
settings are errors, never silently ignored. Per-check enable flags are unsupported; omit unwanted
checks instead. Changes to configuration take effect when the native context is recreated.

### Fixed endpoint checks

| Key                | Contract                                                                                   |
| ------------------ | ------------------------------------------------------------------------------------------ |
| `url`              | Required fixed `https://` address, at most 2048 visible basic-Latin characters             |
| `expected_status`  | Integer 200-299; default 200                                                               |
| `readiness_field`  | Optional dotted property path, paired with `readiness_equals`                              |
| `readiness_equals` | Boolean, integer -1000000000 through 1000000000, or 1-128 printable basic-Latin characters |
| `bearer_token`     | Optional `env:NAME` or `file:/absolute/private/path` reference                             |
| `allow_http`       | Boolean; default false                                                                     |
| `timeout_seconds`  | Integer 1-30; default 5                                                                    |

Hypertext Transfer Protocol (HTTP) operations are bodyless `GET` requests to that configured
address. Certificate verification is enabled for encrypted transport. There are no redirects,
retries, authentication fallback, custom request headers, request bodies, alternate methods,
response-selected addresses, or model-selected addresses. Responses are capped at 65536 bytes.
The native transport uses a whole-request timeout as well as a connection timeout.

Addresses cannot contain credentials, query strings, fragments, percent escapes, backslashes,
whitespace, invalid/noncanonical ports, unspecified addresses, or multicast addresses. Hostnames,
numeric version-four addresses, and bracketed version-six addresses are supported. Paths contain
only letters, digits, `/`, `_`, `.`, `~`, and `-`; `.` and `..` path segments are refused.

Plain HTTP requires explicit `allow_http=true` and exact `localhost`, numeric loopback,
`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, or `fc00::/7`. Public literals, arbitrary
hostnames, and link-local metadata addresses are not accepted for plain transport. Sending a
bearer token over plain transport exposes it to network observers. Named hosts are resolved
only during an actual request; deployment configuration and its name-resolution environment are
trusted. This is not an address-discovery facility or a general network sandbox.

Readiness paths have at most eight identifier components, each at most 32 characters, and at most
255 characters overall. Components start with a letter or underscore and then use letters,
digits, or underscores. Both criterion fields must be supplied or both omitted. Equality is
typed: `true`, `1`, `1.0`, and `"true"` are different. Floating-point criteria, nulls, arrays,
and objects are unsupported. A readiness response must be an object; duplicate properties,
invalid encoding, non-finite numbers, and nesting beyond 32 parser levels fail closed.

An expected response without a criterion is `reachable`, with `ready=null`, never `healthy`.
With a criterion, the expected response status and exact field/value equality are both required
for `healthy`. A refused redirect or unexpected response is reachable but unhealthy.
No arbitrary response field, body, header, criterion value, address, or credential is persisted.
Only bounded numeric response measurements and the outcome leave measurement code.

Credential references are validated lexically at registration and resolved only for an explicit
check or scheduled poll. Command references, literal credentials, relative secret paths, and
other credential modes are refused. After the core secret reader's normalization, a token must
be 1-4096 visible basic-Latin characters without whitespace. File credentials need an owned
mode-700 parent and a private regular file. Missing or invalid credentials produce an error
before any request; the connector never falls back to anonymous access.

Choose genuinely read-only endpoints on servers you control. A `GET` can still create access
logs, and this connector cannot prove that a badly designed endpoint avoids server-side effects.

### Producer completion reports

A `backup` check accepts only `kind`, `directory`, `filename`, and `max_age_seconds`.
`directory` is an explicit absolute local path. `filename` is a simple `.json` basename of at
most 128 characters matching `[a-zA-Z0-9][a-zA-Z0-9_.-]{0,122}\.json`.
`max_age_seconds` is a required integer 30-31536000, measured since **completion**, not file
modification time. Paths are not expanded: no `~`, shell variables, environment interpolation,
backslashes, leading `//`, or `.`/`..` segments. Repeated/trailing separators normalize lexically.

The report is an object in the 8-bit Unicode transformation format (UTF-8), at most 16384 bytes
both in file metadata and in the actual bounded read. No additional properties are accepted:

| Field            | Contract                                                                |
| ---------------- | ----------------------------------------------------------------------- |
| `schema_version` | Required integer exactly 1; neither `true` nor `1.0`                    |
| `check`          | Required string exactly matching the configured check name              |
| `status`         | Required `"succeeded"` or `"failed"`; no in-progress states             |
| `started_at`     | Required timezone-aware start timestamp                                 |
| `finished_at`    | Required timezone-aware completion timestamp                            |
| `bytes_written`  | Optional integer 0-9007199254740991; no boolean or floating-point value |

Timestamps use `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` or a numeric `+HH:MM`/`-HH:MM` offset.
Fractions have one through six digits. Calendar dates and local years 1970-9999 must be valid,
normalized instants must be in the supported epoch range, and unknown `-00:00` offsets are refused.
Naive times, leap seconds, invalid offsets, duplicate properties, and non-finite numbers fail.
The required ordering is `started_at <= finished_at <= observed time`; **future completion is
rejected before freshness is tested**, without a clock-skew grace period. Observations use the
core's whole-second epoch clock; report arithmetic retains microseconds. A fractional completion
later than that clock second remains future until the clock advances.

Valid producer times normalize to universal time with six fractional digits and a `Z` suffix.
Age equal to `max_age_seconds` passes; one microsecond beyond it is stale. Fresh failed reports
are `unhealthy` / `backup_failed`. Stale reports are `stale` / `backup_report_stale`, even when
they claim success; a failed producer outcome remains visible in `report.status`.

This is **synthetic data only**, assessed against a hypothetical observation clock of
`2030-01-02T12:00:00Z` and a 3600-second maximum age:

```json
{
  "schema_version": 1,
  "check": "nightly_backup",
  "status": "succeeded",
  "started_at": "2030-01-02T10:00:00Z",
  "finished_at": "2030-01-02T11:00:00Z",
  "bytes_written": 4096
}
```

Its completion age is exactly 3600 seconds at that hypothetical instant. An earlier real clock
must reject it as future-dated. Never install sample data as evidence of a real backup.

#### Producer setup and trust boundary

1. Supply your own producer integration with backup software you already operate. Switchboard
   neither supplies nor launches that producer.
2. Before monitoring, create a dedicated absolute report directory owned by the same unprivileged
   account as Switchboard, with mode `700`. Publish an owned regular report with mode `600`.
3. Capture the real start, completion time, exact configured check identity, and actual outcome of
   one completed attempt. Publish failed attempts too. In-progress attempts do not satisfy this schema.
4. Write a new complete private staging file in the same directory, close it, and atomically rename
   it over the configured basename. Do not truncate a report while readers may be reading it.
5. Synchronize clocks. Do not update an old success timestamp merely to keep monitoring green.
6. Conduct and record separate restore drills. Producer assertions cannot establish completeness,
   repository integrity, retention compliance, or that data can actually be restored.

The shared native `read_private_file` helper opens read-only descriptors, pins the directory and
report, rejects symbolic links at their final components and nonregular inputs, and checks
ownership, directory mode, file privacy, and byte bounds. Monitoring requests its strict private
file mode and never creates, repairs, or changes report directories or files.
Ancestor directories are trusted deployment configuration, not a filesystem sandbox. Unlike the
Python report reader, the shared native helper does not require a single hard link. The producer
and account remain trusted; descriptor checks do not authenticate the producer's claims.
Missing, unsafe, unreadable, or oversized files map to `report_unavailable` rather than exposing
paths or operating-system exception text. Malformed contents map to `report_malformed`;
identity/schema/time failures use `report_check_mismatch`, `report_schema_invalid`, or
`report_time_invalid`. None is silently interpreted as success.

### Configured local capacity

A `disk` check accepts only `kind`, `path`, `min_free_bytes`, and `min_free_percent`.
`path` uses the absolute-path rules above. At least one threshold is required:

- `min_free_bytes`: integer 1-9007199254740991.
- `min_free_percent`: finite number greater than zero and at most 100.

Only an explicit check or scheduled poll calls `std::filesystem::space` for that path.
`total_bytes` is capacity; `used_bytes` is capacity minus free blocks; `free_bytes` is space
available to the caller, which can exclude reserved blocks. `free_percent` is available space
divided by capacity, multiplied by 100. Used plus available free need not equal total.
Both configured minima are included in measurements. Below **either** minimum is
`unhealthy` / `disk_below_threshold`; equality passes.

Missing/inaccessible paths are `disk_unavailable`; zero/invalid capacity, inconsistent available
space, or values beyond the native exact-integer range are `disk_data_invalid`. Failure produces
no made-up capacity measurement. The pure `monitoring_detail::disk_capacity` classifier allows
deterministic authored threshold tests without measuring a filesystem.

Paths are interpreted in the native process's machine and mount namespace. Containers may see
different capacity. A disappeared intended mount can expose its underlying filesystem; this
check does not verify mount identity, physical storage, remote host state, or device health.
Use local filesystems. Filesystem metadata operations, including report reads, can block on
misconfigured/network filesystems; there is no portable whole-check deadline for local file operations.

## Tools, cache, job, and events

Both tools have tier `read`, object argument schemas with `additionalProperties=false`, and a
`name` string enumeration containing only currently configured checks, length 1-64.

| Tool                | Arguments                             | Result                                                                   |
| ------------------- | ------------------------------------- | ------------------------------------------------------------------------ |
| `monitoring_status` | `{}` or `{"name":"configured_name"}`  | Cached `checks`, `poll_interval_seconds`, `snapshot_stale_after_seconds` |
| `monitoring_check`  | Required `{"name":"configured_name"}` | One `check`, boolean `changed`, integer `events_queued`                  |

There is no poll tool accepting external operations. The single job `monitoring.poll` serially
checks all operator-configured targets. Its first run is deferred by at least the configured
interval, never less than 30 seconds; later execution requires the native scheduler to tick.
There is no background thread created here. Measurement failures become error observations so
other checks continue. Storage/event failures also allow later checks to run, then the job
throws safe `monitoring_poll` to signal incomplete recording. There is no automatic immediate retry.
Up to 32 sequential endpoint timeouts plus filesystem operations can make a poll slow.

Snapshots contain `name`, `kind`, `status`, nullable safe `error`, nullable `reachable`/`ready`,
numeric `measurements`, bounded `report`, `checked_at`, `changed_at`, and `revision`.
`checked_at` is the observation timestamp, not a fresh cached-read timestamp. `unchecked` means
no observation for the **current configuration**: null timestamps, revision zero, and `not_checked`.

The native-only table `native_monitoring_snapshots` has columns `name` (primary key), `config_key`,
`state_key`, `snapshot_json`, integer `checked_at`, integer `changed_at`, and positive integer
`revision`. It is independent of Python's `monitoring_snapshots` and migrations. Native events
use the core's `native_events`; no Python database is opened or modified.

The configuration fingerprint covers native monitoring schema version 1, check name, kind, and
normalized settings: target, criteria, expected status, timeout, transport consent, secret
reference, report identity/path/freshness limit, or disk path/thresholds. Defaults normalize
consistently. Resolved secret values are never hashed or stored. Changing a secret value alone
does not invalidate a previously observed result; checking again is required.
Changed or removed configuration cannot reuse old healthy data. Removed-name rows may remain
stored but are not returned; there is no automatic removed-name cleanup.

Cached rendering adds `observed_status`, `observed_error`, `snapshot_stale`, and `report_stale`.
Age strictly greater than the current configured snapshot lifetime, or a backward clock, renders
`stale` / `snapshot_stale`. Backup completion freshness is recomputed from cached producer times
even before snapshot expiry. Measurements retain their original values, and observed outcomes
remain visible. Rendering never writes, emits, resolves secrets, reopens reports, or measures.
Corrupt cache is an explicit `monitoring_cache` failure, not a hidden refresh or healthy fallback.

Each semantic change emits `monitoring.changed`, source `"monitoring"`. A changed observation
whose new status is `unhealthy`, `stale`, or `error` also emits `monitoring.alert`.
Initial observations count as changes. Recovery emits a change without an alert; recurrence
can alert again. Unchanged errors and changing ages/byte counts alone do not emit reminders.
Producer outcome changes are semantic even if both observations remain stale.

Each payload is at most 16384 bytes and contains the snapshot plus nullable `previous_status`.
There are no raw bodies, exception messages, tokens, criteria, secret references, addresses,
or configured paths. Deduplication keys are
`native:monitoring:<name>:<revision>:<event_type>`. Revisions advance only on a semantic or
configuration transition; previous status is null after a configuration change.
Each snapshot and its zero, one, or two events commit in **one explicit transaction on the same
native database connection**. An event failure rolls back the snapshot and every associated
event. Older observations cannot overwrite newer observations of the same configuration.
This is local atomic recording, not exactly-once external notification delivery.

## Native/Python limits and authored evidence

The Python implementation and its documentation remain unchanged. Supported named check
settings intentionally resemble Python tables, but the native implementation is not drop-in
state or operational parity:

- Native configuration is JSON, state and deduplication are native-only, and removed rows are not
  migrated or garbage-collected here.
- Native byte counts stop at 9007199254740991 for exact JSON integers, rather than Python's
  signed 64-bit ceiling. Boolean and floating-point byte counts are refused.
- The observation clock is whole seconds; report arithmetic preserves microseconds. Normalized
  report timestamps use a fractional `Z` representation rather than Python's explicit zero offset.
- Initial native polling is deferred, unlike Python's first-tick poll. Native polling has no
  exposed service/count result or monitoring doctor; registration is only lexical configuration
  validation. It is not a credential, report, mount, readiness, or restore check.
- Native transport uses a whole-request timeout; Python documents per-operation socket timeouts.
  The native helper does not enforce Python's single-hard-link report condition.
- Unsupported settings and behavior are rejected, not emulated: discovery, custom scripts,
  arbitrary headers/methods, other authentication, command secrets, remote capacity, backup/
  restore execution, restore claims, automatic notifications, and administration are outside scope.

The 44 UNRUN test functions author coverage for fake transport behavior, lazy synthetic secrets,
private synthetic reports, exact configuration fingerprints, invalidated cache, typed readiness,
timestamp offsets/fractions/future/chronology, stale boundaries, failed/recovery/unchanged
transitions, deterministic capacity thresholds, unsafe/missing paths, and transaction rollback.
Capacity classification tests inject numbers; the missing-path integration test uses a future
private fixture directory under the executing project, not a real configured disk. Tests create
their fixtures only if explicitly run in the future and clean up their own directories.
No runtime, test collection, live/network, or host-capacity validation is claimed.

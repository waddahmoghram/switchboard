# Native Home Assistant adapter

This C++20 adapter uses Home Assistant's Representational State Transfer (REST) application programming
interface (API), with JavaScript Object Notation (JSON) requests. It has no Python subprocess,
interpreter embedding, fallback, or dependency on Python home state. The Python baseline `6989a4b`
is a read-only behavior reference, not evidence that native behavior has run.

**Validation boundary:** source and authored tests have received syntax/type and formatting checks only.
Every test below is **authored but UNRUN**. No project program, test collection, test, example, build,
Home Assistant server, account, or device was executed or contacted. Public provider and SQLite
documentation was read on 2026-11-11.

**Not a safety controller.** Review the actual physical effect of every concrete entity. Locks,
alarm panels, covers including garage doors, cameras, switches, scripts, scenes, automations,
appliance relays and other domains outside the six listed below cannot be configured. A misleading
`light`, `fan`, or `climate` entity can still conceal unsafe integration behavior or high-power
equipment; names and reported membership cannot prove otherwise. Do not review such entities for
use here. Keep physical interlocks and manual control independent.

## Tools and tiers

Entry point: `switchboard::register_home(Context&, Registry&)` in
[`home.cpp`](../src/home.cpp). It returns immediately unless `Context.enabled("home")`.
Enabled registration validates settings, creates the two home tables when they are missing
(`CREATE TABLE IF NOT EXISTS`, in one transaction, even when polling is disabled), and registers
exactly eleven tools. It never resolves a secret, makes a provider request, records a snapshot or
emits an event. It schedules the `home.poll` job only when `poll.enabled` is `true`. Empty profiles
and preset tables are allowed, but their action schemas admit no targets.

The literal tiers `read` and `act` mean read-only and externally acting tools. `home_poll` is a
`read` tool: it writes only local snapshots, health and events, and never sends a device request.

| Tool                   | Tier   | Inputs and behavior                                                                                                                                                                   |
| ---------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `home_targets`         | `read` | Empty object; offline exact exposures, rooms, profiles, lighting presets with fingerprints, and current `binding`. No token/reference, server address or polling setting is returned. |
| `home_states`          | `read` | Optional `entity_id`, `domain`, `room`, `limit` (integer 1-200, default 100). One exact state or filtered, sorted states.                                                             |
| `home_capabilities`    | `read` | Required exact exposed `entity_id`; optional `room`. Live reported attributes, feature decoding, availability, leaf indication, reviewed profile and binding.                         |
| `home_sensor_summary`  | `read` | Optional exposed sensor `entity_id`, `room`, `kind`, `source` (`live` or `cached`), `limit` (1-200, default 100). See [Sensor summary](#sensor-summary).                              |
| `home_poll_status`     | `read` | Empty object; offline polling health, cached minimized snapshots, revisions and staleness.                                                                                            |
| `home_poll`            | `read` | Empty object; one sample of the configured entities, refused while polling is disabled. See [Polling, events and storage](#polling-events-and-storage).                               |
| `home_lights`          | `act`  | Required `binding`, `entity_ids` (1-16 distinct reviewed lights), `state` (`on`/`off`); optional `room`, `brightness_pct`, `rgb_color`, `color_temp_kelvin`.                          |
| `home_climate`         | `act`  | Required `binding`, one profiled `entity_id`, `unit`; optional `room`, `temperature`, complete `target_temp_low`/`target_temp_high`, `hvac_mode`.                                     |
| `home_fan`             | `act`  | Required `binding`, one profiled `entity_id`, integer `percentage`; optional `room`.                                                                                                  |
| `home_media`           | `act`  | Required `binding`, one profiled `entity_id`, `action` (`play`, `pause`, `stop`, `volume`, `mute`); optional `room`; only the selected action's value is accepted.                    |
| `home_lighting_preset` | `act`  | Required `binding`, preset `name` and that preset's 64-character `fingerprint`; nothing else. See [Lighting presets](#lighting-presets).                                              |

`home_capabilities` uses `GET /api/states/<entity_id>`, not a fabricated service catalog or a
device probe. Reported support is advisory even when unavailable or unprofiled; it never
authorizes an action. Sensors return no inferred writable capabilities.

Assistant and rule `act` calls require approval under the runtime's default policy. Explicit
configuration can change that policy; the example pins each action to `ask`. A trusted human-origin
call counts as approval, but cannot bypass denial or the core kill switch. Approval, expiry,
rate limits, single-use claims and current-policy rechecking belong to the shared runtime,
not a second adapter-specific authorization path. Direct handlers are for trusted internal use.
Do not approve from a shortened summary; review the complete stored arguments. A preset request
stores only its name and fingerprint: its approval summary appends the reviewed target content, which
the runtime may shorten, so compare the fingerprint with `home_targets` before approving.

## Configuration

Merge [`home.example.json`](../config/home.example.json) into the native configuration, preserving
other enabled connectors and policy. All addresses and entity names are fictional. Deliberately
append `home` to the connector list. The example uses separate native storage and keeps polling disabled.

Use a dedicated non-administrator Home Assistant user and a long-lived access token. That token
is **not entity-scoped**; the adapter's local allowlists are an additional software boundary.
Use `env:NAME` or an owner-private `file:PATH` secret reference handled by `Context.secret`.
Command references and literal credentials are rejected. Registration does not read the file or
environment. Credentials are resolved for each actual request and never returned or hashed.

Unknown settings are rejected, including unsupported `speech`, `quiet_hours` or `timezone` settings
set to `false` or empty objects. The same applies to extra keys in device profiles, presets, polling
and action inputs. JSON booleans are not numbers; numeric settings must be finite.

| Setting            | Contract                                                                                                                                                                                         |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `base_url`         | Required, at most 2,048 characters; American Standard Code for Information Interchange (ASCII) `https://host[:port]` origin, optional final slash. No credentials, path prefix, query, fragment. |
| `token`            | Required `env:` or `file:` reference, at most 2,048 characters. Environment names match `[A-Za-z_][A-Za-z0-9_]{0,127}`. Secret-file access is enforced by the core.                              |
| `allow_http`       | Boolean, default `false`. Hypertext Transfer Protocol (HTTP) without encryption requires explicit `true` and exposes the token on the network.                                                   |
| `expose`           | Default `[]`; at most 200 distinct exact lowercase entity identifiers. No globs, devices, areas, floors or labels.                                                                               |
| `deny_domains`     | Default `[]`; at most 100 distinct lowercase domain names; **additional denials only**, never an override of the hard domain restriction.                                                        |
| `lights`           | Default `[]`; at most 200 distinct exposed, non-denied `light` entities, explicitly reviewed as physical leaf devices.                                                                           |
| `rooms`            | Default `{}`; at most 100 lowercase aliases of 1-64 characters, each containing 1-200 distinct exposed, non-denied entities.                                                                     |
| `climate`          | Default `{}`; at most 100 exact exposed `climate` profiles. Required `unit`, `minimum`, `maximum`, `modes`; optional `max_change`.                                                               |
| `fans`             | Default `{}`; at most 100 exact exposed `fan` profiles. Optional integer `minimum` (default 0), `maximum` (100), `max_change` (100).                                                             |
| `media_players`    | Default `{}`; at most 100 exact exposed `media_player` profiles. Required numeric `maximum`; optional `minimum` (default 0).                                                                     |
| `lighting_presets` | Default `{}`; at most 100 lowercase names of 1-64 characters, each `{"targets": {...}}` mapping 1-16 reviewed `lights` to a light state. See [Lighting presets](#lighting-presets).              |
| `poll`             | Default disabled. Object with `enabled`, `entities`, `interval_seconds` and `stale_after_seconds`. See [Polling settings](#polling-settings).                                                    |

The base address also rejects percent escapes. Only `sensor`, `binary_sensor`, `light`, `climate`,
`fan`, and `media_player` are permitted in exposure, rooms and polling. Each identifier matches
`[a-z0-9_]{1,64}\.[a-z0-9_]{1,190}`. Every action target also needs its typed profile; exposure or
room membership alone does not grant control. `room` only restricts explicitly supplied targets and
**never expands** them.

Climate `unit` is `C` (Celsius) or `F` (Fahrenheit), matching the provider's **reported**
`temperature_unit` (`"\u00b0C"` or `"\u00b0F"`). Configuration bounds are -50 to 100 Celsius or
-58 to 212 Fahrenheit, with `maximum >= minimum`. Configure a much narrower suitable range.
`max_change` defaults to 2 in that unit, bounded to 0.1-20. `modes` explicitly lists 1-5 distinct
members of `off`, `heat`, `cool`, `heat_cool`, `fan_only`. No automatic unit conversion occurs.

Fan bounds are integer percentages 0-100 with `minimum <= maximum`; `max_change` is 1-100.
Media bounds are fractions 0-1 with `minimum <= maximum`, not measured loudness or hearing-safety
guarantees. Change caps can also block large reductions; manual controls remain independent.

### Polling settings

| Key                   | Contract                                                                                                                                              |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`             | Boolean, default `false`. Only `true` schedules `home.poll` and permits `home_poll` or cached summaries.                                              |
| `entities`            | At most 200 distinct exact exposed, non-denied identifiers in the six permitted domains; 1-200 are required when enabled. Checked even when disabled. |
| `interval_seconds`    | Integer 30-86,400, default 60; the job interval.                                                                                                      |
| `stale_after_seconds` | Integer from `interval_seconds` to 259,200; default is the larger of 300 and three intervals.                                                         |

Polling settings are read-only configuration. They are excluded from the action binding, so changing
them never invalidates a queued approval.

The `mcp.expose` setting selects tools exposed through the Model Context Protocol (MCP).
It is not the home entity allowlist. Core policy settings and the operator's secret-file rules are
independent of this adapter's configuration.

## Binding and reloads

Read `home_targets` before each new action. Copy its full 64-character `binding` into the action
you review. Every action schema pins that value and enumerates its concrete reviewed target set;
the preset schema also enumerates the current preset names and fingerprints. The handler checks again
before network access. Duplicate light targets fail before any preflight.

Binding version `native_binding_version = 2` hashes the component `home`, normalized server origin,
`allow_http`, exact exposures, additional denials, all lights/rooms, climate modes/units/limits,
fan/media limits, and every lighting preset's normalized targets and fingerprint. Polling settings are
excluded. Bindings from builds that used version 1 no longer match; read `home_targets` again and
request fresh approval. Entity sets, room members and mode sets are sorted; profile numbers and
defaults are normalized. Origin normalization lowercases it, removes a final slash and host dot, and
removes default ports. Alternate Internet Protocol version 6 (IPv6) spellings can conservatively yield
different bindings. No secret value, secret reference, timestamp, pointer, unordered iteration, or
process identity enters the digest. Rotating a credential reference alone preserves the binding.

A preset fingerprint is a digest of its component name, `native_preset_version = 1` and its normalized
targets. It does not include the preset name or the key order of the configuration file. Changing a
preset's content changes its fingerprint and the binding; other presets keep their fingerprints.

On runtime reconstruction, stored approval arguments are revalidated against the new schema.
Changing an endpoint, target, room, denial, limit or preset invalidates the old binding even if the old
requested value would fit the new limits. Request fresh approval; never substitute a new binding into
an old approval. Current policy and live capabilities are rechecked at execution. The digest cannot
detect provider-side entity remapping or hidden side effects. Long-lived instances use their
captured configuration until deliberately reconstructed.

## Control coverage

Each action validates scope and combinations before effects and reads every intended target during
preflight. `unknown`/`unavailable` states fail closed. Reported `entity_id` or `group_members`
membership must be absent, null, empty, or a singleton containing only the target itself.
Other members, malformed membership, and even groups containing other explicitly requested lights
are refused. Absence of group metadata is not proof of a physical leaf; human review is still required.

| Control | Supported checks and exact service                                                                                                                                                                                                                                                                      |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Lights  | Known nonempty `supported_color_modes`; on/off; integer brightness 1-100 percent; three integer red/green/blue channels 0-255; integer kelvin 1,000-20,000 intersected with reported limits. Off cannot include brightness/color. Two color forms cannot be mixed. `light.turn_on` or `light.turn_off`. |
| Climate | Single setpoint for reviewed/reported `heat` or `cool`; ordered complete range for `heat_cool`; mode-only calls for all reviewed modes. Reported feature bits, unit, ordered bounds, profile intersection and previous-setpoint change cap. `climate.set_temperature` or `climate.set_hvac_mode`.       |
| Fan     | Reported speed-control bit and finite prior percentage 0-100; configured bounds and absolute change cap; zero requests off. Only `fan.set_percentage`; no implicit speed mapping.                                                                                                                       |
| Media   | Per-action reported feature bits; exact action/value pairing. Play/unmute require observed in-profile volume; explicit volume values must fit the profile. Pause, stop and mute do not require a known volume. `media_player.media_play`, `media_pause`, `media_stop`, `volume_set`, `volume_mute`.     |
| Presets | The Lights checks for every target, then one `light.turn_on` or `light.turn_off` per target. Never `scene` or `script` services. See [Lighting presets](#lighting-presets).                                                                                                                             |

Climate mode-only entry into heating/cooling requires existing setpoints/range to be known, ordered
and within both profile and device bounds; it cannot energize an already out-of-profile setpoint.
Mode-only `off`/`fan_only` does not need temperature attributes, but still needs a reported/reviewed
mode. Supplying temperatures with an incompatible mode is rejected. Readback also checks the
effective mode for a temperature-only call. This is observation of requested setpoints/mode, not comfort.

There are no implicit volume changes, power-on, unmute, source selection, content addresses, playlists,
or grouping requests. Media play does not guarantee audible playback, including when a player stays
muted. There is no quiet-hours implementation; any `quiet_hours` configuration is rejected.
Reported temperature/speed step attributes are displayed, not used for automatic rounding or conversion;
device quantization may leave a request unconfirmed.

## Action and readback contract

Reads use `GET /api/states` or `GET /api/states/<entity_id>`. The list endpoint fetches everything the
token can read, then filters locally; `limit` is an output cap, not an upstream scope or pagination.
Malformed or duplicate states fail the entire read. Output retains only bounded observation/capability
attributes; artwork, access-token attributes, provider context, content addresses and arbitrary attributes
are omitted. Names, retained values and timestamps remain untrusted data, never instructions.
Provider timestamps are bounded strings, not validated evidence of device freshness.

Each typed action uses exactly one `POST /api/services/<domain>/<service>`, containing one concrete
`entity_id` or, for multi-light calls, the exact explicit entity list and only typed values. Every target
is preflighted before that submission. Multi-light effects can still be partial and are not atomic.
The provider's returned changed-state list is ignored; only independent per-target reads are evaluated.
A successful response with an unreadable body can therefore still be accepted, but that body never
proves anything.

Every request has a 15-second timeout and `Authorization: Bearer ...` plus `Accept: application/json`.
Posts additionally use `Content-Type: application/json`. State responses are capped at 1,048,576 bytes;
action responses at 65,536 bytes. The real shared transport verifies certificates and does not redirect
or retry; it also applies the runtime's per-call deadline and cancellation. The adapter does not retry,
sleep, schedule readback, compensate, or resend.

| Result                                  | Meaning                                                                                                                                                    |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `request_status = accepted`             | A bounded successful 2xx response, not physical completion.                                                                                                |
| `request_status = rejected`             | A bounded 3xx/4xx response except 408; no redirect, no readback, and therefore no `confirmed` field.                                                       |
| `request_status = unknown`              | Transport exception, response-cap failure, 408, 5xx or other ambiguous status; one readback is attempted, never another effect; `"effect_unknown": true`.  |
| `status = observed`, `confirmed = true` | Accepted request and one independent matching, available, leaf-reported, within-profile readback for every explicit target.                                |
| `status = requested_unconfirmed`        | Mismatch, ambiguity, lost capability, unavailable/grouped/out-of-profile result or failed readback. Read errors are reported in `readback_errors`.         |

Rejections have `status = rejected`. An unknown submission never becomes confirmed merely because
its readback matches. Numeric comparison tolerances are one brightness step, one per color channel,
0.01 volume and 0.05 for other numeric values. Brightness percent is compared with its nearest
0-255 step. Profile/device bounds are checked separately without tolerance. Matching just beyond a
volume/temperature bound does not confirm the action. Readback rechecks applicable capabilities,
units and memberships as well as requested values.

### Standardized confirmation fields

Every `act` report follows the shared [cross-engine contract](../../contracts/README.md):

- `confirmed` is present exactly when a readback ran, that is, for accepted and unknown submissions.
  It is `true` only when the submission was accepted and every target's readback matched within its
  profile. A rejected submission has no readback and no `confirmed` field.
- `"effect_unknown": true` appears at the top level exactly when a submission outcome is unknown and is
  absent otherwise. The shared runtime then journals the call as `uncertain`, and its duplicate guard
  refuses an identical request until the operator reconciles it. Nothing here resends anything.
- `status` keeps its values `rejected`, `observed` and `requested_unconfirmed`.

Through the C interface, an accepted report is therefore `SB_EFFECT_CONFIRMED` or
`SB_EFFECT_UNCONFIRMED`, a rejected report is `SB_EFFECT_RETURNED`, and an unknown report is
`SB_EFFECT_UNKNOWN`. Earlier native receipts set `confirmed = false` on rejections and had no
`effect_unknown`.

Every receipt has `physical_delivery_verified = false`, `retry_safe = false`, observations and
safe error metadata. A runtime outer `status = ok` means the handler returned a receipt; inspect
the inner `request_status`, `status`, `confirmed`, `effect_unknown` and `readback_errors`.
**Readback is software evidence only, not verified physical delivery or causal proof.**
Another automation or a manual change can race all reads.

## Lighting presets

A preset is reviewed lighting-only configuration, applied as individual light requests:

```json
"lighting_presets": {
  "evening": {
    "targets": {
      "light.living_lamp": {"state": "on", "brightness_pct": 40, "color_temp_kelvin": 2700},
      "light.reading_lamp": {"state": "off"}
    }
  }
}
```

Each preset contains only `targets`, mapping 1-16 identifiers that are already in `lights` (and are
therefore exposed, non-denied `light` entities) to a state. A state requires `state` (`on`/`off`) and
accepts the `home_lights` bounds: integer `brightness_pct` 1-100, `rgb_color` as three integers 0-255, or
integer `color_temp_kelvin` 1,000-20,000. `rgb_color` and `color_temp_kelvin` cannot be combined, and
`off` cannot carry brightness or color. Transitions, effects, services, scenes, scripts and arbitrary
action data are rejected.

`home_lighting_preset` takes only `binding`, `name` and `fingerprint`, all copied from `home_targets`.
A changed binding fails with code `home_binding`; a missing, renamed or changed preset fails with code
`home_preset`. Both refusals happen before the credential is resolved or Home Assistant is contacted.

1. Every target is preflighted exactly like a one-light `home_lights` call: scope, reviewed light,
   availability, leaf membership, known color modes, and live brightness/color/kelvin capability. Each
   submission request, including its credential, is prepared before the first effect. Any failure
   sends nothing.
2. Targets are then submitted in entity-identifier order. Each is its own `light.turn_on` or
   `light.turn_off` request with that target's values, followed by one independent readback.
3. A rejected or unknown submission stops the remaining targets. Before each later submission, the
   adapter also stops when the call's deadline has passed or cancellation was requested.
4. Nothing is retried, resent, rolled back or compensated. The effect is sequential, not atomic.

| Field                                                | Meaning                                                                                                                                       |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `results`                                            | One standard light receipt per submitted target, in order, each with its own `request_status`, `status`, `confirmed` and `effect_unknown`.    |
| `not_attempted`                                      | Targets that were never submitted.                                                                                                            |
| `stop_reason`                                        | `null` when every target was submitted; otherwise `rejected`, `unknown`, `deadline` or `cancelled`.                                           |
| `partial`                                            | `true` when some, but not all, targets were accepted.                                                                                         |
| `status`                                             | `rejected` when the first submission was refused; `observed` when every target was accepted and confirmed; otherwise `requested_unconfirmed`. |
| `confirmed`                                          | Present when any readback ran; `true` only when every target was submitted, accepted and confirmed.                                           |
| `effect_unknown`                                     | Top-level `true` when any submission outcome is unknown; absent otherwise.                                                                    |
| `atomic`, `retry_safe`, `physical_delivery_verified` | Always `false`.                                                                                                                               |

Reports also repeat `name`, `fingerprint`, the preset's `entity_ids` and a `confirmation_scope` note.

## Sensor summary

`home_sensor_summary` classifies only exposed `sensor` and `binary_sensor` entities by their reported
`device_class`. It never infers a class from names, and it never converts units.

| Domain and `device_class`     | `kind`        | Value                                    | Recognized units, returned unchanged                                                                 |
| ----------------------------- | ------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `sensor` / `temperature`      | `temperature` | Finite number                            | `"\u00b0C"`, `"\u00b0F"`, `K`                                                                        |
| `sensor` / `humidity`         | `humidity`    | Finite number; usable only from 0 to 100 | `%`                                                                                                  |
| `sensor` / `battery`          | `battery`     | Finite number; usable only from 0 to 100 | `%`                                                                                                  |
| `sensor` / `power`            | `power`       | Finite number                            | `mW`, `W`, `kW`, `MW`, `GW`, `TW`                                                                    |
| `sensor` / `energy`           | `energy`      | Finite number and reported `state_class` | `J`, `kJ`, `MJ`, `GJ`, `mWh`, `Wh`, `kWh`, `MWh`, `GWh`, `TWh`, `cal`, `kcal`, `Mcal`, `Gcal`, `thm` |
| `binary_sensor` / `motion`    | `motion`      | Boolean; `meaning = motion_detected`     | None                                                                                                 |
| `binary_sensor` / `occupancy` | `motion`      | Boolean; `meaning = occupied`            | None                                                                                                 |
| `binary_sensor` / `moisture`  | `leak`        | Boolean; `meaning = moisture_detected`   | None                                                                                                 |
| `binary_sensor` / `battery`   | `battery`     | Boolean; `meaning = low_battery`         | None                                                                                                 |

Each reading has `entity_id`, `kind`, `device_class`, `state_class`, `state`, `value`, `unit`, `meaning`,
`availability`, `stale`, `usable`, `reason`, `observed_at` and `last_updated`. Numeric states must be
complete finite decimal numbers, without surrounding spaces, a leading plus sign, underscores, `inf`
or `nan`. `value` is `null` whenever the entity is not available, never zero or `false`. `usable`
requires availability, freshness, no recorded polling error and no `reason`. Reasons are
`no finite numeric reading`, `unit missing or unrecognized; no conversion attempted`,
`reported percentage is outside 0 to 100` and `no known binary state`. Other classes, and entities
without one, appear under `unclassified` with only availability, staleness and a fixed reason.

Units are exactly what the state reports, which can already reflect Home Assistant's own display-unit
setting. There is no conversion, total, integration, tariff or cost calculation, and an energy meter
reading is not a household total.

- `source = live` (default) uses one `GET /api/states/<entity_id>` for a named sensor, otherwise one
  `GET /api/states` filtered locally; readings are `stale = false` at the local read time.
- `source = cached` requires `poll.enabled`, is offline, and reads the polling snapshots. A named sensor
  must be in `poll.entities`. Never-observed entities are unclassified as `not_observed`. Staleness comes
  from each snapshot's age, and a recorded polling error appears as `source_error` and makes every
  reading unusable.
- `limit` caps classified readings first and fills remaining slots with unclassified entries; `truncated`
  reports any omission. `kind` filters classified readings only. `room` restricts and never expands.

## Polling, events and storage

Polling is opt-in sampling, not a subscription to every Home Assistant state transition.

When `poll.enabled` is `true`, registration adds the `home.poll` job at `interval_seconds`. With the
current core, its first run is due at the next runtime tick; the runtime applies the job deadline and
cancellation to the web transfer. `home_poll` runs the same sample on demand and is refused while polling
is disabled.

Each sample makes exactly one `GET /api/states`, capped at 1,048,576 bytes, and validates the whole list
as `home_states` does: malformed or duplicate states fail the sample. Configured entities absent from
the list become `availability = missing` with `state = null`. Snapshots keep the same allowlisted
attributes as `home_states`, plus `availability`, `reported_leaf`, provider timestamps and the local
`observed_at`. A snapshot whose serialized size exceeds 8,192 bytes keeps an empty `attributes` object
and `attributes_omitted = true`, so two snapshots and the envelope fit the core's 20,000-byte event
payload bound.

### Change detection and events

- The first successful observation of each configured entity on a server is a silent baseline at revision 1.
- A later difference in `state`, retained `attributes`, `attributes_omitted`, `availability` or
  `reported_leaf` queues one `home.state_changed` event and increments the revision.
- Changes in provider timestamps or the local observation time alone queue nothing, although the stored
  snapshot's observation time advances.
- `missing`, `unknown`, `unavailable` and recovery are sampled transitions. Changes between samples are
  not seen.
- Staleness is computed when read and never creates an event. Poll failures update health only.

Events have source `home`, type `home.state_changed`, and this payload:

| Key                | Meaning                                                                         |
| ------------------ | ------------------------------------------------------------------------------- |
| `entity_id`        | The configured entity.                                                          |
| `previous`         | The last stored snapshot, including its own `observed_at`.                      |
| `current`          | The new minimized snapshot.                                                     |
| `observed_at`      | Local read time of this sample, rendered `YYYY-MM-DDTHH:MM:SSZ`.                |
| `revision`         | Per-entity revision after this change, starting at 2.                           |
| `source`           | Opaque 64-character digest of the normalized server origin, never the address.  |
| `history_complete` | Always `false`.                                                                 |

The deduplication key is `native:home:<source>:<entity_id>:<revision>`. Snapshot writes, revision
changes, health and event inserts through `Context.emit` on the same database connection happen in one
transaction; any failure rolls all of them back and records only failure health. A sample that started
before the recorded attempt is ignored (`ignored_older_sample = true`), so it cannot overwrite newer state.
`home_poll` returns `entities`, `baselined`, `changed`, `observed_at`, `source`, `ignored_older_sample`
and `history_complete = false`. This is local atomicity, not exactly-once external delivery.

### Storage

| Table                     | Columns                                                                                                                            |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `native_home_snapshots`   | `source` and `entity_id` (primary key), `snapshot_json`, `observed_at` (integer seconds), `revision` (1 to 9,007,199,254,740,991). |
| `native_home_poll_status` | `source` (primary key), `attempted_at`, `succeeded_at`, `error` (a safe error code, never provider text), `consecutive_failures`.  |

There are no other home migrations, and Python `home:1` tables are neither read nor migrated. Changing
servers starts fresh baselines under a new source. Rows for removed entities or other servers are kept
but filtered from reads; there is no home retention job, and event payload retention follows the core's
event retention. A corrupt stored snapshot fails each sample with code `home_cache` until the operator
restores or removes that row.

### Health and staleness

`home_poll_status` is offline and returns `enabled`, `source`, `interval_seconds`, `stale_after_seconds`,
`attempted_at`, `succeeded_at`, `error`, `consecutive_failures`, overall `stale`, `entities`,
`history_complete = false` and `freshness_basis`. Entities follow sorted configured order. Before a
baseline each is `{"entity_id": ..., "availability": "not_observed", "revision": 0, "stale": true}`;
afterwards it is the stored snapshot plus `revision` and `stale`. An observation is stale when it is
older than `stale_after_seconds` or lies in the local future; overall `stale` uses the last success.

A failed sample raises code `home_poll` with a fixed message, keeps every cached snapshot, and records
an error code such as `home_http` (refused, failed or oversized read), `home` (malformed or duplicate
list), `home_secret`, `home_cache` or `database`; anything else is recorded as `internal`. Freshness means
the age of the **local read**; a successful read does not prove the physical integration is current.
Polling misses intermediate changes, transient motion or leak signals, downtime and fast toggles.

## Native versus Python

The Python reference offers twelve tools when speech is enabled; this native adapter offers eleven,
including the explicit `home_capabilities` read. There is still no speech or quiet-hours support.
This is not a drop-in Python configuration port.

| Python reference feature                                   | Native coverage or explicit limit                                                                                                                                                                                             |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `home_states`                                              | Live reads remain, but exposure is exact-only, six hard domains, additional room restriction and richer safe capability observations. No legacy permissive-domain behavior.                                                   |
| `home_targets`, typed light/climate/fan/media tools        | Implemented with native-only canonical binding, concrete schemas, one-shot effects and independent observed/unconfirmed receipts. Bindings/results are not Python-compatible state formats.                                   |
| Exposure globs; replaceable legacy deny list               | Rejected/narrowed: exact entities only; additional denials cannot remove hard restrictions.                                                                                                                                   |
| Reverse-proxy path in `base_url`; command secrets          | Rejected: origin-only endpoints and `env:`/`file:` references.                                                                                                                                                                |
| `home_call_service`                                        | Not registered. No generic service, arbitrary `data`, select/number/switch actions, scenes, scripts or legacy profile bypass.                                                                                                 |
| `home_lighting_preset`                                     | Implemented. Presets use `targets` keyed by light instead of an `actions` list, have no `transition`, use native-only fingerprints, and add `stop_reason`, `partial` and a deadline or cancellation stop.                     |
| `home_sensor_summary`                                      | Implemented with the same kinds and unit lists, plus `binary_sensor` `occupancy` under `motion` with `meaning = occupied`. Numeric states are parsed more strictly.                                                           |
| `home_poll`, `home_poll_status`, `home.state_changed`      | Implemented with native tables, integer-second timestamps, a `native:` deduplication prefix, a native source digest, intervals up to 86,400 seconds (Python: 3,600), `consecutive_failures` and an 8,192-byte snapshot bound. |
| Change detection                                           | Native also compares `reported_leaf` and `attributes_omitted`; Python compares state, retained attributes and availability.                                                                                                   |
| Confirmation fields                                        | Native omits `confirmed` when no readback ran and adds top-level `effect_unknown` for unknown submissions; the Python reference returns `confirmed = false` for rejections and has no home `effect_unknown`.                  |
| Light transitions/fades                                    | Not supported. `transition` inputs are rejected for lights and presets.                                                                                                                                                       |
| Quiet hours and time-zone handling                         | Not supported. `quiet_hours`, standalone `timezone` and other unknown settings are rejected; there is no hidden time-based governor.                                                                                          |
| Speech/announcements and provider-privacy declarations     | Not supported. `speech` settings are rejected and `home_announce` is absent. No speech engine or audible-delivery claim.                                                                                                      |
| Additional device controls                                 | No fan presets/direction/oscillation, climate auto/dry/humidity/presets, media content/seek/source/grouping, light effects/flashes, or unsupported domains. Unknown inputs fail closed.                                       |

The native adapter is stricter about reported groups and known light modes. It checks the effective
climate mode on temperature readback and does not return the action reply's changed-state list.
There is no physical delivery verification in either implementation.

## Coverage and UNRUN tests

[`native/tests/home.cpp`](../tests/home.cpp) includes `support.hpp`, its deterministic `FakeHttp`,
and a wrapper that can throw on a fake submission. No test creates a real transport.
Fixtures, if explicitly run later, create owner-private directories relative to the test's working
directory, not the shared support helper's system temporary directory. They restore fake environment
credentials and remove their own fixtures. The fixture clock is adjustable for staleness tests; the
atomicity test adds and drops temporary SQLite triggers in its own fixture database; one preset test
installs `ScopedExecutionLimits` around a direct handler call.

All 40 test functions below are **authored, UNRUN**. The described assertions are intended coverage,
not passed results:

| Test function(s)                                               | Authored assertions                                                                                                                                                                                                                            |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_disabled_and_offline_registration`                       | Disabled early return without tables; eleven tools and tiers; two empty home tables; no token resolution, requests, jobs, snapshots or events; offline disabled-poll status and refused `home_poll`; empty allowlists and presets fail closed. |
| `test_settings_reject_unsupported_and_unsafe_scope`            | Unsupported speech, quiet-hours, time-zone and retry settings even when disabled; malformed/nested values, endpoints, secrets, duplicates, globs, unsafe domains and limits.                                                                   |
| `test_canonical_binding_and_reload`                            | Stable normalized sets/defaults/numbers/origin/credential references; polling excluded; every reviewed configuration class, including presets, changes the binding; stale arguments rejected.                                                  |
| `test_schemas_bind_targets_and_bound_shapes`                   | Catalog target/binding enumeration; numeric/array limits and types; exposed room membership cannot bypass a missing profile.                                                                                                                   |
| `test_plain_http_requires_explicit_opt_in`                     | Secure default and explicit exception forwarding.                                                                                                                                                                                              |
| `test_states_exact_requests_and_redaction`                     | Exact headers/method/address, response cap, timeout, filtering/sorting/truncation and attribute redaction.                                                                                                                                     |
| `test_states_single_entity_room_and_validation`                | Exact one-entity request, room restriction, path injection, conflicting domain and invalid shapes.                                                                                                                                             |
| `test_states_malformed_duplicate_and_capped_responses`         | Invalid/duplicate states, oversized bodies and safe provider-refusal errors.                                                                                                                                                                   |
| `test_capabilities_are_live_and_advisory`                      | Live feature/profile/unit reporting, unavailable status, sensor non-inference and zero effects.                                                                                                                                                |
| `test_light_exact_request_and_observed_readback`               | Exact brightness/color and kelvin bodies, headers/caps, independent observed state, no physical-delivery claim.                                                                                                                                |
| `test_multi_light_preflight_and_concrete_room_scope`           | All preflights before one exact multi-target post, per-target readback, no partial submission on preflight failure, no room expansion.                                                                                                         |
| `test_light_rejects_conflicts_groups_and_missing_capabilities` | Duplicates, incompatible settings, unsupported transitions, target injection, missing color/brightness support, group metadata, availability and kelvin bounds.                                                                                |
| `test_climate_single_and_range_requests`                       | Exact single/range service bodies and observed results.                                                                                                                                                                                        |
| `test_climate_modes_and_fahrenheit`                            | Mode-only off without setpoints, bounded entry into heat, explicit Fahrenheit with no conversion.                                                                                                                                              |
| `test_climate_refuses_unit_bounds_caps_and_change`             | Unit mismatch, incomplete/reversed/mixed ranges, unreviewed modes, missing/boolean features, absent prior setpoints, bounds/change caps and unsafe mode-only heating.                                                                          |
| `test_fan_limits_change_and_zero`                              | Exact bounded percentage request, zero/off behavior and independent confirmation.                                                                                                                                                              |
| `test_fan_rejects_unknown_percentage_and_features`             | Maximum/change limits, missing/invalid prior percentages, absent features and groups.                                                                                                                                                          |
| `test_media_actions_exact_requests`                            | Play/pause/stop/volume/mute/unmute exact bodies; no implicit auxiliary action.                                                                                                                                                                 |
| `test_media_refuses_volume_group_features_and_values`          | Action/value pairing, content/group rejection, unsafe/unknown volume, missing feature, grouped player and explicit mute with unknown volume.                                                                                                   |
| `test_readback_mismatch_unavailable_and_profile_tolerance`     | Mismatch/unavailability, tolerance outside a profile/color-channel bound, changed group membership and lost media capability; no resend.                                                                                                       |
| `test_submission_unknown_and_rejected_without_retry`           | Fake 503/500/408/zero ambiguity with `confirmed = false` and `effect_unknown`; 403/401/302/429 refusal without `confirmed`; thrown transport error; at-most-one submission.                                                                    |
| `test_post_body_is_not_confirmation`                           | Changed-state reply cannot replace readback; unreadable success body; over-cap ambiguity declares an unknown effect.                                                                                                                           |
| `test_readback_failure_is_reported_without_resend`             | 404, non-JSON, wrong entity and malformed attributes become unconfirmed receipts without an unknown effect, not resubmissions.                                                                                                                 |
| `test_secret_resolution_is_lazy_and_errors_are_redacted`       | Offline targets with an invalid fake credential; safe failure before transport.                                                                                                                                                                |
| `test_runtime_default_approval_and_single_submission`          | Assistant/rule default pending approvals with no preflight, deny, fresh preflight on approval and single-use execution.                                                                                                                        |
| `test_runtime_reload_rejects_stale_approval`                   | Pending approval persisted across runtime destruction; changed limit rejects it before provider work.                                                                                                                                          |
| `test_runtime_credential_rotation_preserves_review_binding`    | Pending approval survives secret-reference-only rotation and resolves the new fake credential at execution.                                                                                                                                    |
| `test_runtime_policy_rechecked_on_approval_and_human_calls`    | A new deny policy wins over a persisted approval and trusted human origin.                                                                                                                                                                     |
| `test_poll_and_preset_settings_are_strict`                     | Invalid poll types, ranges, entities, staleness and keys; invalid preset names, targets, states, bounds, mixes, transitions, services, scenes and 17 targets; offline boundary values.                                                         |
| `test_poll_registration_job_and_status_before_baseline`        | One `home.poll` job at its interval; no sampling at registration; not-observed status; opaque source; default staleness; polling excluded from the binding.                                                                                    |
| `test_poll_silent_baseline_and_change_events`                  | Silent baselines; timestamp-only samples; one event per change with envelope, deduplication key and revision; snapshot equals event; missing/unavailable transitions; job path.                                                                |
| `test_poll_change_event_is_atomic_with_snapshot`               | Synthetic trigger refusals of a snapshot write and of an event insert roll back every snapshot and event of the sample; only failure health remains; recovery queues once.                                                                     |
| `test_poll_failure_staleness_and_status`                       | Threshold staleness; refused and malformed lists record safe codes, consecutive failures and retained snapshots; recovery; older samples ignored; future observations stale.                                                                   |
| `test_poll_minimized_observations_and_source_namespace`        | Unlisted attributes and provider context excluded; oversized attributes omitted within the payload bound; another server gets fresh silent baselines.                                                                                          |
| `test_lighting_preset_binding_and_stale_configuration`         | Fingerprints and content in targets; catalog pins; content change alters one fingerprint and the binding; stale, swapped, unknown and extended requests refused offline.                                                                       |
| `test_lighting_preset_individual_submissions_and_readback`     | All preflights first; one exact light submission and readback per target; no scene or script calls; preflight or capability failure sends nothing.                                                                                             |
| `test_lighting_preset_partial_results_and_effect_unknown`      | Later refusal is partial; unknown stops with top-level `effect_unknown`; first refusal rejects without `confirmed`; unconfirmed target continues; deadline and cancellation stops.                                                             |
| `test_sensor_summary_live_classification_and_units`            | Kinds, unchanged units, unusable unknown units and percentages, null unavailable values, motion/occupancy/moisture meanings, unclassified entries, filters, rooms, invalid scopes.                                                             |
| `test_sensor_summary_cached_staleness_and_scope`               | Offline not-observed entries; cached readings from snapshots; recorded poll error makes readings unusable; age staleness; polling allowlist.                                                                                                   |
| `test_runtime_unknown_submission_is_journaled_uncertain`       | Through the runtime, an unknown submission is journaled `uncertain` and blocks an identical request; an observed one is `succeeded`. Depends on the shared journal.                                                                            |

No runtime coverage, physical effects, credentials/file permissions against a real installation,
Home Assistant version compatibility, network/certificate reachability, build/link success,
concurrency/races, device quantization, SQLite trigger behavior, or hidden provider integration behavior
has been verified. Approval and journal tests exercise the shared runtime by authored calls, not by
running it during development.

## Syntax-only verification

From the repository root, these portable templates describe the syntax and formatting checks.
Replace the dependency-directory placeholder with your locally restored header directory:

```bash
c++ -std=c++20 -Wall -Wextra -Wpedantic -Werror -fsyntax-only \
    -I native/include \
    -isystem /path/to/validation-dependencies/usr/include \
    -isystem /path/to/validation-dependencies/usr/include/x86_64-linux-gnu \
    native/src/home.cpp native/tests/home.cpp
clang-format-14 --dry-run --Werror native/src/home.cpp native/tests/home.cpp
```

These check only the two translation units; they neither link the native engine nor execute any
test. Execution-limit declarations used here are defined in shared runtime sources outside this
adapter. Do not interpret the checks as provider compatibility or runtime coverage. CMake
configuration/build and test collection/execution were not performed.

Authoring result on 2026-11-11: the system `c++` 11.4 compiler exited 0 with no diagnostics for either
translation unit, and the clang-format dry run reported no changes. The example JSON was parsed as data
only (ASCII, no duplicate keys, preset and polling references consistent), without importing or executing
project code.

## Public references

- [REST authentication, state reads and action replies][rest].
- [Light color modes, brightness and color-temperature attributes][light].
- [Climate reported units, modes, bounds and feature semantics][climate].
- [Single/range climate action data][climate-action].
- [Fan percentages and speed conversion limits][fan].
- [Media feature flags, states, volume and group membership][media].
- [Sensor device classes, units and state classes][sensor].
- [Binary sensor device classes and on/off meanings][binary-sensor].
- Provider bit constants inspected at commit `0dc24540ce48f65f616230857e2cf21ceec92b80`:
  [climate][climate-flags], [fan][fan-flags], [media player][media-flags].
  The adapter uses climate target bits 1/2, fan speed bit 1, and media pause/play/stop/volume/mute
  bits 1/16384/4096/4/8 respectively. No feature is inferred from entity names.
- SQLite [trigger `RAISE` semantics][sqlite-trigger] and the [upsert clause][sqlite-upsert], as used by
  the authored atomicity test.

[rest]: https://contributors.home-assistant.io/core/internals/rest-api/
[light]: https://contributors.home-assistant.io/core/entities/light/
[climate]: https://contributors.home-assistant.io/core/entities/climate/
[climate-action]: https://www.home-assistant.io/actions/climate.set_temperature/
[fan]: https://contributors.home-assistant.io/core/entities/fan/
[media]: https://contributors.home-assistant.io/core/entities/media-player/
[sensor]: https://contributors.home-assistant.io/core/entities/sensor/
[binary-sensor]: https://contributors.home-assistant.io/core/entities/binary-sensor/
[climate-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/climate/const.py
[fan-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/fan/const.py
[media-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/media_player/const.py
[sqlite-trigger]: https://www.sqlite.org/lang_createtrigger.html
[sqlite-upsert]: https://www.sqlite.org/lang_upsert.html

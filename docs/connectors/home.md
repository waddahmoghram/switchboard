# Home connector

The connector uses Home Assistant's Representational State Transfer (REST) application programming interface (API).
It preserves the two original tools and adds explicit device profiles, room aliases, lighting-only presets, sensor summaries, opt-in sampling and opt-in speaker announcements.
Python 3.11 or newer and the standard library are sufficient; system time-zone data is needed for configured quiet hours.

**This is not a safety controller.**
Locks, garage doors, alarms, cameras, appliance relays and other high-power appliance controls are outside the new features.
Keep physical safety mechanisms, Home Assistant safety automations and manual controls independent.
Device types and names do not prove what a device controls: you must review every concrete entity, especially thermostats and integrations with hidden side effects.

All added tests are authored but unexecuted (`UNRUN`).
No Switchboard process, test, example, device or account was run or probed while authoring this extension.
The instructions below describe operation on **your machine**, not verified operation on the author's machine.

## Tools and registration

Tool inputs and results use JavaScript Object Notation (JSON).
The literal policy tiers `READ` and `ACT` mean read-only and externally acting tools respectively.
Assistant and rule actions ask for approval by default; typing a direct human command counts as approval.
Inspect the inner result, not just the dispatcher's `ok`.

| Tool                   | Tier | Purpose                                                     |
| ---------------------- | ---- | ----------------------------------------------------------- |
| `home_states`          | READ | Legacy exposed states or one entity                         |
| `home_call_service`    | ACT  | Legacy single-entity action, only on unprofiled targets     |
| `home_targets`         | READ | Offline profiles, rooms, presets and current action binding |
| `home_sensor_summary`  | READ | Live or cached readings, with original units and quality    |
| `home_poll_status`     | READ | Offline polling health, snapshots and freshness             |
| `home_poll`            | READ | One sample, only when polling is enabled                    |
| `home_lights`          | ACT  | Explicit lights: on/off, brightness, color and fades        |
| `home_climate`         | ACT  | One thermostat: bounded unit-aware setpoint/range/mode      |
| `home_fan`             | ACT  | One fan: bounded percentage                                 |
| `home_media`           | ACT  | One player: play/pause/stop/volume/mute                     |
| `home_lighting_preset` | ACT  | Configured, content-pinned lighting settings only           |
| `home_announce`        | ACT  | Optional, explicitly approved short speaker announcement    |

`home_announce` is registered only when speech is enabled.
The other tools are always registered; an unconfigured typed target or disabled poll fails explicitly rather than inferring a device.

Registration parses configuration, checks the speech policy, registers tools, provides the internal `home` service and, if enabled, adds the `home.poll` job.
It performs no network request and resolves no secret.
Application assembly applies the additive `home:1` database migration even when polling is disabled.
There are no event subscriptions, new command-line subcommands or dependencies on other connectors.

The internal service is `HomeService` in [`service.py`](../../src/switchboard/connectors/home/service.py).
Trusted callers must use the dispatcher for externally acting tools; directly calling a service is not a second approval boundary.

## Setup on your machine

1. Use your existing Switchboard installation and configuration.
   Append `home` to the existing `[switchboard] connectors` list; do not overwrite other enabled connectors.
1. In Home Assistant, create a dedicated non-administrator user and a long-lived access token under the profile's **Security** section.
   This avoids administrator-only endpoints, but is not an entity-scoped token.
   The connector's allowlists are an additional software boundary.
1. Save the token in an owner-readable secret file, or use an existing supported secret reference.
   The connector never creates users, speech engines, device entities, scenes, scripts or automations.
1. Merge [the complete example](../config/home.toml), replacing every fictional address and entity.
   Keep polling and speech disabled until you explicitly choose to enable them.
1. Inspect each entity's state, attributes, supported features, grouping and physical effect in Home Assistant.
   Approve leaf devices only.
   A room alias is a local list, not a Home Assistant area, floor, label or device.
1. Choose an installed time zone and sensible climate/volume limits.
   Check the thermostat's **reported** `temperature_unit`, not its native hardware unit or a guessed locale.
1. On your machine, run the diagnostic and read-only checks below.
   The diagnostic resolves the token locally but does not test the network; read tools are the first deliberate provider requests.

```bash
switchboard doctor
switchboard call home_targets
switchboard call home_states --args '{"limit":10}'
switchboard call home_sensor_summary --args '{"room":"living","source":"live"}'
switchboard call home_poll_status
```

The default configuration path is `~/.config/switchboard/config.toml`.
Use the global `--config PATH` option before the command if you choose a different file.
`base_url` normally uses an address starting with `https://`.
Plain Hypertext Transfer Protocol (HTTP) is refused unless `allow_http = true`; that exception sends the token without encryption and should be limited to a network you control.
If your existing `[mcp] expose` list names only the legacy tools, add the reviewed new tool names there too.
Entity names, states and attributes are untrusted data, never instructions or authority to choose new targets.

No raw access token belongs in configuration, arguments, events or returned data.
References such as `env:NAME` and `file:~/.config/switchboard/secrets/home-assistant` are resolved only inside requests.
Revoking the dedicated user's token in Home Assistant remains an independent way to stop access.

## Configuration reference

Unknown keys, malformed types, non-finite numbers, duplicates and disallowed targets raise `ConfigError`.
Every new entity list contains exact lowercase identifiers, not globs, and every entry must also be exposed and not denied.
Lists are capped at 200 entries; profile and room tables are capped at 100 entries.

### Base settings and compatibility

| Setting        | Default                                          | Meaning                                        |
| -------------- | ------------------------------------------------ | ---------------------------------------------- |
| `base_url`     | required                                         | Server address, no credentials/query/fragment  |
| `token`        | required                                         | Secret reference, never a resolved value       |
| `allow_http`   | `false`                                          | Explicit plain-HTTP exception                  |
| `expose`       | `[]`                                             | Up to 100 entity globs, each at most 255 chars |
| `deny_domains` | `lock`, `alarm_control_panel`, `cover`, `camera` | Legacy domain deny list, always wins           |
| `lights`       | `[]`                                             | Exact reviewed leaf lights                     |
| `rooms`        | `{}`                                             | Alias-to-exact-entity-list mapping             |

The legacy `deny_domains` list still **replaces** its default when explicitly set.
Legacy reads preserve that behavior.
New rooms and polling accept only `sensor`, `binary_sensor`, `light`, `climate`, `fan` and `media_player` domains.
Speech engines must be exact `tts` entities.
New lists cannot override these domain restrictions to include locks, covers, alarms, cameras, switches, buttons, scripts, scenes or automations.
A read-only leak sensor is not an alarm actuator and is not a replacement for a leak alarm.

`home_call_service` keeps its existing schema and result for **unprofiled** existing configurations.
There is no new required binding on that legacy tool.
However, it refuses every control entity in `lights`, `climate`, `fans`, `media_players` or a new `rooms` list, and directs callers to a bound typed tool.
Speech also refuses generic `tts` calls when enabled.
This deliberate compatibility boundary prevents legacy `volume_up`, mode changes, toggles or preset-like calls from bypassing newly configured restrictions.
An old generic approval also fails if its target gains a profile before execution.

Unprofiled generic actions are still broad.
They are not a way to obtain the new safety guarantees.
Do not expose appliance relays, garage controls or unreviewed `script`, `scene` or `automation` entities; those entities can indirectly affect denied domains.
Prefer exact exposure and consider denying `home_call_service` entirely after migrating to typed tools.

### Device profiles and presets

| Table                               | Fields and bounds                                                     |
| ----------------------------------- | --------------------------------------------------------------------- |
| `climate."climate.NAME"`            | Required `unit`, `minimum`, `maximum`, `modes`; optional `max_change` |
| `fans."fan.NAME"`                   | `minimum` 0, `maximum` 100, `max_change` 100 by default, in percent   |
| `media_players."media_player.NAME"` | `minimum` defaults to 0; required `maximum`, both from 0 to 1         |
| `lighting_presets.NAME`             | Required `actions`: 1 to 16 distinct, approved leaf-light settings    |

Climate `unit` is `C` for Celsius or `F` for Fahrenheit.
Hard configuration bounds are -50 to 100 Celsius or -58 to 212 Fahrenheit; configure a much narrower suitable range.
`maximum` cannot be below `minimum`.
`max_change` defaults to 2 in the configured unit and must be 0.1 to 20.
Allowed reviewed modes are `off`, `heat`, `cool`, `heat_cool` and `fan_only`; `modes` must explicitly list the subset you approved.
Runtime checks intersect configured limits with reported `min_temp` and `max_temp`, verify the reported unit and capabilities, and compare each new setpoint to its previous reported value.
Unknown values fail closed.
No automatic temperature conversion, clamping or rounding is performed.
Entering `heat`, `cool` or `heat_cool` without a new temperature also requires the existing reported setpoint/range to be known, ordered and within bounds.
It cannot energize an already unsafe setpoint.
Readback checks the reported unit as well as the verified setpoint values; mode-only `off` does not need a setpoint.

Fan bounds are integer percentages from 0 to 100, with `maximum >= minimum` and `max_change` from 1 to 100.
The previous percentage and reported speed-control feature are required.
A change cap can also refuse a large reduction; manual control remains available independently.
There is no conversion to revolutions per minute.

Media volume bounds are fractions from 0 to 1.
They do not measure acoustic loudness or guarantee hearing safety.
Play and unmute require an observed volume within bounds.
Volume changes must themselves fit the profile.
The connector never raises volume, unmutes, turns on a player or groups speakers as an implicit side effect.

Preset actions accept only `entity_id`, `state`, `brightness_pct`, `rgb_color`, `color_temp_kelvin` and `transition`, using the lighting tool's bounds below.
Off actions cannot set brightness/color, two color representations cannot be mixed, and a preset cannot repeat a target.
It cannot name a service, execute a Home Assistant scene/script, or supply arbitrary action data.
All targets are preflighted before any effect.
Preset effects are sequential, not atomic; an ambiguous or rejected submission stops remaining actions.
Accepted fades may continue to the next explicitly reviewed preset action while remaining unconfirmed.
There is no compensation, rollback, delayed retry or automatic resend.

### Polling, quiet hours and speech settings

| Table         | Setting               | Default and bounds                                  |
| ------------- | --------------------- | --------------------------------------------------- |
| `poll`        | `enabled`             | `false`                                             |
| `poll`        | `entities`            | Exact exposed entities; nonempty when enabled       |
| `poll`        | `interval_seconds`    | 60; integer 30 to 3,600                             |
| `poll`        | `stale_after_seconds` | At least the interval; default max(300, 3*interval) |
| `quiet_hours` | `enabled`             | `false`; must be true before enabling speech        |
| `quiet_hours` | `timezone`            | System zone key `UTC`; must be installed locally    |
| `quiet_hours` | `start`, `end`        | `22:00`, `07:00`; distinct 24-hour `HH:MM` values   |
| `speech`      | `enabled`             | `false`                                             |
| `speech`      | `engine`              | Required when enabled; exposed exact `tts` entity   |
| `speech`      | `destinations`        | Required when enabled; exact profiled media players |
| `speech`      | `external_processing` | Explicit declaration required when speech enabled   |
| `speech`      | `allow_external`      | `false`                                             |
| `speech`      | `allow_sensitive`     | `false`; cannot be true with external processing    |

`stale_after_seconds` is an integer from 30 to 86,400.
Speech engine and destination identifiers are limited to 64 characters each so the complete announcement summary fits the approval display.

Quiet hours use the configured zone at **execution time**, with an inclusive start and exclusive end.
Overnight intervals and daylight-saving changes follow the system time-zone database.
They block all announcements, play, unmute and volume increases.
Pause, stop, mute and known volume reductions remain possible through typed tools.
There is no assistant-chosen quiet-hour override.

Speech needs an exact `[policy.tools.home_announce]` rule with `decision = "ask"` or `"deny"`.
Missing rules, a matching glob alone, or an exact `"allow"` fail registration while speech is enabled.
The core additionally rechecks recipient allowlists, rate limits, denials and its kill switch on approval.

`external_processing` is your provider-review declaration, **not** automatically verified routing.
If the existing engine sends text/audio to an outside service, declare it and explicitly opt into `allow_external`.
Sensitive speech remains refused for external processing.
The `sensitive` input is a caller declaration, not a semantic privacy classifier: do not broadcast secrets just because it is false.
Approved text and arguments remain in the private approval/audit database; `cache = false` only requests that Home Assistant not cache synthesis output.
It does not promise erasure from provider/device logs.

## Binding approvals to configuration and targets

Read `home_targets` immediately before constructing **each new action**.
It returns `binding`, a 64-character digest of canonical configuration data.
Every typed action, preset and announcement requires that exact value.
Its registered schema has `enum = [current_binding]`, and its handler checks it again.
Approving stored arguments under a newly assembled configuration revalidates the schema before sending.

Binding version 1 includes the component `home`, normalized configured endpoint, `allow_http`, exposure, deny domains, exact light/room/device sets, climate/fan/media limits, full reviewed preset content, quiet hours and speech engine/destination/privacy settings.
It excludes both secret references and resolved credentials, and excludes read-only polling settings.
A credential rotation does not invalidate the action binding; changing an endpoint, alias, preset, engine or safety profile does.

Room actions still require **concrete** `entity_id` or `entity_ids`; `room` is only an additional membership check and never expands to whatever the room contains later.
Presets additionally require the separate `fingerprint` returned beside their full actions.
No new argument accepts arbitrary target fields.

If configuration changes, the old approval fails.
Re-read targets, inspect the new binding and submit a new action for approval.
Do not automatically replace the digest in an old queued request.
Long-lived runner and assistant-facing processes do not watch configuration files: restart/reload them deliberately.
The next assembled application, including an approval command, uses the newly loaded settings.
Provider-side entity remapping or undisclosed integration side effects cannot be detected by this digest.

Review exact stored arguments rather than relying on a shortened multi-light summary:

```bash
switchboard approvals list
switchboard approvals show APPROVAL_ID
switchboard approvals approve APPROVAL_ID
switchboard call approvals_status --args '{"id":"APPROVAL_ID"}'
```

Replace `APPROVAL_ID` with the real pending identifier.
The core's durable `executed` status means its handler finished, **not** that physical playback, a fade or room temperature was achieved.

## Typed action arguments and result interpretation

The examples below are JSON request bodies, not executed commands.
Replace `CURRENT_BINDING` and `CURRENT_PRESET_FINGERPRINT` with the values you just read; placeholder strings fail validation.

### Lighting

`home_lights` requires `binding`, `entity_ids` (1 to 16 distinct approved leaf lights) and `state` (`on`/`off`).
Optional `room` checks every explicitly supplied entity against the configured alias.
Red, green and blue (RGB) color values are passed to Home Assistant without connector-side color conversion.

| Argument            | Bounds                          | Runtime requirement                         |
| ------------------- | ------------------------------- | ------------------------------------------- |
| `brightness_pct`    | Integer 1 to 100                | Reported dimming capability                 |
| `rgb_color`         | Three integers, each 0 to 255   | Reported color capability                   |
| `color_temp_kelvin` | Integer 1,000 to 20,000         | Reported color-temperature mode and bounds  |
| `transition`        | Number 0 to 300 seconds         | Reported transition feature when positive   |

Choose either RGB or kelvin, not both.
Set `state = "off"` instead of a zero typed brightness.
Reported groups with other members are refused, even when some members happen to be exposed.
Home Assistant may translate an accepted RGB color into the device's supported format; this is not a promise that every device can reproduce every color.

```json
{
  "binding": "CURRENT_BINDING",
  "room": "living",
  "entity_ids": ["light.living_lamp"],
  "state": "on",
  "brightness_pct": 40,
  "color_temp_kelvin": 3000,
  "transition": 2
}
```

`home_lighting_preset` takes only `binding`, `name` and the preset `fingerprint`:

```json
{
  "binding": "CURRENT_BINDING",
  "name": "reading",
  "fingerprint": "CURRENT_PRESET_FINGERPRINT"
}
```

### Climate and fan

`home_climate` requires `binding`, one `entity_id`, and `unit` (`C`/`F`); `room` is optional.
Supply one `temperature`, both ordered `target_temp_low` and `target_temp_high`, or a reviewed `hvac_mode`.
Do not mix a single temperature with a range.
`heat_cool` needs the range; `heat`/`cool` need one temperature.
Temperature changes need a reviewed heating/cooling mode and reported setpoint capability.
Mode-only `off` does not pretend that a temperature was reached.

```json
{
  "binding": "CURRENT_BINDING",
  "entity_id": "climate.living_thermostat",
  "unit": "C",
  "temperature": 22,
  "hvac_mode": "heat"
}
```

`home_fan` requires `binding`, one `entity_id` and integer `percentage` from 0 to 100; `room` is optional.
Device bounds and `max_change` narrow that range.
The observed percentage is compared directly, never converted from another speed unit.

```json
{
  "binding": "CURRENT_BINDING",
  "entity_id": "fan.living_fan",
  "percentage": 40
}
```

### Media

`home_media` requires `binding`, one `entity_id` and `action`, with optional `room`.
`action` is `play`, `pause`, `stop`, `volume` or `mute`.
Volume requires only `volume_level`;
mute requires only boolean `is_volume_muted`; play/pause/stop accept neither extra value.
The player must report the action's supported feature.
No media address, source, grouping or shell command can be supplied.

```json
{
  "binding": "CURRENT_BINDING",
  "entity_id": "media_player.living_speaker",
  "action": "volume",
  "volume_level": 0.3
}
```

### Speaker announcements

The existing text-to-speech (TTS) engine must already work in your Home Assistant installation.
`home_announce` requires `binding`, exact `engine`, exact destination `entity_id`, `text`, `volume_level` and boolean `sensitive`.
Text is 1 to 140 printable basic Latin characters (code points 32 to 126), nonblank and without `<`/`>` markup.
This intentionally limited input keeps the full text, engine, destination, volume and privacy declaration visible in the approval summary.

The speaker must already be available, unmuted, ungrouped, capable of media playback and at the approved volume within 0.01, inside its profile.
Adjust volume or mute in a **separate** approved `home_media` request if needed; the announcement does not do it for you.

```json
{
  "binding": "CURRENT_BINDING",
  "engine": "tts.existing_local_voice",
  "entity_id": "media_player.living_speaker",
  "text": "The kitchen timer has finished.",
  "volume_level": 0.3,
  "sensitive": false
}
```

The exact verified call is `POST /api/services/tts/speak`, with the engine as `entity_id` and the speaker as `media_player_entity_id`:

```json
{
  "entity_id": "tts.existing_local_voice",
  "media_player_entity_id": "media_player.living_speaker",
  "message": "The kitchen timer has finished.",
  "cache": false
}
```

This combines the provider's [Speak action][speak] target/data shape with its [REST action endpoint][rest].
It does not call a legacy `say` action, generate a media address, run a script, or pass arbitrary engine options.
Delivery/playback **cannot always be verified**.
Even a `playing` readback does not identify what audio played or prove anybody heard it.
Every accepted or ambiguous announcement remains `requested_unconfirmed`,
with `confirmed = false` and `delivery_verified = false`.

### Acceptance is not completion

Each new action submits once, disables redirects, and independently reads back only its intended targets.
There is no blind resend after a timeout, ambiguous server error or unreadable response.

| Inner result                       | Meaning                                                        |
| ---------------------------------- | -------------------------------------------------------------- |
| `request_status = "accepted"`      | A successful provider response, not proof of a physical effect |
| `request_status = "rejected"`      | A non-ambiguous refusal; `status = "rejected"`                 |
| `request_status = "unknown"`       | Timeout/network/partial server error; effect may have happened |
| `status = "observed"`              | Accepted and requested reported values matched one readback    |
| `status = "requested_unconfirmed"` | Mismatch, missing readback, ambiguity, fade or announcement    |

Positive fades always remain unconfirmed even if an immediate readback happens to match.
Climate confirmation means the reported **setpoint/mode**, not that the room reached it.
Media confirmation means its reported state or volume, not audible playback.
Numeric readback tolerances are 1 brightness step, 1 per RGB channel, 0.01 volume and 0.05 for other numeric fields; these account only for reporting precision.
Readbacks must also remain within their applicable profile/device bounds (`within_profile`);
a tolerance never confirms a value just beyond a configured limit.

Readback failures and subsequent volume changes are returned without compensating effects.
The outer dispatcher can return `ok` for a rejected or unconfirmed receipt because the handler successfully reported it.
Inspect `request_status`, `status`, `confirmed`, `error` and `readback_errors`/`readback_error`.
Typed controls, lighting presets, and announcements also report `effect_unknown`, which is `true` exactly when a submission's outcome is unknown.
The core action journal then records the call as `uncertain` and refuses an identical request until you check the device and run `switchboard actions resolve`.
The generic service tool raises an uncertain error instead when its request may have been delivered.
Preflight validation failures use `ToolError` before submission.
Manual changes and other automations can race these reads; bounds are request-time guards, not a permanent governor.

## Sensor and energy summaries

`home_sensor_summary` accepts optional `entity_id`, `room`, `kind`, `limit` (1 to 200, default 100) and `source` (`live`, the default, or `cached`).
A specific sensor must be exposed in the requested scope.
Cached requests require polling and a specifically requested sensor must be in `poll.entities`.
Live failures are errors, not silent substitutions of old cached values.

Classification uses reported `device_class`, never names or guessed device capabilities:

| Reported class/domain        | Summary kind | Value and unit                                      |
| ---------------------------- | ------------ | --------------------------------------------------- |
| `sensor` / `temperature`     | temperature  | Finite number, original Celsius/Fahrenheit/kelvin   |
| `sensor` / `humidity`        | humidity     | Reported percentage                                 |
| `binary_sensor` / `motion`   | motion       | Boolean meaning motion detected                     |
| `binary_sensor` / `moisture` | leak         | Boolean meaning moisture detected, not safety proof |
| `sensor` / `battery`         | battery      | Percentage, separate from low-battery binary state  |
| `binary_sensor` / `battery`  | battery      | Boolean meaning low battery; no invented percentage |
| `sensor` / `power`           | power        | Reported instantaneous power with original unit     |
| `sensor` / `energy`          | energy       | Reported meter value/unit and `state_class`         |

Supported power units are the provider's milliwatts through terawatts.
Supported energy units follow the provider list, including joules, watt-hours and their supported multiples.
Units are returned unchanged.
Unrecognized/missing units, non-finite readings and percentages outside 0 to 100 are not marked usable.
An unclassified or never-observed entity is reported without inventing a class.
Unknown/unavailable values are `null`, not zero, false or a fabricated last-known current value.

There is no universal unit conversion, power integration, combined energy total, daily usage estimate, tariff lookup or cost calculation.
Different meters may overlap and counters may reset.
An energy reading is not a daily household total.
Fields include `value`, `unit`, `meaning`, `availability`, `stale`, `usable`, `reason`, `observed_at`, `last_updated` and the reported `state_class`.

## Sampling, events, jobs and storage

When `poll.enabled = true`, the `home.poll` job is added at the configured interval.
Its first run is due when your runner starts.
`home_poll` provides the same one-shot operation on demand.
Both fetch `GET /api/states`, validate the response, and retain only exact configured, exposed, non-denied entities.
An entire sampling failure preserves the prior snapshots and records safe health metadata.

Run one deliberate event-processing runner on your machine:

```bash
switchboard run
```

That command polls **all enabled connectors** and processes their rules, not just home.
`run --once` also runs every scheduled job once; it is not a harmless home-only diagnostic.
Review your existing rules and policy before running either.
`home_poll` by itself enqueues events but does not process rules.

### Event contract

The only emitted event type is `home.state_changed`, source `home`.
It is a **local sampled observation**,
not Home Assistant's native event stream:

- The first successful observation of each configured entity is a silent baseline, including a missing entity.
- Later changes in state, retained attributes or availability enqueue one durable event.
- Timestamp-only changes and identical repeated samples do not enqueue events.
- `unknown`, `unavailable`, disappearing (`missing`) and recovery can be sampled transitions.
- A newly configured entity gets its own silent baseline.
- Staleness caused by elapsed local time is a read-time flag, not a fabricated device transition.
- Poll failures update health, not device snapshots; there is no invented state event for an error.

Payload keys are `entity_id`, `previous`, `current`, `observed_at`, `revision`, opaque `source` and `history_complete = false`.
Previous/current contain the entity, bounded safe attributes, availability, provider timestamps and the local observation time.
The deduplication key is `home:<server_digest>:<entity_id>:<revision>`.
A repeated unchanged sample or restart reuses the baseline.

`home:1` creates `home_snapshots` and `home_poll_status`.
[`HomeStore.success`](../../src/switchboard/connectors/home/store.py) reads the previous snapshot, assigns its revision, writes snapshots/health and calls `events.emit(..., connection=conn)` inside the same database transaction.
Failure rolls back both snapshot advancement and enqueueing.
Older concurrent samples do not overwrite a newer recorded attempt.
This is local atomicity, not exactly-once external effects.

Server namespaces use a digest of the configured address.
Changing servers starts fresh baselines.
Removed targets are filtered from cached reads; existing local rows and queued events are not automatically erased.
The core event retention policy controls processed-event payload retention.
There is no separate home retention job, automatic source cleanup or backfilled history.

### Availability and freshness

`home_poll_status` is offline and returns `enabled`, interval/stale threshold, `attempted_at`, `succeeded_at`, `error`, overall `stale`, approved `entities`, `history_complete` and `freshness_basis`.
Entities include reported availability, revision, local observation time and their own `stale` flag.
Before a baseline they are explicitly `not_observed`.
A failure preserves the last good observation and sets `error`; a cached reading with that error is not marked usable even before its age threshold.

Freshness means the age of the **local read**.
Old `last_changed`/`last_updated` values alone do not imply unavailability: a device may simply be unchanged.
Conversely, a successful API read does not prove the physical integration's state is fresh.
Future/invalid local timestamps are treated as stale.
Polling misses intermediate changes, transient motion/leak signals, downtime and fast toggles.
There is no WebSocket subscription, recorder history query or claim of complete real-time history.

## Legacy tool details

`home_states` retains optional `entity_id`, `domain` and `limit` (1 to 200, default 100).
It returns `entity_id`, `state`, `last_changed` and only scalar `friendly_name`, `device_class`,
`unit_of_measurement`, `current_temperature`, `temperature`, `hvac_action` and `brightness`.
Camera tokens, artwork addresses and arbitrary attributes are omitted.

`home_call_service` retains `domain`, `service`, `entity_id` and optional bounded `data`.
The domain must equal the entity's domain, and deny/exposure checks still apply.
Its legacy `changed` list contains exposed states reported by the provider, not independent readback or physical confirmation.

| Legacy data key                                      | Existing bounds                             |
| ---------------------------------------------------- | ------------------------------------------- |
| `brightness_pct`, `brightness`                       | Integer 0 to 100 / 0 to 255                 |
| `color_temp_kelvin`, `rgb_color`, `transition`       | 1,000 to 20,000 / three 0 to 255 / 0 to 300 |
| `temperature`, `target_temp_low`, `target_temp_high` | -50 to 100, without a new unit argument     |
| `hvac_mode`                                          | Lowercase mode name, up to 32 characters    |
| `preset_mode`, `fan_mode`                            | Text, 1 to 64 characters                    |
| `percentage`, `humidity`                             | Integer 0 to 100                            |
| `volume_level`, `is_volume_muted`                    | Number 0 to 1 / boolean                     |
| `source`, `option`                                   | Text, 1 to 100 characters                   |
| `value`                                              | Bounded number/string/boolean               |

The legacy `value` number is -1,000,000 to 1,000,000 and its string is at most 255 characters.
Extra `data` keys, especially `entity_id`, `device_id`, `area_id`, `floor_id` and `label_id`, are refused.
New action schemas additionally require their binding and reject all unrecognized top-level fields.

## Verified provider references and proof limits

Public primary references were read on 2026-11-11:

- [REST endpoints, permissions, authentication, state reads and action replies][rest].
- [Light actions][light] and [light entity capabilities/color translation][light-entity].
- [Climate target temperature/range call shape][climate-action] and [climate reported units, bounds and modes][climate-entity].
- [Fan percentage action][fan-action].
- [Media volume action][volume-action], [mute action][mute-action] and [media entity states, grouping and supported features][media-entity].
- [Speak action][speak] and [TTS caching and playback troubleshooting][tts].
- [Numeric sensor classes/units][sensor] and [binary sensor meanings][binary-sensor].

Feature-bit values are verified against provider constants at commit `0dc24540ce48f65f616230857e2cf21ceec92b80`:
[light][light-flags], [climate][climate-flags], [fan][fan-flags] and [media player][media-flags].
Only the specific reported capabilities used by an action are required; no feature is inferred from a name.

The synthetic authored tests cover configuration/setup, concrete targets, schema bounds, stale approvals, legacy profile bypass refusal, policy gates, event/snapshot rollback, baselines/restarts, units, quiet hours, availability, ambiguous effects, readback and announcement privacy.
They are in `tests/unit/test_home*.py` and `tests/integration/test_home_extension.py`, with synthetic fixtures under `tests/fixtures/home/`.
Formatting, lint, static types and data parsing are not runtime evidence.
Integration-version changes, actual device support, system-zone availability, network/certificate setup, engine privacy declarations, audio format/reachability and playback delivery remain unverified on your machine.

[rest]: https://contributors.home-assistant.io/core/internals/rest-api/
[light]: https://www.home-assistant.io/actions/light.turn_on/
[light-entity]: https://contributors.home-assistant.io/core/entities/light/
[climate-action]: https://www.home-assistant.io/actions/climate.set_temperature/
[climate-entity]: https://contributors.home-assistant.io/core/entities/climate/
[fan-action]: https://www.home-assistant.io/actions/fan.set_percentage/
[volume-action]: https://www.home-assistant.io/actions/media_player.volume_set/
[mute-action]: https://www.home-assistant.io/actions/media_player.volume_mute/
[media-entity]: https://contributors.home-assistant.io/core/entities/media-player/
[speak]: https://www.home-assistant.io/actions/tts.speak/
[tts]: https://www.home-assistant.io/integrations/tts/
[sensor]: https://www.home-assistant.io/integrations/sensor/
[binary-sensor]: https://www.home-assistant.io/integrations/binary_sensor/
[light-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/light/const.py
[climate-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/climate/const.py
[fan-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/fan/const.py
[media-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/media_player/const.py

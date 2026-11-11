# Native Home Assistant adapter

This C++20 adapter uses Home Assistant's Representational State Transfer (REST) application programming
interface (API), with JavaScript Object Notation (JSON) requests. It has no Python subprocess,
interpreter embedding, fallback, or dependency on Python home state. The Python baseline `6989a4b`
is a read-only behavior reference, not evidence that native behavior has run.

**Validation boundary:** source and authored tests have received syntax/type checking only.
Every test below is **UNRUN**. No project program, test collection, test, example, build, account, or
device was executed or contacted. Public provider documentation was read on 2026-11-11.

**Not a safety controller.** Review the actual physical effect of every concrete entity. Locks,
alarm panels, covers including garage doors, cameras, switches, scripts, scenes, automations,
appliance relays and other domains outside the six listed below cannot be configured. A misleading
`light`, `fan`, or `climate` entity can still conceal unsafe integration behavior or high-power
equipment; names and reported membership cannot prove otherwise. Do not review such entities for
use here. Keep physical interlocks and manual control independent.

## Tools and tiers

Entry point: `switchboard::register_home(Context&, Registry&)` in
[`home.cpp`](../src/home.cpp). It returns immediately unless `Context.enabled("home")`.
Enabled registration validates settings and registers exactly seven tools, without resolving a
secret, making a provider request, creating home tables, emitting events, or scheduling work.
Empty profiles are allowed, but their action schemas admit no targets.

The literal tiers `read` and `act` mean read-only and externally acting tools.

| Tool                | Tier   | Inputs and behavior                                                                                                                                                |
| ------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `home_targets`      | `read` | Empty object; offline exact exposures, rooms, profiles and current `binding`. No token/reference or server address is returned.                                    |
| `home_states`       | `read` | Optional `entity_id`, `domain`, `room`, `limit` (integer 1-200, default 100). One exact state or filtered, sorted states.                                          |
| `home_capabilities` | `read` | Required exact exposed `entity_id`; optional `room`. Live reported attributes, feature decoding, availability, leaf indication, reviewed profile and binding.      |
| `home_lights`       | `act`  | Required `binding`, `entity_ids` (1-16 distinct reviewed lights), `state` (`on`/`off`); optional `room`, `brightness_pct`, `rgb_color`, `color_temp_kelvin`.       |
| `home_climate`      | `act`  | Required `binding`, one profiled `entity_id`, `unit`; optional `room`, `temperature`, complete `target_temp_low`/`target_temp_high`, `hvac_mode`.                  |
| `home_fan`          | `act`  | Required `binding`, one profiled `entity_id`, integer `percentage`; optional `room`.                                                                               |
| `home_media`        | `act`  | Required `binding`, one profiled `entity_id`, `action` (`play`, `pause`, `stop`, `volume`, `mute`); optional `room`; only the selected action's value is accepted. |

`home_capabilities` uses `GET /api/states/<entity_id>`, not a fabricated service catalog or a
device probe. Reported support is advisory even when unavailable or unprofiled; it never
authorizes an action. Sensors return no inferred writable capabilities.

Assistant and rule `act` calls require approval under the runtime's default policy. Explicit
configuration can change that policy; the example pins each action to `ask`. A trusted human-origin
call counts as approval, but cannot bypass denial or the core kill switch. Approval, expiry,
rate limits, single-use claims and current-policy rechecking belong to the shared runtime,
not a second adapter-specific authorization path. Direct handlers are for trusted internal use.
Do not approve from a shortened summary; review the complete stored arguments.

## Configuration

Merge [`home.example.json`](../config/home.example.json) into the native configuration, preserving
other enabled connectors and policy. All addresses and entity names are fictional. Deliberately
append `home` to the connector list. The example uses separate native storage.

Use a dedicated non-administrator Home Assistant user and a long-lived access token. That token
is **not entity-scoped**; the adapter's local allowlists are an additional software boundary.
Use `env:NAME` or an owner-private `file:PATH` secret reference handled by `Context.secret`.
Command references and literal credentials are rejected. Registration does not read the file or
environment. Credentials are resolved for each actual request and never returned or hashed.

Unknown settings are rejected, including unsupported settings set to `false` or empty objects.
The same applies to extra keys in device profiles and action inputs. JSON booleans are not numbers;
numeric settings must be finite.

| Setting         | Contract                                                                                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `base_url`      | Required, at most 2,048 characters; ASCII `https://host[:port]` origin, optional final slash. No credentials, path prefix, query, fragment, or percent escapes.     |
| `token`         | Required `env:` or `file:` reference, at most 2,048 characters. Environment names match `[A-Za-z_][A-Za-z0-9_]{0,127}`. Secret-file access is enforced by the core. |
| `allow_http`    | Boolean, default `false`. Hypertext Transfer Protocol (HTTP) without encryption requires explicit `true` and exposes the token on the network.                      |
| `expose`        | Default `[]`; at most 200 distinct exact lowercase entity identifiers. No globs, devices, areas, floors or labels.                                                  |
| `deny_domains`  | Default `[]`; at most 100 distinct lowercase domain names; **additional denials only**, never an override of the hard domain restriction.                           |
| `lights`        | Default `[]`; at most 200 distinct exposed, non-denied `light` entities, explicitly reviewed as physical leaf devices.                                              |
| `rooms`         | Default `{}`; at most 100 lowercase aliases of 1-64 characters, each containing 1-200 distinct exposed, non-denied entities.                                        |
| `climate`       | Default `{}`; at most 100 exact exposed `climate` profiles. Required `unit`, `minimum`, `maximum`, `modes`; optional `max_change`.                                  |
| `fans`          | Default `{}`; at most 100 exact exposed `fan` profiles. Optional integer `minimum` (default 0), `maximum` (100), `max_change` (100).                                |
| `media_players` | Default `{}`; at most 100 exact exposed `media_player` profiles. Required numeric `maximum`; optional `minimum` (default 0).                                        |

Only `sensor`, `binary_sensor`, `light`, `climate`, `fan`, and `media_player` are permitted in
exposure and rooms. Each identifier matches `[a-z0-9_]{1,64}\.[a-z0-9_]{1,190}`.
Every action target also needs its typed profile; exposure or room membership alone does not grant
control. `room` only restricts explicitly supplied targets and **never expands** them.

Climate `unit` is `C` (Celsius) or `F` (Fahrenheit), matching the provider's **reported**
`temperature_unit` (`"\u00b0C"` or `"\u00b0F"`). Configuration bounds are -50 to 100 Celsius or
-58 to 212 Fahrenheit, with `maximum >= minimum`. Configure a much narrower suitable range.
`max_change` defaults to 2 in that unit, bounded to 0.1-20. `modes` explicitly lists 1-5 distinct
members of `off`, `heat`, `cool`, `heat_cool`, `fan_only`. No automatic unit conversion occurs.

Fan bounds are integer percentages 0-100 with `minimum <= maximum`; `max_change` is 1-100.
Media bounds are fractions 0-1 with `minimum <= maximum`, not measured loudness or hearing-safety
guarantees. Change caps can also block large reductions; manual controls remain independent.

The `mcp.expose` setting selects tools exposed through the Model Context Protocol (MCP).
It is not the home entity allowlist. Core policy settings and the operator's secret-file rules are
independent of this adapter's configuration.

## Binding and reloads

Read `home_targets` before each new action. Copy its full 64-character `binding` into the action
you review. Every action schema pins that value and enumerates its concrete reviewed target set;
the handler checks again before network access. Duplicate light targets fail before any preflight.

Binding version `native_binding_version = 1` hashes the component `home`, normalized server origin,
`allow_http`, exact exposures, additional denials, all lights/rooms, climate modes/units/limits,
and fan/media limits. Entity sets, room members and mode sets are sorted; profile numbers and defaults
are normalized. Origin normalization lowercases it, removes a final slash and host dot, and removes
default ports. Alternate IPv6 spellings can conservatively yield different bindings.
No secret value, secret reference, timestamp, pointer, unordered iteration, or process identity enters
the digest. Rotating a credential reference alone preserves the binding.

On runtime reconstruction, stored approval arguments are revalidated against the new schema.
Changing an endpoint, target, room, denial, or limit invalidates the old binding even if the old
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

Effects use exactly one `POST /api/services/<domain>/<service>`, containing one concrete `entity_id`
or, for multi-light calls, the exact explicit entity list and only typed values. Every target is
preflighted before that submission. Multi-light effects can still be partial and are not atomic.
The provider's returned changed-state list is ignored; only independent per-target reads are evaluated.
A successful response with an unreadable body can therefore still be accepted, but that body never
proves anything.

Every request has a 15-second timeout and `Authorization: Bearer ...` plus `Accept: application/json`.
Posts additionally use `Content-Type: application/json`. State responses are capped at 1,048,576 bytes;
action responses at 65,536 bytes. The real shared transport verifies certificates and does not redirect
or retry. The adapter does not retry, sleep, schedule readback, compensate, or resend.

| Result                                  | Meaning                                                                                                                                            |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `request_status = accepted`             | A bounded successful 2xx response, not physical completion.                                                                                        |
| `request_status = rejected`             | A bounded 3xx/4xx response except 408; no redirect and no readback.                                                                                |
| `request_status = unknown`              | Transport exception, response-cap failure, 408, 5xx or other ambiguous status; one readback is attempted, never another effect.                    |
| `status = observed`, `confirmed = true` | Accepted request and one independent matching, available, leaf-reported, within-profile readback for every explicit target.                        |
| `status = requested_unconfirmed`        | Mismatch, ambiguity, lost capability, unavailable/grouped/out-of-profile result or failed readback. Read errors are reported in `readback_errors`. |

Rejections have `status = rejected`. An unknown submission never becomes confirmed merely because
its readback matches. Numeric comparison tolerances are one brightness step, one per color channel,
0.01 volume and 0.05 for other numeric values. Brightness percent is compared with its nearest
0-255 step. Profile/device bounds are checked separately without tolerance. Matching just beyond a
volume/temperature bound does not confirm the action. Readback rechecks applicable capabilities,
units and memberships as well as requested values.

Every receipt has `physical_delivery_verified = false`, `retry_safe = false`, observations and
safe error metadata. A runtime outer `status = ok` means the handler returned a receipt; inspect
the inner `request_status`, `status`, `confirmed`, and `readback_errors`.
**Readback is software evidence only, not verified physical delivery or causal proof.**
Another automation or a manual change can race all reads.

## State, events, jobs and migrations

This adapter is deliberately stateless: **no module migrations, no `native_home_*` tables, snapshots,
polling, jobs, event emission, or subscriptions**. It does not read or write Python home tables.
Only the shared runtime persists its normal native approvals, audit metadata and call records.
Every read and capability check is live; there is no stale cache fallback.
No event-processing or rules compatibility is claimed.

## Native versus Python

The Python reference offers twelve tools when speech is enabled; this native adapter offers seven,
including the new explicit `home_capabilities` read. This is not a drop-in Python configuration port.

| Python reference feature                                                   | Native coverage or explicit limit                                                                                                                                                           |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `home_states`                                                              | Live reads remain, but exposure is exact-only, six hard domains, additional room restriction and richer safe capability observations. No legacy permissive-domain behavior.                 |
| `home_targets`, typed light/climate/fan/media tools                        | Implemented with native-only canonical binding, concrete schemas, one-shot effects and independent observed/unconfirmed receipts. Bindings/results are not Python-compatible state formats. |
| Exposure globs; replaceable legacy deny list                               | Rejected/narrowed: exact entities only; additional denials cannot remove hard restrictions.                                                                                                 |
| Reverse-proxy path in `base_url`; command secrets                          | Rejected: origin-only endpoints and `env:`/`file:` references.                                                                                                                              |
| `home_call_service`                                                        | Not registered. No generic service, arbitrary `data`, select/number/switch actions, scenes, scripts or legacy profile bypass.                                                               |
| Light transitions/fades and lighting presets                               | Not supported. `transition` inputs and `lighting_presets` settings are rejected, including zero/disabled forms. `home_lighting_preset` is absent.                                           |
| Quiet hours and time-zone handling                                         | Not supported. `quiet_hours`, standalone `timezone` and other unknown settings are rejected; there is no hidden time-based governor.                                                        |
| Speech/announcements and provider-privacy declarations                     | Not supported. `speech` settings are rejected and `home_announce` is absent. No speech engine or audible-delivery claim.                                                                    |
| Classified sensor/energy summaries                                         | Raw safe sensor/binary-sensor states and original attributes only. No `home_sensor_summary`, classification, normalized units, aggregate energy, tariffs or costs.                          |
| Polling, cached summaries, health, baselines, snapshots and sampled events | Not supported. `poll` settings rejected; `home_poll`, `home_poll_status`, `home.state_changed`, home jobs and home migrations are absent.                                                   |
| Additional device controls                                                 | No fan presets/direction/oscillation, climate auto/dry/humidity/presets, media content/seek/source/grouping, light effects/flashes, or unsupported domains. Unknown inputs fail closed.     |

The native adapter is stricter about reported groups and known light modes. It checks the effective
climate mode on temperature readback and does not return the action reply's changed-state list.
There is no physical delivery verification in either implementation.

## Coverage and UNRUN tests

[`native/tests/home.cpp`](../tests/home.cpp) includes `support.hpp`, its deterministic `FakeHttp`,
and a wrapper that can throw on a fake submission. No test creates a real transport.
Fixtures, if explicitly run later, create owner-private directories relative to the test's working
directory, not the shared support helper's system temporary directory. They restore fake environment
credentials and remove their own fixtures.

All 28 test functions below are **authored, UNRUN**. The described assertions are intended coverage,
not passed results:

| Test function(s)                                               | Authored assertions                                                                                                                                                               |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_disabled_and_offline_registration`                       | Disabled early return; seven tools and tiers; no token resolution, requests, jobs or home tables; unsupported tools absent; empty allowlists fail closed.                         |
| `test_settings_reject_unsupported_and_unsafe_scope`            | Unsupported settings even when disabled; malformed/nested values, endpoints, secrets, duplicates, globs, unsafe domains and limits.                                               |
| `test_canonical_binding_and_reload`                            | Stable normalized sets/defaults/numbers/origin/credential references; every reviewed configuration class changes the binding; reloaded schema and handler reject stale arguments. |
| `test_schemas_bind_targets_and_bound_shapes`                   | Catalog target/binding enumeration; numeric/array limits and types; exposed room membership cannot bypass a missing profile.                                                      |
| `test_plain_http_requires_explicit_opt_in`                     | Secure default and explicit exception forwarding.                                                                                                                                 |
| `test_states_exact_requests_and_redaction`                     | Exact headers/method/address, response cap, timeout, filtering/sorting/truncation and attribute redaction.                                                                        |
| `test_states_single_entity_room_and_validation`                | Exact one-entity request, room restriction, path injection, conflicting domain and invalid shapes.                                                                                |
| `test_states_malformed_duplicate_and_capped_responses`         | Invalid/duplicate states, oversized bodies and safe provider-refusal errors.                                                                                                      |
| `test_capabilities_are_live_and_advisory`                      | Live feature/profile/unit reporting, unavailable status, sensor non-inference and zero effects.                                                                                   |
| `test_light_exact_request_and_observed_readback`               | Exact brightness/color and kelvin bodies, headers/caps, independent observed state, no physical-delivery claim.                                                                   |
| `test_multi_light_preflight_and_concrete_room_scope`           | All preflights before one exact multi-target post, per-target readback, no partial submission on preflight failure, no room expansion.                                            |
| `test_light_rejects_conflicts_groups_and_missing_capabilities` | Duplicates, incompatible settings, unsupported transitions, target injection, missing color/brightness support, group metadata, availability and kelvin bounds.                   |
| `test_climate_single_and_range_requests`                       | Exact single/range service bodies and observed results.                                                                                                                           |
| `test_climate_modes_and_fahrenheit`                            | Mode-only off without setpoints, bounded entry into heat, explicit Fahrenheit with no conversion.                                                                                 |
| `test_climate_refuses_unit_bounds_caps_and_change`             | Unit mismatch, incomplete/reversed/mixed ranges, unreviewed modes, missing/boolean features, absent prior setpoints, bounds/change caps and unsafe mode-only heating.             |
| `test_fan_limits_change_and_zero`                              | Exact bounded percentage request, zero/off behavior and independent confirmation.                                                                                                 |
| `test_fan_rejects_unknown_percentage_and_features`             | Maximum/change limits, missing/invalid prior percentages, absent features and groups.                                                                                             |
| `test_media_actions_exact_requests`                            | Play/pause/stop/volume/mute/unmute exact bodies; no implicit auxiliary action.                                                                                                    |
| `test_media_refuses_volume_group_features_and_values`          | Action/value pairing, content/group rejection, unsafe/unknown volume, missing feature, grouped player and explicit mute with unknown volume.                                      |
| `test_readback_mismatch_unavailable_and_profile_tolerance`     | Mismatch/unavailability, tolerance outside a profile/color-channel bound, changed group membership and lost media capability; no resend.                                          |
| `test_submission_unknown_and_rejected_without_retry`           | Fake 503/500/408/zero ambiguity, 403/401/302/429 refusal, thrown transport error, safe diagnostics and at-most-one submission.                                                    |
| `test_post_body_is_not_confirmation`                           | Changed-state reply cannot replace readback; unreadable success body and over-cap ambiguity.                                                                                      |
| `test_readback_failure_is_reported_without_resend`             | 404, non-JSON, wrong entity and malformed attributes become unconfirmed receipts, not resubmissions.                                                                              |
| `test_secret_resolution_is_lazy_and_errors_are_redacted`       | Offline targets with an invalid fake credential; safe failure before transport.                                                                                                   |
| `test_runtime_default_approval_and_single_submission`          | Assistant/rule default pending approvals with no preflight, deny, fresh preflight on approval and single-use execution.                                                           |
| `test_runtime_reload_rejects_stale_approval`                   | Pending approval persisted across runtime destruction; changed limit rejects it before provider work.                                                                             |
| `test_runtime_credential_rotation_preserves_review_binding`    | Pending approval survives secret-reference-only rotation and resolves the new fake credential at execution.                                                                       |
| `test_runtime_policy_rechecked_on_approval_and_human_calls`    | A new deny policy wins over a persisted approval and trusted human origin.                                                                                                        |

No runtime coverage, physical effects, credentials/file permissions against a real installation,
Home Assistant version compatibility, network/certificate reachability, build/link success,
concurrency/races, device quantization, or hidden provider integration behavior has been verified.
Approval tests exercise the shared runtime by authored calls, not by running it during development.

## Syntax-only verification

From the repository root, this portable template describes the syntax-only check.
Replace the dependency-directory placeholder with your locally restored header directory:

```bash
c++ -std=c++20 -Wall -Wextra -Wpedantic -fsyntax-only \
    -I native/include \
    -isystem /path/to/validation-dependencies/usr/include \
    native/src/home.cpp native/tests/home.cpp
```

This checks only these two translation units; it neither links the native engine nor executes any
test. Do not interpret it as provider compatibility or runtime coverage. CMake configuration/build
and test collection/execution were not performed.

Authoring result on 2026-11-11: exit status 0, with no diagnostics for either translation unit.
The example JSON, local guide links, ASCII/whitespace and the 28-function guide inventory were
also checked as data/text only, without importing or executing project code.

## Public references

- [REST authentication, state reads and action replies][rest].
- [Light color modes, brightness and color-temperature attributes][light].
- [Climate reported units, modes, bounds and feature semantics][climate].
- [Single/range climate action data][climate-action].
- [Fan percentages and speed conversion limits][fan].
- [Media feature flags, states, volume and group membership][media].
- Provider bit constants inspected at commit `0dc24540ce48f65f616230857e2cf21ceec92b80`:
  [climate][climate-flags], [fan][fan-flags], [media player][media-flags].
  The adapter uses climate target bits 1/2, fan speed bit 1, and media pause/play/stop/volume/mute
  bits 1/16384/4096/4/8 respectively. No feature is inferred from entity names.

[rest]: https://contributors.home-assistant.io/core/internals/rest-api/
[light]: https://contributors.home-assistant.io/core/entities/light/
[climate]: https://contributors.home-assistant.io/core/entities/climate/
[climate-action]: https://www.home-assistant.io/actions/climate.set_temperature/
[fan]: https://contributors.home-assistant.io/core/entities/fan/
[media]: https://contributors.home-assistant.io/core/entities/media-player/
[climate-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/climate/const.py
[fan-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/fan/const.py
[media-flags]: https://github.com/home-assistant/core/blob/0dc24540ce48f65f616230857e2cf21ceec92b80/homeassistant/components/media_player/const.py
